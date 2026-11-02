"""Portable contract tests for the single shared source of storage/budget logic."""
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]

from devflow_core.common.storage import (DevFlowError, atomic_json, checked_text, digest,
    file_lock, json_bytes, no_symlinks, read_json, within, write_new)
from devflow_core.common.errors import ReviewerError
from devflow_core.common.review import all_approved, check_fields, next_reviewer, review_budget, validate_limit
from devflow_core.common.settings import validate_document, workflow_overlay
from devflow_core.common.environment import bash_limit, bash_timeout_ms, limit_warnings, python_errors
from devflow_core.common.errors import GuardError
from devflow_core.common.guard import cd_checker, check_protected_names, name_key
from devflow_core.common.paths import msys_path
from devflow_core.common.shell import (ALLOWED_SUMMARY, COMMANDS, ShellPolicyError, check_command,
                                       powershell_tokens, shell_tokens, split_list, split_words)
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


PROJECT = "/p"


def allow_project(path: str) -> bool:
    return path in ("/p", "/p/sub")


ALLOWED_COMMANDS = (
    "grep -rn 'a|b' docs | head -20", 'grep -E "foo|bar" .', "grep -rn --include='*.py' foo .", "ls *.md",
    "find . -name '*.py' 2>/dev/null | wc -l", "sort a | uniq -c | sort -rn", "uniq -f 2 -c", "uniq -f2 -d",
    "uniq --all-repeated=separate", "sed -n '10,40p' f", "sed -n '/^def /,/^$/p' f.py", "sed -n '1p; 3p' f",
    "git --no-optional-locks --no-pager status", "git --no-pager --no-optional-locks -C /p log --oneline -5",
    "git --no-optional-locks --no-pager log --format='%h %s' -- src", "date +%F", "date -u +%s", "date -r f +%F",
    # BSD's -v; GNU date only reports an invalid option. Never executed here.
    "date -v +1d +%F",
    "echo --- ; ls", "ls\ncat x", "wc -l < f", "command -v uv", "uv --version", "cd /p/sub && cat x",
    "cat a 2>&1 >/dev/null", "tail -n 5 log.txt &>/dev/null", "diff -u a b", "file -b x", "rg -n --glob '*.md' foo",
    # -O only runs a program in git grep; -S/-G (log) and -e (grep) take the rest of the word as a value.
    "git --no-optional-locks --no-pager log -SFOO", "git --no-optional-locks --no-pager log -GOOD",
    "git --no-optional-locks --no-pager diff -Oorder.txt", "git --no-optional-locks --no-pager grep -eFOO",
    "git --no-optional-locks --no-pager grep --no-ext-grep x",
)
DENIED_COMMANDS = (
    "find . -rm", "find * -name x", "find ^x -name y", "find . -name 'x'*", "find . -name café", "find . -dele\\te",
    "find .\u00a0-delete", "rg foo *", "rg -z foo", "rg -e -- --pre* input", "grep -n foo -- *.py",
    "sort -o out a", "sort --o=f a", "sort --out=f a", "sort -nro f a", "sort -T d a", "sort -o\\ut a",
    "git status", "git --no-optional-locks --no-pager grep --op=echo x",
    "git -c core.pager=sh --no-optional-locks --no-pager log",
    "git --no-optional-locks --no-pager log --output\\=x", "git --no-optional-locks --no-pager log --show-signature",
    "git --no-optional-locks --no-pager log --format=%GG", "git --no-optional-locks --no-pager log --pretty=format:%G?",
    "git --no-optional-locks --no-pager log --format '%GK'", "git --no-optional-locks --no-pager log -- --pre*",
    "git --no-optional-locks --no-pager push", "git --no-optional-locks --no-pager -C /other log",
    # --help opens the manual with man, info or a browser.
    "git --no-optional-locks --no-pager log --help", "git --no-optional-locks --no-pager status --hel",
    "git --no-optional-locks --no-pager grep -O x", "git --no-optional-locks --no-pager grep -lO x",
    "git --no-optional-locks --no-pager grep -nOvim x", "git --no-optional-locks --no-pager grep --open=vim x",
    "git --no-optional-locks --no-pager grep --ext-grep x", "git --no-optional-locks --no-pager grep --ext-grep=always x",
    'git --no-optional-locks --no-pager log "--output\\\n=x"',
    "sed -i 's/a/b/' f", "sed -n '1p' -i f", "sed -n '1p;w out' f", "sed -n '/[/]/p' f", "sed -n -- '1p' f",
    "uniq a b", "uniq -- - -out", "uniq -", "uniq -f 2 out", "uniq -c 2",
    "date 0101", "date -s now", "date --s=now", "date -- 0101", "date -d @0 +%s", "date -f fmt x", "date -t 0",
    "file --co m", "file -z -S x", "file -C", "file -p x", "file --preserve-date x", "diff -l a b", "diff --pag a b",
    "grep --filter='*:sh' x .", "grep ---x", "cat a > b", "cat a >> /dev/null", "cat a >| /dev/null",
    "ls # it's\nfind . -delete", 'grep "$HOME" .', "echo \\$HOME", "echo $(id)", "ls &", "ls |& cat",
    "ls \\\nfoo", 'echo "a\\\nb"', "echo 'a\\\nb'", "echo 'a\\' ; rm -rf x", "echo 'a'\\'' ; rm x",
    'echo "a\\"b" ; rm x', "find . -name x \\; -delete", "cd /tmp", "cd rel", "cd /p/x/..", "cd -P /p", "cd",
    "ls && /usr/bin/grep x", "uv run x && ls", "ls | ", "ls && && ls", "ls ;; ls", "ls 3>/dev/null",
    "cat <<x", "cat <(ls)", "uv --help", "command ls",
)
# A single command outside the table goes to the runner's own (stricter) checks.
RUNNER_CANDIDATES = ("uv run --offline x", "printf x", "printf -v 'a[$(touch m)]' %s x", "env", "xargs grep x",
                     "awk 1 f", "(ls)", "{ ls; }", "| ls", "/usr/bin/grep x", "pwdx")


