"""Portable contract tests for the single shared source of storage/budget logic."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from devflow_core.common.storage import (DevFlowError, atomic_json, checked_text, digest,
    file_lock, json_bytes, no_symlinks, read_json, within, write_new)
from devflow_core.common.errors import ReviewerError
from devflow_core.common.review import all_approved, check_fields, next_reviewer, review_budget, validate_limit
from devflow_core.common.settings import validate_document, workflow_overlay
from devflow_core.common.environment import (GIT_FACT_SECONDS, PROBE_SECONDS, REVIEW_SETUP_SECONDS, bash_timeout_ms,
                                              python_errors, review_timeout_ms)
from devflow_core.common.paths import msys_path
from devflow_core.common import runs

# The one canonical field error; the plan and implement validator tests share it.
FIELD_ERROR = 'findings[0] has missing or unknown fields (missing: none; unknown: "consequence")'


class SharedStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="devflow common ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_utf8_atomic_round_trip_and_determinism(self):
        path = self.root / "records" / "日本語.json"
        atomic_json(path, {"request": "日本語\nanswer", "version": 1})
        self.assertEqual(read_json(path)["request"], "日本語\nanswer")
        first = path.read_bytes()
        atomic_json(path, {"version": 1, "request": "日本語\nanswer"})
        self.assertEqual(first, path.read_bytes())
        self.assertEqual(len(digest(first)), 64)

    def test_immutable_create_never_replaces_existing_bytes(self):
        path = self.root / "artifact.txt"
        write_new(path, b"original")
        with self.assertRaises(DevFlowError):
            write_new(path, b"replacement")
        self.assertEqual(path.read_bytes(), b"original")
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_duplicate_keys_and_nonfinite_json_rejected(self):
        path = self.root / "invalid.json"
        for raw in (b'{"a":1,"a":2}', b'{"x":NaN}', b'{"x":Infinity}', b'[]', b'null'):
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                with self.assertRaises(DevFlowError):
                    read_json(path)
        with self.assertRaises(ValueError):
            json_bytes({"x": float("nan")})

    def test_size_limit_is_checked_before_json_load(self):
        path = self.root / "large.json"
        path.write_bytes(b'{"x": "large"}')
        with self.assertRaises(DevFlowError):
            read_json(path, 4)

    def test_text_rejects_empty_binary_missing_and_over_budget(self):
        path = self.root / "text.md"
        for raw in (b"  \n", b"hello\0world", b"\xff"):
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                with self.assertRaises(DevFlowError):
                    checked_text(path)
        path.write_bytes(b"valid")
        with self.assertRaises(DevFlowError):
            checked_text(path, 2)
        with self.assertRaises(DevFlowError):
            checked_text(self.root / "missing")
        self.assertEqual(checked_text(path), "valid")

    def test_path_must_be_below_base(self):
        self.assertEqual(within(self.root / "draft" / "plan.md", self.root), self.root / "draft" / "plan.md")
        for path in (self.root, self.root / ".." / "escape"):
            with self.subTest(path=path):
                with self.assertRaises(DevFlowError):
                    within(path, self.root)

    def test_symlink_components_fail_closed(self):
        source = self.root / "source"
        source.mkdir()
        link = self.root / "alias"
        try:
            link.symlink_to(source, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Creating symlinks requires OS permission")
        with self.assertRaises(DevFlowError):
            no_symlinks(link / "new.txt")
        with self.assertRaises(DevFlowError):
            write_new(link / "new.txt", b"not allowed")
        self.assertFalse((source / "new.txt").exists())

    def test_lock_is_exclusive_released_and_never_guessed_stale(self):
        path = self.root / "state.lock"
        with file_lock(path):
            record = read_json(path)
            self.assertIn("pid", record)
            with self.assertRaises(DevFlowError):
                with file_lock(path):
                    self.fail("Second lock unexpectedly acquired")
        self.assertFalse(path.exists())
        stale = {"token": "historical", "pid": -123, "host": "unknown"}
        write_new(path, json_bytes(stale))
        with self.assertRaises(DevFlowError):
            with file_lock(path):
                self.fail("Existing lock unexpectedly removed")
        self.assertEqual(read_json(path), stale)


class FieldCheckTests(unittest.TestCase):
    EXPECTED = {"id", "evidence"}

    def message(self, value, label="findings[0]"):
        with self.assertRaises(ReviewerError) as caught:
            check_fields(value, self.EXPECTED, label)
        return str(caught.exception)

    def test_exact_fields_pass(self):
        self.assertIsNone(check_fields({"evidence": "e", "id": "F1"}, self.EXPECTED, "findings[0]"))

    def test_canonical_message(self):
        self.assertEqual(self.message({"id": "F1", "evidence": "e", "consequence": "c"}), FIELD_ERROR)

    def test_missing_only(self):
        self.assertEqual(self.message({}, "Review"),
                         'Review has missing or unknown fields (missing: "evidence", "id"; unknown: none)')

    def test_names_are_sorted_whatever_the_input_order(self):
        for order in (("b", "a", "c"), ("c", "b", "a")):
            with self.subTest(order=order):
                value = {"id": "F1", "evidence": "e", **{name: 1 for name in order}}
                self.assertTrue(self.message(value).endswith('(missing: none; unknown: "a", "b", "c")'))

    def test_at_most_eight_names_per_category(self):
        value = {f"x{index:02d}": 1 for index in range(10)}
        shown = ", ".join(f'"x{index:02d}"' for index in range(8))
        self.assertEqual(self.message(value),
                         'findings[0] has missing or unknown fields '
                         f'(missing: "evidence", "id"; unknown: {shown}, and 2 more)')

    def test_long_names_are_cut_before_escaping(self):
        name = "n" * 100
        self.assertIn(f'unknown: "{"n" * 64}")', self.message({"id": "F1", "evidence": "e", name: 1}))

    def test_control_and_bidirectional_characters_are_escaped(self):
        message = self.message({"id": "F1", "evidence": "e", "a\n\u202eb": 1})
        self.assertIn('unknown: "a\\n\\u202eb")', message)
        self.assertNotIn("\n", message)
        self.assertNotIn("\u202e", message)

    def test_non_objects_are_rejected(self):
        for value in (None, [], "id", 1):
            with self.subTest(value=value):
                self.assertEqual(self.message(value), "findings[0] must be a JSON object")


class SharedReviewTests(unittest.TestCase):
    def setUp(self):
        self.reviewers = [{"id": "codex"}, {"id": "opencode"}]

    def test_hard_limit_and_no_boolean_integer_coercion(self):
        for value in (None, True, 1, 7, 6.0, "6"):
            with self.subTest(value=value):
                with self.assertRaises(DevFlowError):
                    validate_limit(value)
        self.assertEqual(validate_limit(6), 6)

    def test_alternating_schedule_cannot_dispatch_seventh(self):
        self.assertEqual([next_reviewer(self.reviewers, used, 6)["id"] for used in range(6)],
                         ["codex", "opencode", "codex", "opencode", "codex", "opencode"])
        for used in (-1, 6, True, "0"):
            with self.subTest(used=used):
                with self.assertRaises(DevFlowError):
                    next_reviewer(self.reviewers, used, 6)

    def test_same_digest_required_from_every_reviewer(self):
        self.assertFalse(all_approved(self.reviewers, {"codex": "A"}, "A"))
        self.assertFalse(all_approved(self.reviewers, {"codex": "A", "opencode": "B"}, "A"))
        self.assertTrue(all_approved(self.reviewers, {"codex": "A", "opencode": "A"}, "A"))
        self.assertFalse(all_approved([], {}, "A"))


class SharedSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="devflow settings ")
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name).resolve()
        self.path = self.project / "devflow.config.json"

    def test_empty_project_is_inert(self):
        self.assertEqual(workflow_overlay(self.project, "plan"), {})
        self.assertEqual(workflow_overlay(self.project, "implement"), {})
        self.assertEqual(list(self.project.iterdir()), [])

    def test_legacy_configuration_is_not_implicitly_imported(self):
        # Files named after the former standalone components are ignored.
        for name in ("plan" + "flow", "build" + "flow"):
            atomic_json(self.project / (name + ".config.json"), {"max_reviews": 2})
        self.assertEqual(workflow_overlay(self.project, "plan"), {})
        self.assertEqual(workflow_overlay(self.project, "implement"), {})

    def test_common_values_and_workflow_override_are_separate(self):
        atomic_json(self.path, {"schema_version": 1, "max_reviews": 6,
                              "reviewers": [{"id": "synthetic"}],
                              "plan": {"max_reviews": 4, "grill_skill": "interview"},
                              "implement": {"checks": []}})
        plan = workflow_overlay(self.project, "plan")
        implement = workflow_overlay(self.project, "implement")
        self.assertEqual(plan["max_reviews"], 4)
        self.assertEqual(implement["max_reviews"], 6)
        self.assertNotIn("checks", plan)
        self.assertNotIn("grill_skill", implement)
        plan["reviewers"][0]["id"] = "changed"
        self.assertEqual(implement["reviewers"][0]["id"], "synthetic")
        self.assertEqual(workflow_overlay(self.project, "plan")["reviewers"][0]["id"], "synthetic")

    def test_unknown_top_level_and_schema_versions_fail(self):
        names = ("plan", "implement")
        for value in ({"unknown": 1}, {"schema_version": True}, {"schema_version": 2},
                      {"schema_version": "1"}, {"plan": []}, {"implement": "other"}):
            with self.subTest(value=value):
                atomic_json(self.path, value)
                with self.assertRaises(DevFlowError):
                    validate_document(self.project, names)
                    workflow_overlay(self.project, "plan")
                    workflow_overlay(self.project, "implement")

    def test_top_level_keys_come_from_the_caller_registry(self):
        atomic_json(self.path, {"schema_version": 1, "deploy": {}})
        with self.assertRaisesRegex(DevFlowError, "deploy"):
            validate_document(self.project, ("plan", "implement"))
        validate_document(self.project, ("plan", "implement", "deploy"))

    def test_symlink_configuration_is_rejected(self):
        target = self.project / "other.json"
        atomic_json(target, {})
        try:
            self.path.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("Creating symlinks requires OS permission")
        with self.assertRaises(DevFlowError):
            workflow_overlay(self.project, "implement")


class ReviewBudgetTests(unittest.TestCase):
    def test_the_listed_next_reviewer_is_the_one_that_will_be_dispatched(self):
        config = {"max_reviews": 6, "reviewers": [{"id": "codex"}, {"id": "opencode"}, {"id": "second"}]}
        for used in range(6):
            with self.subTest(used=used):
                budget = review_budget(used, config, "ready")
                self.assertEqual(budget["next_reviewer"], next_reviewer(config["reviewers"], used, 6)["id"])
                self.assertEqual((budget["review_count"], budget["remaining_reviews"]), (used, 6 - used))
        self.assertIsNone(review_budget(6, config, "ready")["next_reviewer"])
        self.assertIsNone(review_budget(2, config, "completed")["next_reviewer"])


class PathTests(unittest.TestCase):
    def test_msys_paths_become_windows_paths_on_every_os(self):
        for value, expected in (("/c/Users/x", "C:/Users/x"), ("/d", "D:/"), ("/C/", "C:/"), ("/tmp/x", "/tmp/x"),
                                ("C:/x", "C:/x"), ("/cc/x", "/cc/x"), ("rel", "rel")):
            with self.subTest(value=value):
                self.assertEqual(msys_path(value), expected)

    def test_msys_or_cygwin_python_is_a_doctor_error(self):
        self.assertEqual(python_errors("linux"), [])
        self.assertEqual(python_errors("win32"), [])
        for platform in ("msys", "cygwin"):
            with self.subTest(platform=platform):
                self.assertEqual(python_errors(platform, "/usr/bin/python3"),
                                 [f"On Windows, DevFlow needs a native Windows Python; uv selected {platform} "
                                  "Python at /usr/bin/python3"])


class GitEnvironmentTests(unittest.TestCase):
    def test_the_users_configuration_files_are_kept_and_repository_variables_dropped(self):
        from devflow_core.common.git import git_environment
        with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": "/home/u/.gitconfig-work", "GIT_CONFIG_NOSYSTEM": "1",
                                     "GIT_DIR": "/elsewhere/.git", "GIT_INDEX_FILE": "/elsewhere/index"}):
            env = git_environment()
        self.assertEqual((env["GIT_CONFIG_GLOBAL"], env["GIT_CONFIG_NOSYSTEM"]), ("/home/u/.gitconfig-work", "1"))
        self.assertNotIn("GIT_DIR", env)
        self.assertNotIn("GIT_INDEX_FILE", env)
        self.assertEqual((env["GIT_TERMINAL_PROMPT"], env["GIT_OPTIONAL_LOCKS"]), ("0", "0"))


class EnvironmentTests(unittest.TestCase):
    def test_bash_timeout_is_the_process_limit_plus_the_runners_margin(self):
        self.assertEqual(bash_timeout_ms(900), 960000)
        # The longest review (3600 s) stays inside the 2-hour cap Claude Code puts on
        # an unattended background command's timeout.
        self.assertLess(review_timeout_ms(3600), 7200000)

    def test_versions_newer_than_the_checked_ones_are_warned_about(self):
        from devflow_core.common.environment import CHECKED_VERSIONS, version_warnings
        self.assertEqual(set(CHECKED_VERSIONS), {"claude", "codex", "opencode"})
        self.assertEqual(version_warnings("codex", "codex-cli 0.166.0"), [])
        self.assertEqual(version_warnings("codex", "codex-cli 0.150.2"), [])
        self.assertEqual(version_warnings("opencode", "opencode v2.2.5"), [])
        self.assertEqual(version_warnings("claude", None), [])
        self.assertEqual(version_warnings("claude", "not a version"), [])
        newer = version_warnings("opencode", "opencode v3.0.0")
        self.assertEqual(len(newer), 1)
        self.assertTrue(newer[0].startswith("OpenCode 3.0.0 is newer than 2.2.5"))
        self.assertIn("docs/validation.md", newer[0])
        self.assertIn("Claude Code 2.2.0", version_warnings("claude", "2.2.0 (Claude Code)")[0])
        # A reviewer CLI added later, with no checked version yet, gets no warning.
        self.assertEqual(version_warnings("a-new-cli", "9.9.9"), [])

    def test_review_timeout_covers_the_work_before_the_reviewer(self):
        # Two CLI probes (version, then the subcommand's help) and the git call that
        # describes the workspace run before the reviewer, in the same Bash call.
        self.assertEqual(REVIEW_SETUP_SECONDS, 2 * PROBE_SECONDS + GIT_FACT_SECONDS)
        self.assertEqual((PROBE_SECONDS, GIT_FACT_SECONDS), (15, 10))
        self.assertEqual(review_timeout_ms(900), 1000000)
        from devflow_core.common import reviewers
        from devflow_core.common.process import Completed
        from devflow_core.implement import workspace
        # What the probes and the git call actually wait for.
        waited = []

        def bounded(argv, **options):
            waited.append(options["timeout"])
            text = " ".join(reviewers.CLIS["codex"].flags)
            return Completed(0, f"codex 1.0.0 {text}".encode(), b"", False, False, False, None, 0.0)
        with patch.object(reviewers, "run_bounded", bounded), patch.object(reviewers, "_resolve", lambda prefix: prefix):
            reviewers.preflight({"id": "codex", "kind": "codex", "command": ["codex"], "model": "m", "effort": "e",
                                 "timeout_seconds": 10})
        self.assertEqual(waited, [PROBE_SECONDS, PROBE_SECONDS])
        with patch.object(workspace, "run_git", return_value=b"main\n") as git:
            self.assertEqual(workspace.branch(Path("."), "git"), "main")
        self.assertEqual(git.call_args.kwargs["timeout"], GIT_FACT_SECONDS)

    def test_released_run_formats_have_names_and_unreleased_ones_are_generic(self):
        self.assertEqual(runs.RELEASES, {1: "0.1.x", 2: "0.2.x", 3: "0.3.x"})
        for version, release in runs.RELEASES.items():
            with self.subTest(version=version):
                self.assertEqual(runs.release_name(version), release)
        # Formats 4 and 5 were never released (0.4.0 development); they are named generically.
        self.assertGreater(runs.SCHEMA_VERSION, 5)
        self.assertEqual(runs.release_name(4), "an older version (run format 4)")
        self.assertEqual(runs.release_name(5), "an older version (run format 5)")
        self.assertEqual(runs.release_name(9), "an older version (run format 9)")


if __name__ == "__main__":
    unittest.main()
