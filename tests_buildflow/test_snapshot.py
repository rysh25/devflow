from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "devflow" / "components" / "buildflow" / "scripts"))
from buildflow_core.snapshot import capture, compare, path_in_scope, validate_relative
from buildflow_core import snapshot as snapshot_module
from buildflow_core.storage import BuildFlowError


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="buildflow-snapshot-test-")
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
        self.git("config", "user.name", "BuildFlow Tests")
        self.git("config", "user.email", "buildflow-tests@example.invalid")
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
        self.assertIsNone(delta["changed_files"][0]["diff"])

    def test_missing_final_newline_is_explicit_in_diff(self):
        self.write("app.txt", "old")
        baseline = self.snapshot()
        self.write("app.txt", "new")
        delta = compare(baseline, self.snapshot())
        self.assertIn("-old\n\\ No newline at end of file\n+new\n", delta["changed_files"][0]["diff"])

    def test_concurrent_change_detected_before_snapshot_finishes(self):
        self.write("a.txt", "before")
        original = snapshot_module._read_file
        def racing_read(path, limit, git_mode):
            result = original(path, limit, git_mode)
            path.write_text("changed while capturing")
            return result
        with mock.patch.object(snapshot_module, "_read_file", side_effect=racing_read):
            with self.assertRaises(BuildFlowError):
                self.snapshot()

    def test_concurrent_added_file_detected(self):
        self.write("a.txt", "before")
        original = snapshot_module._read_file
        def racing_read(path, limit, git_mode):
            result = original(path, limit, git_mode)
            self.write("late.txt", "added while capturing")
            return result
        with mock.patch.object(snapshot_module, "_read_file", side_effect=racing_read):
            with self.assertRaises(BuildFlowError):
                self.snapshot()

    def test_internal_secret_and_cache_paths_not_read(self):
        self.write("src/main.py", "pass")
        hidden = [".devflow/runs/implement/a/state.json", ".devflow/runs/plan/b/interview.md", "devflow.config.json", ".buildflow/runs/c/state.json", ".planflow/runs/b/plan.md", ".env", ".env.production", "nested/secrets/auth.json", "private.key", "node_modules/a/index.js", "Library/cache.bin", ".venv/pyvenv.cfg"]
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
                with self.assertRaises(BuildFlowError):
                    validate_relative(path)

    def test_builtin_exclusions_cannot_be_overridden_by_include(self):
        for path in (".git/config", ".devflow/state", "sub/.DEVFLOW/state", "devflow.config.json", "sub/DEVFLOW.CONFIG.JSON", "SUB/.ENV.local", "src/secrets/passwords.json"):
            self.assertFalse(path_in_scope(path, {"include": ["**"], "exclude": []}))

    def test_context_file_requires_captured_scope(self):
        self.write("app.py", "pass")
        with self.assertRaises(BuildFlowError):
            self.snapshot(context_paths=["missing.py"])
        result = self.snapshot(context_paths=["app.py"])
        self.assertEqual(result["context_paths"], ["app.py"])

    def test_budgets_fail_without_truncation(self):
        self.write("a.txt", "12345")
        self.write("b.txt", "12345")
        for settings in ({"max_files": 1}, {"max_file_bytes": 4}, {"max_total_bytes": 9}):
            with self.subTest(settings=settings):
                with self.assertRaises(BuildFlowError):
                    self.snapshot(**settings)

    def test_case_collisions_rejected_on_case_sensitive_filesystem(self):
        first = self.write("A.txt", "A")
        second = self.write("a.txt", "b")
        if first.samefile(second):
            self.skipTest("Filesystem is case-insensitive")
        with self.assertRaises(BuildFlowError):
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
        with self.assertRaises(BuildFlowError):
            self.snapshot()
        link.unlink()
        link.symlink_to(outside / "secret.txt")
        with self.assertRaises(BuildFlowError):
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
        with self.assertRaises(BuildFlowError):
            self.snapshot()

    def test_git_requires_exact_root(self):
        self.init_git()
        (self.project / "child").mkdir()
        with self.assertRaises(BuildFlowError):
            capture(self.project / "child", {"snapshot": {"mode": "git"}})

    def test_git_unborn_head_supported_and_no_silent_files_fallback(self):
        with self.assertRaises(BuildFlowError):
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
        self.write(".planflow/runs/two/plan.md", "plan")
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
        self.write("app.py", "user change plus buildflow\n")
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
        with self.assertRaises(BuildFlowError):
            capture(self.project, {"snapshot": {"mode": "git"}})


if __name__ == "__main__":
    unittest.main()