class SharedShellTests(unittest.TestCase):
    def test_strict_tokens_reject_operators_even_inside_quotes(self):
        self.assertEqual(shell_tokens("uv run 'a b'"), ["uv", "run", "a b"])
        for command in ("a 'x|y'", "a; b", "a\nb", "a $(b)", "", None, "a 'unclosed"):
            with self.subTest(command=command), self.assertRaises(ShellPolicyError):
                shell_tokens(command)

    def test_allowed_commands(self):
        for command in ALLOWED_COMMANDS:
            with self.subTest(command=command):
                self.assertIs(check_command(command, allow_project, PROJECT), True)

    def test_denied_commands(self):
        for command in DENIED_COMMANDS:
            with self.subTest(command=command), self.assertRaises(ShellPolicyError):
                check_command(command, allow_project, PROJECT)

    def test_non_string_commands_are_policy_errors(self):
        for command in (None, 3, "", "x" * 40000):
            with self.subTest(command=command), self.assertRaises(ShellPolicyError) as raised:
                check_command(command, allow_project, PROJECT)
            self.assertEqual(str(raised.exception), "The command must be a bounded nonempty string.")

    def test_single_commands_outside_the_table_go_to_the_runner_checks(self):
        for command in RUNNER_CANDIDATES:
            with self.subTest(command=command):
                self.assertIs(check_command(command, allow_project, PROJECT), False)

    def test_words_follow_bash_quoting_and_escapes(self):
        words = [word.text for word in split_words("echo 'a'\\''b' \"a\\\"b\" \\; \\| \"c\\d\" 'e\\'")[0]]
        self.assertEqual(words, ["echo", "a'b", 'a"b', ";", "|", "c\\d", "e\\"])
        self.assertEqual([[w.text for w in s] for s in split_words("a b|c\nd;e")], [["a", "b"], ["c"], ["d"], ["e"]])
        # Only space and tab separate words, as in bash and zsh.
        self.assertEqual([w.text for w in split_words("ls a\u00a0b")[0]], ["ls", "a\u00a0b"])
        self.assertEqual([w.text for w in split_words("ls 2>/dev/null x < f")[0]], ["ls", "x"])

    def test_every_forbidden_option_and_its_abbreviation_is_rejected(self):
        for name, rule in COMMANDS.items():
            for option in rule.forbidden:
                forms = [option] + ([option[:3]] if option.startswith("--") and len(option) > 3 else [])
                for form in forms:
                    command = (f"{name} --no-optional-locks --no-pager grep {form}" if name == "git"
                               else f"{name} {form} x")
                    with self.subTest(command=command), self.assertRaises(ShellPolicyError):
                        check_command(command, allow_project, PROJECT)
        # A long option that merely shares a prefix stays allowed.
        self.assertTrue(check_command("git --no-optional-locks --no-pager log --oneline", allow_project, PROJECT))
        self.assertTrue(check_command("sort --check a", allow_project, PROJECT))

    def test_messages_are_the_documented_ones(self):
        cases = (("cat a > b", "Output can be redirected only to /dev/null; write files with the Write tool"),
                 ("echo $(id)", "Shell expansions, substitutions, subshells, groups, comments, background jobs and "
                                "line continuations are not allowed"),
                 ("git log", "git needs --no-optional-locks and --no-pager before the subcommand"),
                 ("cd /tmp", "cd is allowed only to an absolute path without '..' inside this project (/p)"),
                 ("ls && awk 1 f", "'awk' is not an allowed command here; run the DevFlow runner alone, without operators"),
                 ("sort -o x a", "sort -o can run programs or write files."),
                 ("rg foo *", "Quote arguments of rg that contain characters other than letters, digits and "
                              "-_./=,:+@%; pass glob patterns quoted, e.g. --include='*.py'"))
        for command, message in cases:
            with self.subTest(command=command), self.assertRaises(ShellPolicyError) as raised:
                check_command(command, allow_project, PROJECT)
            self.assertEqual(str(raised.exception), message)
        self.assertIn("basename, cat, cd, cmp", ALLOWED_SUMMARY)
        self.assertIn("output redirection is only to /dev/null", ALLOWED_SUMMARY)

    def test_protected_names_cannot_be_reached_through_windows_aliases(self):
        self.assertEqual(name_key("DevFlow.Config.JSON. "), "devflow.config.json")
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve() / "project"
            draft = project / ".devflow" / "runs" / "plan" / "r" / "draft"
            draft.mkdir(parents=True)
            for name in ("devflow.config.json::$DATA", "devflow.config.json.", "devflow.config.json ",
                         ".git::$INDEX_ALLOCATION", ".UGREP.", "notes:x.md"):
                with self.subTest(name=name), self.assertRaises(GuardError):
                    check_protected_names(draft / name, project)
            for name in ("notes.md", "config.json"):
                with self.subTest(name=name):
                    check_protected_names(draft / name, project)

    @unittest.skipIf(os.name == "nt", "Windows cannot create a ':' in a directory name")
    def test_a_colon_above_the_project_is_not_a_stream(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve() / "a:b project"
            (project / "src").mkdir(parents=True)
            check_protected_names(project / "src" / "notes.md", project)
            with self.assertRaises(GuardError):
                check_protected_names(project / "src" / "devflow.config.json::$DATA", project)

    def test_msys_paths_become_windows_paths_on_every_os(self):
        for value, expected in (("/c/Users/x", "C:/Users/x"), ("/d", "D:/"), ("/C/", "C:/"), ("/tmp/x", "/tmp/x"),
                                ("C:/x", "C:/x"), ("/cc/x", "/cc/x"), ("rel", "rel")):
            with self.subTest(value=value):
                self.assertEqual(msys_path(value), expected)

    @unittest.skipIf(os.name == "nt", "POSIX symlink layout")
    def test_cd_allows_only_this_project_without_dot_dot(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve() / "p"
            (project / ".git").mkdir(parents=True)
            (project / "nested" / ".git").mkdir(parents=True)
            (project / "subdir").mkdir()
            (project / "nested" / "link").symlink_to(project / "subdir")
            outside = Path(temporary).resolve() / "outside"
            outside.mkdir()
            (project / "out").symlink_to(outside)
            allowed = cd_checker(project)
            self.assertTrue(allowed(str(project)))
            self.assertTrue(allowed(str(project / "subdir")))
            # Python resolves the link before '..' (-> project); bash removes 'link/..' first (-> nested).
            self.assertFalse(allowed(str(project / "nested" / "link" / "..")))
            self.assertFalse(allowed(str(project / "nested")))
            self.assertFalse(allowed(str(project / "out")))
            self.assertFalse(allowed("subdir"))
            self.assertFalse(allowed(str(project / "missing")))

    def test_msys_or_cygwin_python_is_a_doctor_error_and_cd_stays_closed(self):
        self.assertEqual(python_errors("linux"), [])
        self.assertEqual(python_errors("win32"), [])
        for platform in ("msys", "cygwin"):
            with self.subTest(platform=platform):
                self.assertEqual(python_errors(platform, "/usr/bin/python3"),
                                 [f"On Windows, DevFlow needs a native Windows Python; uv selected {platform} "
                                  "Python at /usr/bin/python3"])
        if os.name != "nt":
            # msys_path is applied only for a native Windows Python, so these stay refused.
            allowed = cd_checker(Path("/nonexistent-project"))
            self.assertFalse(allowed("/c/Users/x"))
            with self.assertRaises(ShellPolicyError):
                check_command("git --no-optional-locks --no-pager -C /c/x log", allowed, "/nonexistent-project")

    def test_powershell_tokens_keep_backslashes_and_refuse_specials(self):
        self.assertEqual(powershell_tokens('uv.exe run "C:\\Users\\user@example\\100%\\devflow.py" --x'),
                         ["uv.exe", "run", "C:\\Users\\user@example\\100%\\devflow.py", "--x"])
        self.assertEqual(powershell_tokens("uv 'C:\\a b'"), ["uv", "C:\\a b"])
        for command in ("uv $x", 'uv "a$b"', 'uv "a`b"', "uv 'a''b'", 'uv "a""b"', "uv \u201cx\u201d", "uv --%",
                        "uv ;x", "uv a,b", "uv @x", "uv #x", "uv a\nb", "uv 'open"):
            with self.subTest(command=command), self.assertRaises(ShellPolicyError):
                powershell_tokens(command)

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_allowed_git_status_never_rewrites_the_index(self):
        def repository(root: Path) -> Path:
            root.mkdir()
            env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
            for argv in (["init", "-q"], ["config", "user.email", "t@example.invalid"], ["config", "user.name", "t"]):
                subprocess.run(["git", "-C", str(root), *argv], check=True, env=env)
            (root / "a.txt").write_text("one\n")
            subprocess.run(["git", "-C", str(root), "add", "a.txt"], check=True, env=env)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "a"], check=True, env=env)
            os.utime(root / "a.txt", (1, 1))  # Stale stat data makes a refresh want to rewrite the index.
            return root

        with tempfile.TemporaryDirectory() as temporary:
            env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
            allowed = repository(Path(temporary) / "allowed")
            before = ((allowed / ".git" / "index").read_bytes(), (allowed / ".git" / "index").stat().st_mtime_ns)
            command = "git --no-optional-locks --no-pager status"
            self.assertTrue(check_command(command, allow_project, PROJECT))
            subprocess.run(command.split(), cwd=allowed, check=True, env=env, capture_output=True)
            after = ((allowed / ".git" / "index").read_bytes(), (allowed / ".git" / "index").stat().st_mtime_ns)
            self.assertEqual(before, after)
            if os.name != "nt":
                plain = repository(Path(temporary) / "plain")
                before = (plain / ".git" / "index").read_bytes()
                subprocess.run(["git", "status"], cwd=plain, check=True, env=env, capture_output=True)
                # The reason the option is required: a plain status rewrites the index.
                self.assertNotEqual(before, (plain / ".git" / "index").read_bytes())

    @unittest.skipIf(os.name == "nt" or not shutil.which("sort"), "needs a POSIX sort (Windows sort.exe differs)")
    def test_sort_spills_only_into_tmpdir_and_removes_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tmp, project = root / "tmp", root / "project"
            tmp.mkdir()
            project.mkdir()
            data = "".join(f"{(index * 7919) % 100003:08d}\n" for index in range(200000)).encode()
            env = dict(os.environ, TMPDIR=str(tmp), LC_ALL="C")
            self.assertTrue(check_command("sort -S 1K", allow_project, PROJECT))
            result = subprocess.run(["sort", "-S", "1K"], cwd=project, input=data, env=env, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, b"".join(sorted(data.splitlines(keepends=True))))
            self.assertEqual(list(tmp.iterdir()), [])
            self.assertEqual(list(project.iterdir()), [])
            if sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() != 0:
                # Spilling really goes to TMPDIR: an unwritable one makes the same sort fail.
                tmp.chmod(0o500)
                try:
                    failed = subprocess.run(["sort", "-S", "1K"], cwd=project, input=data, env=env,
                                            capture_output=True)
                finally:
                    tmp.chmod(0o700)
                self.assertNotEqual(failed.returncode, 0)
                self.assertEqual(list(project.iterdir()), [])


