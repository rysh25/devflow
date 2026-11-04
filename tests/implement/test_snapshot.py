from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from devflow_core.implement.snapshot import capture, change_list, compare, diff_text, path_in_scope, validate_relative
from devflow_core.implement import snapshot as snapshot_module
from devflow_core.common.errors import DevFlowError


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="devflow-implement-snapshot-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.project = self.base / "project with spaces 日本語"
        self.project.mkdir()
        self.config = {"snapshot": {"mode": "files"}}

    def write(self, name, content):
        path = self.project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        return path

    def snapshot(self, **settings):
        return capture(self.project, {"snapshot": {"mode": "files", **settings}})

    def git(self, *args):
        result = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(self.project), *args], capture_output=True, check=True, shell=False)
        return result.stdout.decode("utf-8").strip()

    def init_git(self):
        if not shutil.which("git"):
            self.skipTest("Git is not installed")
        self.git("init", "--quiet")
        self.git("config", "user.name", "DevFlow Tests")
        self.git("config", "user.email", "devflow-tests@example.invalid")
        self.git("config", "commit.gpgsign", "false")

    def commit_all(self):
        self.git("add", "--all")
        self.git("commit", "--quiet", "-m", "test fixture")

    def test_deterministic_id_and_utf8_contents(self):
        self.write("src/main.py", "print('こんにちは')\n")
        first = self.snapshot()
        second = self.snapshot()
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(first["files"]["src/main.py"]["text"], "print('こんにちは')\n")
        self.assertIsNone(first["git"])

    def test_added_deleted_modified_full_contents(self):
        self.write("modify.txt", "old\n")
        self.write("delete.txt", "deleted")
        self.write("keep.txt", "unchanged")
        baseline = self.snapshot()
        self.write("modify.txt", "new\n")
        self.write("added.txt", "added")
        (self.project / "delete.txt").unlink()
        delta = compare(baseline, self.snapshot())
        by_path = {item["path"]: item for item in delta["changed_files"]}
        self.assertEqual({key: value["change"] for key, value in by_path.items()}, {"added.txt": "added", "delete.txt": "deleted", "modify.txt": "modified"})
        self.assertEqual(by_path["modify.txt"]["before"]["text"], "old\n")
        self.assertEqual(by_path["modify.txt"]["after"]["text"], "new\n")
        self.assertIn("-old\n+new\n", by_path["modify.txt"]["diff"])
        self.assertEqual(delta["unchanged_count"], 1)

    def test_binary_change_flagged_without_decoding(self):
        self.write("image.png", b"\x89PNG\0a")
        baseline = self.snapshot()
        self.write("image.png", b"\x89PNG\0b")
        current = self.snapshot()
        self.assertIsNone(current["files"]["image.png"]["text"])
        delta = compare(baseline, current)
        self.assertEqual(delta["binary_changes"], ["image.png"])
        self.assertEqual(delta["changed_files"][0]["diff"],
                         "diff --git a/image.png b/image.png\nBinary files a/image.png and b/image.png differ\n")

    def test_missing_final_newline_is_explicit_in_diff(self):
        self.write("app.txt", "old")
        baseline = self.snapshot()
        self.write("app.txt", "new")
        delta = compare(baseline, self.snapshot())
        self.assertIn("-old\n\\ No newline at end of file\n+new\n", delta["changed_files"][0]["diff"])

    def test_diff_file_headers_match_git_for_every_kind_of_change(self):
        """Every change appears with Git's header lines, even when its body is empty."""
        self.init_git()
        self.write("modified.txt", "old\n")
        self.write("emptied-later.txt", "")
        self.write("image.bin", b"\x89PNG\0a")
        self.write("tool.sh", "echo hi\n")
        self.write("removed.txt", "bye\n")
        self.commit_all()
        config = {"snapshot": {"mode": "git"}}
        baseline = capture(self.project, config)
        self.write("modified.txt", "new\n")
        self.write("added-empty.txt", "")
        self.write("added.txt", "hello\n")
        (self.project / "emptied-later.txt").unlink()
        self.write("image.bin", b"\x89PNG\0b")
        (self.project / "removed.txt").unlink()
        modes = os.name != "nt"
        if modes:
            (self.project / "tool.sh").chmod(0o755)
        delta = compare(baseline, capture(self.project, config))
        ours = diff_text(delta)
        self.git("add", "--all")
        theirs = self.git("-c", "core.quotepath=false", "diff", "--cached", "--no-renames", "--no-color") + "\n"
        heads = ("diff --git ", "new file mode ", "deleted file mode ", "old mode ", "new mode ", "--- ", "+++ ",
                 "Binary files ", "@@ ", "+", "-", " ")

        def headers(text):
            return [line for line in text.splitlines() if line.startswith(heads) and not line.startswith("index ")]

        self.assertEqual(headers(ours), headers(theirs))
        for line in ("diff --git a/added-empty.txt b/added-empty.txt\nnew file mode 100644\n",
                     "diff --git a/emptied-later.txt b/emptied-later.txt\ndeleted file mode 100644\n",
                     "Binary files a/image.bin and b/image.bin differ\n", "--- /dev/null\n+++ b/added.txt\n",
                     "--- a/removed.txt\n+++ /dev/null\n"):
            self.assertIn(line, ours)
        if modes:
            self.assertIn("diff --git a/tool.sh b/tool.sh\nold mode 100644\nnew mode 100755\n", ours)
        # The change list names each change without its contents.
        listed = {item["path"]: item for item in change_list(delta)}
        self.assertEqual(listed["added.txt"], {"path": "added.txt", "change": "added", "binary": False, "before": None,
                                               "after": {"mode": "100644", "size": 6, "sha256": listed["added.txt"]["after"]["sha256"]}})
        self.assertTrue(listed["image.bin"]["binary"])
        self.assertNotIn("hello", repr(change_list(delta)))

    def test_diff_file_starts_at_the_baseline_and_stays_in_scope(self):
        self.init_git()
        self.write("app.py", "VALUE = 0\n")
        self.commit_all()
        self.write("app.py", "VALUE = 'before the run'\n")  # An uncommitted change made before the run.
        config = {"snapshot": {"mode": "git", "exclude": ["notes/"]}}
        baseline = capture(self.project, config)
        self.write("app.py", "VALUE = 1\n")
        self.write(".env", "TOKEN=secret\n")
        self.write("notes/private.txt", "excluded\n")
        ours = diff_text(compare(baseline, capture(self.project, config)))
        self.assertIn("-VALUE = 'before the run'\n+VALUE = 1\n", ours)
        self.assertNotIn("VALUE = 0", ours)
        self.assertNotIn(".env", ours)
        self.assertNotIn("TOKEN", ours)
        self.assertNotIn("private.txt", ours)

    def test_concurrent_change_detected_before_snapshot_finishes(self):
        self.write("a.txt", "before")
        original = snapshot_module._read_file
        def racing_read(path, limit, git_mode):
            result = original(path, limit, git_mode)
            path.write_text("changed while capturing")
            return result
        with mock.patch.object(snapshot_module, "_read_file", side_effect=racing_read):
            with self.assertRaises(DevFlowError):
                self.snapshot()

    def test_concurrent_added_file_detected(self):
        self.write("a.txt", "before")
        original = snapshot_module._read_file
        def racing_read(path, limit, git_mode):
            result = original(path, limit, git_mode)
            self.write("late.txt", "added while capturing")
            return result
        with mock.patch.object(snapshot_module, "_read_file", side_effect=racing_read):
            with self.assertRaises(DevFlowError):
                self.snapshot()

    def test_internal_secret_and_cache_paths_not_read(self):
        self.write("src/main.py", "pass")
        hidden = [".devflow/runs/implement/a/state.json", ".devflow/runs/plan/b/interview.md", "devflow.config.json", ".env", ".env.production", "nested/secrets/auth.json", "private.key", "node_modules/a/index.js", "Library/cache.bin", ".venv/pyvenv.cfg"]
        for name in hidden:
            self.write(name, "SECRET")
        result = self.snapshot()
        self.assertEqual(list(result["files"]), ["src/main.py"])
        self.assertGreaterEqual(len(result["excluded"]), 7)
        self.assertNotIn("SECRET", str(result))

    def test_include_and_exclude_globs(self):
        self.write("root.py", "pass")
        self.write("src/use.py", "pass")
        self.write("src/generated/skip.py", "pass")
        self.write("other.txt", "hello")
        result = self.snapshot(include=["**/*.py"], exclude=["src/generated/"])
        self.assertEqual(sorted(result["files"]), ["root.py", "src/use.py"])
        self.assertFalse(path_in_scope("src/generated/skip.py", {"include": ["**"], "exclude": ["src/generated/"]}))

    def test_invalid_paths_rejected(self):
        for path in ("../x", "a/../x", "/tmp/x", "C:/x", "a\\b", "CON", "x/NUL.txt", "a//b", "a/", "trailing.", "newline\nx"):
            with self.subTest(path=path):
                with self.assertRaises(DevFlowError):
                    validate_relative(path)

    def test_builtin_exclusions_cannot_be_overridden_by_include(self):
        for path in (".git/config", ".devflow/state", "sub/.DEVFLOW/state", "devflow.config.json", "sub/DEVFLOW.CONFIG.JSON", "SUB/.ENV.local", "src/secrets/passwords.json"):
            self.assertFalse(path_in_scope(path, {"include": ["**"], "exclude": []}))

    def test_context_paths_must_be_in_the_captured_scope(self):
        self.write("app.py", "pass")
        with self.assertRaises(DevFlowError):
            self.snapshot(context_paths=["missing.py"])
        result = self.snapshot(context_paths=["app.py"])
        self.assertEqual(result["context_paths"], ["app.py"])

    def test_budgets_fail_without_truncation(self):
        self.write("a.txt", "12345")
        self.write("b.txt", "12345")
        for settings in ({"max_files": 1}, {"max_file_bytes": 4}, {"max_total_bytes": 9}):
            with self.subTest(settings=settings):
                with self.assertRaises(DevFlowError):
                    self.snapshot(**settings)

    def test_case_collisions_rejected_on_case_sensitive_filesystem(self):
        first = self.write("A.txt", "A")
        second = self.write("a.txt", "b")
        if first.samefile(second):
            self.skipTest("Filesystem is case-insensitive")
        with self.assertRaises(DevFlowError):
            self.snapshot()

    def test_symlink_file_and_directory_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret")
        link = self.project / "link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Symlink creation unavailable: {exc}")
        with self.assertRaises(DevFlowError):
            self.snapshot()
        link.unlink()
        link.symlink_to(outside / "secret.txt")
        with self.assertRaises(DevFlowError):
            self.snapshot()

    @unittest.skipIf(os.name == "nt", "POSIX mode changes")
    def test_executable_mode_is_part_of_snapshot_identity(self):
        path = self.write("tool.py", "pass\n")
        path.chmod(0o644)
        first = self.snapshot()
        path.chmod(0o755)
        second = self.snapshot()
        self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(compare(first, second)["changed_files"][0]["after"]["mode"], "100755")

    def test_nested_repository_rejected(self):
        (self.project / "nested" / ".git").mkdir(parents=True)
        self.write("nested/app.py", "pass")
        with self.assertRaises(DevFlowError):
            self.snapshot()

    def test_git_requires_exact_root(self):
        self.init_git()
        (self.project / "child").mkdir()
        with self.assertRaises(DevFlowError):
            capture(self.project / "child", {"snapshot": {"mode": "git"}})

    def test_git_unborn_head_supported_and_no_silent_files_fallback(self):
        with self.assertRaises(DevFlowError):
            capture(self.project, {"snapshot": {"mode": "git"}})
        self.init_git()
        self.write("new.py", "pass")
        result = capture(self.project, {"snapshot": {"mode": "git"}})
        self.assertIsNone(result["git"]["head"])
        self.assertTrue(result["git"]["dirty"])

    def test_git_runtime_artifacts_do_not_mark_scoped_tree_dirty(self):
        self.init_git()
        self.write("app.py", "pass")
        self.commit_all()
        self.write(".devflow/runs/implement/one/state.json", "{}")
        self.write(".devflow/runs/plan/two/interview.md", "SECRET")
        self.write("devflow.config.json", "{}")
        self.write(".env", "SECRET=value")
        result = capture(self.project, {"snapshot": {"mode": "git"}})
        self.assertFalse(result["git"]["dirty"])
        self.assertEqual(list(result["files"]), ["app.py"])

    def test_git_baseline_preserves_preexisting_user_change(self):
        self.init_git()
        self.write("app.py", "original\n")
        self.commit_all()
        self.write("app.py", "user change\n")
        before = capture(self.project, {"snapshot": {"mode": "git"}})
        self.assertTrue(before["git"]["dirty"])
        self.write("app.py", "user change plus devflow\n")
        after = capture(self.project, {"snapshot": {"mode": "git"}})
        self.assertEqual(compare(before, after)["changed_files"][0]["before"]["text"], "user change\n")

    def test_git_ignored_untracked_omitted_but_tracked_ignored_file_kept(self):
        self.init_git()
        self.write(".gitignore", "ignored*\n")
        self.write("ignored-tracked.txt", "tracked")
        self.git("add", "--force", "ignored-tracked.txt")
        self.git("add", ".gitignore")
        self.git("commit", "--quiet", "-m", "fixture")
        self.write("ignored-untracked.txt", "not scanned")
        result = capture(self.project, {"snapshot": {"mode": "git"}})
        self.assertIn("ignored-tracked.txt", result["files"])
        self.assertNotIn("ignored-untracked.txt", result["files"])
        self.assertIn("ignored untracked", result["git"]["scope"])

    def test_the_users_own_git_configuration_applies_and_git_dir_does_not_redirect(self):
        """DevFlow's git sees the repository as the user's git does (here a global
        excludes file), and a GIT_DIR in the environment cannot point it elsewhere."""
        self.init_git()
        self.write("app.py", "pass\n")
        self.commit_all()
        excludes = self.base / "global ignore"
        excludes.write_text("personal-notes.txt\n", encoding="utf-8")
        config = self.base / "global gitconfig"
        config.write_text(f"[core]\n\texcludesFile = {excludes.as_posix()}\n", encoding="utf-8")
        self.write("personal-notes.txt", "mine\n")
        other = self.base / "other repository"
        other.mkdir()
        subprocess.run(["git", "init", "--quiet", str(other)], check=True, capture_output=True)
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config), "GIT_DIR": str(other / ".git")}):
            result = capture(self.project, {"snapshot": {"mode": "git"}})
        self.assertNotIn("personal-notes.txt", result["files"])
        self.assertIn("app.py", result["files"])
        self.assertFalse(result["git"]["dirty"])

    def test_git_rename_reports_source_and_destination(self):
        self.init_git()
        self.write("old.txt", "hello")
        self.commit_all()
        self.git("mv", "old.txt", "new.txt")
        result = capture(self.project, {"snapshot": {"mode": "git"}})
        self.assertTrue(result["git"]["dirty"])
        record = result["git"]["status"][0]
        self.assertEqual(record["path"], "new.txt")
        self.assertEqual(record["source"], "old.txt")

    def test_git_deleted_file_is_absent_and_diff_records_deletion(self):
        self.init_git()
        self.write("gone.txt", "hello")
        self.commit_all()
        before = capture(self.project, {"snapshot": {"mode": "git"}})
        (self.project / "gone.txt").unlink()
        after = capture(self.project, {"snapshot": {"mode": "git"}})
        self.assertNotIn("gone.txt", after["files"])
        self.assertEqual(compare(before, after)["changed_files"][0]["change"], "deleted")

    def test_git_submodule_rejected_without_fetching(self):
        self.init_git()
        self.write("app.txt", "hello")
        self.commit_all()
        head = self.git("rev-parse", "HEAD")
        self.git("update-index", "--add", "--cacheinfo", f"160000,{head},external")
        with self.assertRaises(DevFlowError):
            capture(self.project, {"snapshot": {"mode": "git"}})


if __name__ == "__main__":
    unittest.main()
