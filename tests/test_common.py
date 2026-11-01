"""Portable contract tests for the single shared source of storage/budget logic."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))

from flow_common.storage import (FlowError, atomic_json, checked_text, digest,
    file_lock, json_bytes, no_symlinks, read_json, within, write_new)
from flow_common.review import all_approved, next_reviewer, validate_limit
from flow_common.settings import workflow_overlay
from flow_common.shell import READ_ONLY_COMMANDS, ShellPolicyError, read_only_tokens, shell_tokens


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
        with self.assertRaises(FlowError):
            write_new(path, b"replacement")
        self.assertEqual(path.read_bytes(), b"original")
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_duplicate_keys_and_nonfinite_json_rejected(self):
        path = self.root / "invalid.json"
        for raw in (b'{"a":1,"a":2}', b'{"x":NaN}', b'{"x":Infinity}', b'[]', b'null'):
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                with self.assertRaises(FlowError):
                    read_json(path)
        with self.assertRaises(ValueError):
            json_bytes({"x": float("nan")})

    def test_size_limit_is_checked_before_json_load(self):
        path = self.root / "large.json"
        path.write_bytes(b'{"x": "large"}')
        with self.assertRaises(FlowError):
            read_json(path, 4)

    def test_text_rejects_empty_binary_missing_and_over_budget(self):
        path = self.root / "text.md"
        for raw in (b"  \n", b"hello\0world", b"\xff"):
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                with self.assertRaises(FlowError):
                    checked_text(path)
        path.write_bytes(b"valid")
        with self.assertRaises(FlowError):
            checked_text(path, 2)
        with self.assertRaises(FlowError):
            checked_text(self.root / "missing")
        self.assertEqual(checked_text(path), "valid")

    def test_path_must_be_below_base(self):
        self.assertEqual(within(self.root / "draft" / "plan.md", self.root), self.root / "draft" / "plan.md")
        for path in (self.root, self.root / ".." / "escape"):
            with self.subTest(path=path):
                with self.assertRaises(FlowError):
                    within(path, self.root)

    def test_symlink_components_fail_closed(self):
        source = self.root / "source"
        source.mkdir()
        link = self.root / "alias"
        try:
            link.symlink_to(source, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Creating symlinks requires OS permission")
        with self.assertRaises(FlowError):
            no_symlinks(link / "new.txt")
        with self.assertRaises(FlowError):
            write_new(link / "new.txt", b"not allowed")
        self.assertFalse((source / "new.txt").exists())

    def test_lock_is_exclusive_released_and_never_guessed_stale(self):
        path = self.root / "state.lock"
        with file_lock(path):
            record = read_json(path)
            self.assertIn("pid", record)
            with self.assertRaises(FlowError):
                with file_lock(path):
                    self.fail("Second lock unexpectedly acquired")
        self.assertFalse(path.exists())
        stale = {"token": "historical", "pid": -123, "host": "unknown"}
        write_new(path, json_bytes(stale))
        with self.assertRaises(FlowError):
            with file_lock(path):
                self.fail("Existing lock unexpectedly removed")
        self.assertEqual(read_json(path), stale)


class SharedReviewTests(unittest.TestCase):
    def setUp(self):
        self.reviewers = [{"id": "codex"}, {"id": "opencode"}]

    def test_hard_limit_and_no_boolean_integer_coercion(self):
        for value in (None, True, 1, 7, 6.0, "6"):
            with self.subTest(value=value):
                with self.assertRaises(FlowError):
                    validate_limit(value)
        self.assertEqual(validate_limit(6), 6)

    def test_alternating_schedule_cannot_dispatch_seventh(self):
        self.assertEqual([next_reviewer(self.reviewers, used, 6)["id"] for used in range(6)],
                         ["codex", "opencode", "codex", "opencode", "codex", "opencode"])
        for used in (-1, 6, True, "0"):
            with self.subTest(used=used):
                with self.assertRaises(FlowError):
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
        atomic_json(self.project / "planflow.config.json", {"max_reviews": 2})
        atomic_json(self.project / "buildflow.config.json", {"max_reviews": 2})
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
        for value in ({"unknown": 1}, {"schema_version": True}, {"schema_version": 2},
                      {"schema_version": "1"}, {"plan": []}, {"implement": "other"}):
            with self.subTest(value=value):
                atomic_json(self.path, value)
                with self.assertRaises(FlowError):
                    workflow_overlay(self.project, "plan")

    def test_unknown_workflow_is_rejected_even_without_config(self):
        with self.assertRaises(FlowError):
            workflow_overlay(self.project, "anything")

    def test_symlink_configuration_is_rejected(self):
        target = self.project / "other.json"
        atomic_json(target, {})
        try:
            self.path.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("Creating symlinks requires OS permission")
        with self.assertRaises(FlowError):
            workflow_overlay(self.project, "implement")


class SharedShellTests(unittest.TestCase):
    def test_strict_tokens_reject_operators_even_inside_quotes(self):
        self.assertEqual(shell_tokens("uv run 'a b'"), ["uv", "run", "a b"])
        for command in ("a 'x|y'", "a; b", "a\nb", "a $(b)", "", None, "a 'unclosed"):
            with self.subTest(command=command), self.assertRaises(ShellPolicyError):
                shell_tokens(command)

    def test_read_only_tokens_accept_literal_operators_only_in_single_quotes(self):
        self.assertEqual(read_only_tokens("grep -E 'a|b$' src"), ["grep", "-E", "a|b$", "src"])
        self.assertEqual(read_only_tokens("grep -rn \"it's\" ."), ["grep", "-rn", "it's", "."])
        self.assertIsNone(read_only_tokens("uv run x"))
        self.assertIsNone(read_only_tokens("/usr/bin/grep x"))
        for command in ("grep \"a|b\" .", "grep x \\| sh", "grep \\'x|y\\' .", "cat a > b", "ls ; rm x",
                        "grep \"$(id)\" .", "cat `id`", "find . -exec rm {} +", "rg --pre=sh x"):
            with self.subTest(command=command), self.assertRaises(ShellPolicyError):
                read_only_tokens(command)

    def test_every_listed_dangerous_option_is_rejected(self):
        for name, options in READ_ONLY_COMMANDS.items():
            for option in options:
                with self.subTest(command=name, option=option), self.assertRaises(ShellPolicyError):
                    read_only_tokens(f"{name} . {option} x")


if __name__ == "__main__":
    unittest.main()