class BashAgreementTests(unittest.TestCase):
    """Whatever the guard accepts, bash must split into the same commands and words."""
    ALPHABET = "af-  \t'\"\\;|&#*$(\n"

    def generate(self, rng, structured: bool) -> str:
        """Random characters, or several commands joined by random separators
        (so |, &&, || and newlines meet quotes, escapes and comments)."""
        def noise(low, high):
            return "".join(rng.choice(self.ALPHABET) for _ in range(rng.randint(low, high)))
        if not structured:
            return "f " + noise(1, 14)
        parts = [rng.choice(("f", "f", "a")) + " " + noise(0, 6) for _ in range(rng.randint(2, 4))]
        separators = [rng.choice(("|", "&&", "||", ";", "\n", " && ", " || ", " | ")) for _ in parts[1:]]
        return parts[0] + "".join(sep + part for sep, part in zip(separators, parts[1:]))

    @staticmethod
    def expected_calls(items) -> list:
        """The f calls bash makes: f returns 1 and other names are not found (127),
        so && skips the next pipeline and || runs it; every pipe member runs."""
        pipelines = []
        for separator, words in items:
            if separator == "|":
                pipelines[-1][1].append(words)
            else:
                pipelines.append((separator, [words]))
        calls, status = [], 0
        for operator, members in pipelines:
            if (operator == "&&" and status != 0) or (operator == "||" and status == 0):
                continue
            calls += ["\x1f".join(w.text for w in words[1:]) for words in members if words[0].text == "f"]
            status = 1 if members[-1][0].text == "f" else 127
        return sorted(calls)

    def compare(self, shell: str, prefix: str, count: int, seed: int) -> None:
        rng = random.Random(seed)
        compared = 0
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "calls"
            for index in range(count):
                command = self.generate(rng, index % 2)
                try:
                    expected = self.expected_calls(split_list(command))
                except ShellPolicyError:
                    continue
                log.write_bytes(b"")
                # f records its arguments on fd 3 and fails, so ;, | and || run every part.
                script = prefix + " 3>>" + shlex_quote(str(log)) + "\n" + command
                result = subprocess.run([shell, *self.flags(shell), "-c", script], cwd=temporary,
                                        env={"PATH": "/nonexistent", "LC_ALL": "C"}, capture_output=True, timeout=5)
                if result.returncode == 2 and b"syntax" in result.stderr.lower():
                    continue  # Nothing runs at all.
                actual = sorted(entry for entry in log.read_bytes().decode("utf-8").split("\x1e")[:-1])
                self.assertEqual(actual, expected, repr(command))
                compared += 1
        self.assertGreater(compared, count // 10)

    @staticmethod
    def flags(shell: str) -> list:
        return ["-f"] if os.path.basename(shell).startswith("zsh") else ["--noprofile", "--norc"]

    @unittest.skipIf(os.name == "nt" or not shutil.which("bash"), "bash agreement runs on Linux and macOS")
    def test_bash_agrees_with_split_words(self):
        bash = shutil.which("bash")
        self.compare(bash, "set -f; f(){ local IFS=$'\\x1f'; printf '%s\\x1e' \"$*\" >&3; return 1; }; exec", 2000, 7)

    @unittest.skipIf(os.name == "nt" or not shutil.which("zsh"), "zsh is not installed")
    def test_zsh_agrees_with_split_words(self):
        zsh = shutil.which("zsh")
        self.compare(zsh, "setopt noglob; f(){ local IFS=$'\\x1f'; printf '%s\\x1e' \"$*\" >&3; return 1; }; exec",
                     1000, 11)


def shlex_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


class EnvironmentTests(unittest.TestCase):
    def test_bash_limit_is_the_larger_setting(self):
        self.assertEqual(bash_limit({}), (600000, "unset", "unset"))
        self.assertEqual(bash_limit({"BASH_MAX_TIMEOUT_MS": "960000"})[0], 960000)
        self.assertEqual(bash_limit({"BASH_DEFAULT_TIMEOUT_MS": "1800000"}), (1800000, "unset", "1800000"))
        self.assertEqual(bash_limit({"BASH_MAX_TIMEOUT_MS": "300000", "BASH_DEFAULT_TIMEOUT_MS": "200000"})[0], 300000)
        self.assertEqual(bash_timeout_ms(900), 960000)

    def test_every_older_run_format_has_a_release_name(self):
        self.assertEqual(set(runs.RELEASES), set(range(1, runs.SCHEMA_VERSION)))
        self.assertEqual(runs.release_name(9), "an older version (run format 9)")

    def test_limit_warnings_name_both_settings(self):
        needs = {"review": bash_timeout_ms(900)}
        self.assertEqual(limit_warnings(needs, {"BASH_MAX_TIMEOUT_MS": "960000"}), [])
        self.assertEqual(limit_warnings(needs, {"BASH_DEFAULT_TIMEOUT_MS": "1800000"}), [])
        self.assertEqual(limit_warnings(needs, {}), [
            "Claude Code's Bash limit (600000 ms = max of BASH_MAX_TIMEOUT_MS=unset and BASH_DEFAULT_TIMEOUT_MS=unset) "
            "is shorter than DevFlow needs for review (960000 ms). Set BASH_MAX_TIMEOUT_MS to at least 960000 in "
            "Claude Code settings (env), or lower timeout_seconds."])
        both = limit_warnings(needs, {"BASH_MAX_TIMEOUT_MS": "700000", "BASH_DEFAULT_TIMEOUT_MS": "650000"})
        self.assertEqual(len(both), 1)
        self.assertIn("BASH_MAX_TIMEOUT_MS=700000 and BASH_DEFAULT_TIMEOUT_MS=650000", both[0])


if __name__ == "__main__":
    unittest.main()
