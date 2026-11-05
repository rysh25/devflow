from __future__ import annotations

import hashlib
import json
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from devflow_core.common.git import content_timeout
from devflow_core.implement.snapshot import between, capture, changes, diff, packet_changes, path_in_scope, validate_relative
from devflow_core.implement import snapshot as snapshot_module
from devflow_core.common.errors import DevFlowError

GIT = {"snapshot": {"mode": "git"}}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="devflow-implement-snapshot-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.project = self.base / "project with spaces 日本語"
        self.project.mkdir()
        # The run directory: start-time copies live in its baseline/ directory.
        self.run_dir = self.base / "run"
        self.run_dir.mkdir()
        self.config = {"snapshot": {"mode": "files"}}

    def write(self, name, content):
        path = self.project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        return path

    def snapshot(self, **settings):
        return capture(self.project, {"snapshot": {"mode": "files", **settings}})

    def start(self, config=None):
        """The start of a run: the listing, with copies of the text Git does not hold."""
        return capture(self.project, config or self.config, copies=self.run_dir / "baseline")

    def checkpoint(self, baseline, config=None):
        """A checkpoint: its change record and its diff."""
        current = capture(self.project, config or self.config)
        record = changes(baseline, current)
        return record, diff(self.project, self.run_dir, baseline, current, record)

    def copies(self):
        directory = self.run_dir / "baseline"
        return sorted(path.name for path in directory.iterdir()) if directory.exists() else []

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

    def index_id(self, name):
        return self.git("ls-files", "--stage", "--", name).split()[1]

    def test_deterministic_id_and_no_contents(self):
        self.write("src/main.py", "print('こんにちは')\n")
        first = self.snapshot()
        second = self.snapshot()
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        record = first["files"]["src/main.py"]
        self.assertEqual(record, {"sha256": sha("print('こんにちは')\n".encode()), "size": len("print('こんにちは')\n".encode()),
                                  "mode": "100644" if os.name == "nt" else record["mode"], "binary": False})
        self.assertNotIn("こんにちは", json.dumps(first, ensure_ascii=False))
        self.assertIsNone(first["git"])

    def test_added_deleted_modified_without_contents(self):
        self.write("modify.txt", "old\n")
        self.write("delete.txt", "deleted")
        self.write("keep.txt", "unchanged")
        baseline = self.start()
        self.write("modify.txt", "new\n")
        self.write("added.txt", "added")
        (self.project / "delete.txt").unlink()
        record, text = self.checkpoint(baseline)
        by_path = {item["path"]: item for item in record["changes"]}
        self.assertEqual({key: value["change"] for key, value in by_path.items()}, {"added.txt": "added", "delete.txt": "deleted", "modify.txt": "modified"})
        self.assertEqual(set(by_path["modify.txt"]["before"]), {"mode", "size", "sha256", "binary"})
        self.assertIsNone(by_path["added.txt"]["before"])
        self.assertIsNone(by_path["delete.txt"]["after"])
        self.assertNotIn("new", json.dumps(record))
        self.assertIn("-old\n+new\n", text)
        self.assertEqual(record["unchanged_count"], 1)

    def test_binary_change_flagged_without_contents(self):
        self.write("image.png", b"\x89PNG\0a")
        baseline = self.start()
        self.assertNotIn("source", baseline["files"]["image.png"])  # A binary diff never shows contents.
        self.assertEqual(self.copies(), [])
        self.write("image.png", b"\x89PNG\0b")
        record, text = self.checkpoint(baseline)
        self.assertEqual(record["binary_changes"], ["image.png"])
        self.assertTrue(record["changes"][0]["after"]["binary"])
        self.assertEqual(text, "diff --git a/image.png b/image.png\nBinary files a/image.png and b/image.png differ\n")

    def test_invalid_utf8_and_a_split_character_are_detected_while_reading(self):
        self.write("latin1.txt", "café".encode("latin-1"))
        # A multi-byte character across the 64 KiB read boundary is still text.
        self.write("boundary.txt", "a" * (snapshot_module.CHUNK - 1) + "é")
        files = self.snapshot()["files"]
        self.assertTrue(files["latin1.txt"]["binary"])
        self.assertFalse(files["boundary.txt"]["binary"])

    def test_missing_final_newline_is_explicit_in_diff(self):
        self.write("app.txt", "old")
        baseline = self.start()
        self.write("app.txt", "new")
        _, text = self.checkpoint(baseline)
        self.assertIn("-old\n\\ No newline at end of file\n+new\n", text)

    def test_diff_file_headers_match_git_for_every_kind_of_change(self):
        """Every change appears with Git's header lines, even when its body is empty."""
        self.init_git()
        self.write("modified.txt", "old\n")
        self.write("emptied-later.txt", "")
        self.write("image.bin", b"\x89PNG\0a")
        self.write("tool.sh", "echo hi\n")
        self.write("removed.txt", "bye\n")
        self.commit_all()
        baseline = self.start(GIT)
        self.write("modified.txt", "new\n")
        self.write("added-empty.txt", "")
        self.write("added.txt", "hello\n")
        (self.project / "emptied-later.txt").unlink()
        self.write("image.bin", b"\x89PNG\0b")
        (self.project / "removed.txt").unlink()
        modes = os.name != "nt"
        if modes:
            (self.project / "tool.sh").chmod(0o755)
        record, ours = self.checkpoint(baseline, GIT)
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
        # The packet's change list names each change without its contents (format 2).
        listed = {item["path"]: item for item in packet_changes(record)}
        self.assertEqual(listed["added.txt"], {"path": "added.txt", "change": "added", "binary": False, "before": None,
                                               "after": {"mode": "100644", "size": 6, "sha256": sha(b"hello\n")}})
        self.assertTrue(listed["image.bin"]["binary"])
        self.assertNotIn("hello", repr(packet_changes(record)))

    def test_diff_file_starts_at_the_baseline_and_stays_in_scope(self):
        self.init_git()
        self.write("app.py", "VALUE = 0\n")
        self.commit_all()
        self.write("app.py", "VALUE = 'before the run'\n")  # An uncommitted change made before the run.
        config = {"snapshot": {"mode": "git", "exclude": ["notes/"]}}
        baseline = self.start(config)
        self.write("app.py", "VALUE = 1\n")
        self.write(".env", "TOKEN=secret\n")
        self.write("notes/private.txt", "excluded\n")
        _, ours = self.checkpoint(baseline, config)
        self.assertIn("-VALUE = 'before the run'\n+VALUE = 1\n", ours)
        self.assertNotIn("VALUE = 0", ours)
        self.assertNotIn(".env", ours)
        self.assertNotIn("TOKEN", ours)
        self.assertNotIn("private.txt", ours)

    def test_concurrent_change_detected_before_snapshot_finishes(self):
        self.write("a.txt", "before")
        original = snapshot_module._read_file
        def racing_read(path, git_mode):
            result = original(path, git_mode)
            path.write_text("changed while capturing")
            return result
        with mock.patch.object(snapshot_module, "_read_file", side_effect=racing_read):
            with self.assertRaises(DevFlowError):
                self.snapshot()

    def test_concurrent_added_file_detected(self):
        self.write("a.txt", "before")
        original = snapshot_module._read_file
        def racing_read(path, git_mode):
            result = original(path, git_mode)
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
        result = self.start()
        self.assertEqual(list(result["files"]), ["src/main.py"])
        self.assertGreaterEqual(len(result["excluded"]), 7)
        self.assertNotIn("SECRET", str(result))
        self.assertEqual(self.copies(), [sha(b"pass")])  # Excluded files are never copied.

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

    def test_large_files_and_many_files_never_stop_and_hold_no_contents(self):
        self.init_git()
        large = ("line of the large tracked file\n" * 70_000).encode()  # About 2 MB.
        self.assertGreater(len(large), 2_000_000)
        self.write("large.txt", large)
        for index in range(6000):
            self.write(f"many/{index // 100:02}/file-{index}.txt", f"file {index}\n")
        self.commit_all()
        baseline = self.start(GIT)
        self.assertEqual((baseline["file_count"], baseline["total_bytes"] >= len(large)), (6001, True))
        self.assertEqual(self.copies(), [])  # Git holds every byte.
        self.assertNotIn("line of the large", json.dumps(baseline))

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
        self.assertEqual(changes(first, second)["changes"][0]["after"]["mode"], "100755")

    def test_nested_repository_rejected(self):
        (self.project / "nested" / ".git").mkdir(parents=True)
        self.write("nested/app.py", "pass")
        with self.assertRaises(DevFlowError):
            self.snapshot()

    def test_git_requires_exact_root(self):
        self.init_git()
        (self.project / "child").mkdir()
        with self.assertRaises(DevFlowError):
            capture(self.project / "child", GIT)

    def test_git_unborn_head_supported_and_no_silent_files_fallback(self):
        with self.assertRaises(DevFlowError):
            capture(self.project, GIT)
        self.init_git()
        self.write("new.py", "pass")
        result = capture(self.project, GIT)
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
        result = capture(self.project, GIT)
        self.assertFalse(result["git"]["dirty"])
        self.assertEqual(list(result["files"]), ["app.py"])

    def test_git_baseline_preserves_preexisting_user_change(self):
        self.init_git()
        self.write("app.py", "original\n")
        self.commit_all()
        self.write("app.py", "user change\n")
        before = self.start(GIT)
        self.assertTrue(before["git"]["dirty"])
        # Git does not hold the user's change, so it is kept as a copy.
        self.assertEqual(before["files"]["app.py"]["source"], {"copy": "baseline/" + sha(b"user change\n")})
        self.assertEqual((self.run_dir / "baseline" / sha(b"user change\n")).read_bytes(), b"user change\n")
        self.write("app.py", "user change plus devflow\n")
        _, text = self.checkpoint(before, GIT)
        self.assertIn("-user change\n+user change plus devflow\n", text)
        self.assertNotIn("original", text)

    def test_git_held_text_is_named_by_its_index_id_and_never_copied(self):
        self.init_git()
        self.write("app.py", "committed\n")
        self.commit_all()
        baseline = self.start(GIT)
        self.assertEqual(baseline["files"]["app.py"]["source"], {"git": self.index_id("app.py")})
        self.assertEqual(self.copies(), [])
        self.write("app.py", "changed\n")
        _, text = self.checkpoint(baseline, GIT)
        self.assertIn("-committed\n+changed\n", text)

    def test_untracked_and_changed_text_are_copied_but_binaries_never(self):
        self.init_git()
        self.write("changed.txt", "committed\n")
        self.write("image.bin", b"\0committed")
        self.commit_all()
        self.write("changed.txt", "changed before the run\n")
        self.write("untracked.txt", "new before the run\n")
        self.write("untracked.bin", b"\0new")
        baseline = self.start(GIT)
        self.assertEqual(self.copies(), sorted([sha(b"changed before the run\n"), sha(b"new before the run\n")]))
        self.assertNotIn("source", baseline["files"]["image.bin"])
        self.assertNotIn("source", baseline["files"]["untracked.bin"])

    def test_files_mode_copies_every_text_file_once(self):
        self.write("a.txt", "same\n")
        self.write("b.txt", "same\n")
        self.write("c.txt", "other\n")
        self.write("d.bin", b"\0binary")
        baseline = self.start()
        self.assertEqual(self.copies(), sorted([sha(b"same\n"), sha(b"other\n")]))
        self.assertEqual(baseline["files"]["a.txt"]["source"], baseline["files"]["b.txt"]["source"])

    def test_the_listing_plus_the_changes_gives_the_snapshot_id_and_binary_flags(self):
        self.write("becomes-text.dat", b"\0binary")
        self.write("modify.txt", "old\n")
        self.write("delete.txt", "gone\n")
        baseline = self.start()
        self.write("becomes-text.dat", "now text\n")
        self.write("modify.txt", "new\n")
        (self.project / "delete.txt").unlink()
        self.write("added.txt", "added\n")
        current = capture(self.project, self.config)
        record = changes(baseline, current)
        listing = {path: {key: value[key] for key in ("sha256", "size", "mode", "binary")}
                   for path, value in baseline["files"].items()}
        for change in record["changes"]:
            if change["after"] is None:
                del listing[change["path"]]
            else:
                listing[change["path"]] = change["after"]
        self.assertEqual(snapshot_module.snapshot_id(listing), record["snapshot_id"])
        self.assertEqual(record["snapshot_id"], current["snapshot_id"])
        self.assertEqual({path: value["binary"] for path, value in listing.items()},
                         {path: value["binary"] for path, value in current["files"].items()})
        self.assertIn("becomes-text.dat", record["binary_changes"])
        self.assertEqual({item["path"]: item["binary"] for item in packet_changes(record)}["becomes-text.dat"], True)
        self.assertEqual(set(packet_changes(record)[0]), {"path", "change", "binary", "before", "after"})

    @unittest.skipIf(os.name == "nt", "Windows file names cannot contain a double quote")
    def test_a_name_that_starts_with_a_double_quote_is_taken_literally(self):
        self.init_git()
        self.write('"quoted.txt', "quoted\n")
        self.commit_all()
        baseline = self.start(GIT)
        self.assertEqual(baseline["files"]['"quoted.txt']["source"], {"git": self.index_id('"quoted.txt')})

    def test_replace_refs_never_change_the_start_time_version(self):
        self.init_git()
        self.write("app.txt", "original\n")
        self.commit_all()
        other = subprocess.run(["git", "-C", str(self.project), "hash-object", "-w", "--stdin"], input=b"replaced\n",
                               capture_output=True, check=True).stdout.decode().strip()
        self.git("replace", self.index_id("app.txt"), other)
        baseline = self.start(GIT)
        self.assertEqual(baseline["files"]["app.txt"]["source"], {"git": self.index_id("app.txt")})
        self.write("app.txt", "changed\n")
        _, text = self.checkpoint(baseline, GIT)
        self.assertIn("-original\n+changed\n", text)
        self.assertNotIn("replaced", text)

    def test_content_reads_from_git_wait_by_their_size(self):
        self.init_git()
        large = b"x\n" * 400_000
        self.write("large.txt", large)
        self.write("small.txt", "small\n")
        self.commit_all()
        calls = []
        original = snapshot_module.run_git

        def recorded(root, *args, **options):
            output = original(root, *args, **options)
            calls.append((args, options.get("timeout"), len(output)))
            return output
        with mock.patch.object(snapshot_module, "run_git", side_effect=recorded):
            baseline = self.start(GIT)
            self.write("large.txt", large + b"more\n")
            self.write("small.txt", "changed\n")
            self.checkpoint(baseline, GIT)
        hashing = [call for call in calls if "hash-object" in call[0]]
        self.assertEqual([timeout for _, timeout, _ in hashing], [content_timeout(len(large) + len(b"small\n"))])
        reads = [call for call in calls if "cat-file" in call[0]]
        # One Git process per changed file, each returning only that file.
        self.assertEqual(sorted(timeout for _, timeout, _ in reads), sorted([content_timeout(len(large)), content_timeout(6)]))
        self.assertTrue(all(args[0] == "--no-replace-objects" for args, _, _ in reads))
        self.assertEqual(sorted(size for _, _, size in reads)[0] < 100, True)
        self.assertLess(max(size for _, _, size in reads), len(large) + 100)

    def test_a_start_time_version_pruned_from_git_stops_the_checkpoint(self):
        self.init_git()
        self.write("a.txt", "staged only\n")
        self.git("add", "a.txt")  # In the index, in no commit.
        baseline = self.start(GIT)
        self.assertIn("git", baseline["files"]["a.txt"]["source"])
        self.write("a.txt", "staged again\n")
        self.git("add", "a.txt")
        self.git("-c", "gc.reflogExpire=now", "gc", "--quiet", "--prune=now")
        with self.assertRaisesRegex(DevFlowError, r"^The start-time version of a\.txt is no longer in Git \(it was "
                                                  r"staged again and removed by git gc\); cancel this run and start a new one$"):
            self.checkpoint(baseline, GIT)

    def test_a_changed_copy_is_never_used(self):
        self.write("app.txt", "start\n")
        baseline = self.start()
        (self.run_dir / "baseline" / sha(b"start\n")).write_bytes(b"tampered\n")
        self.write("app.txt", "changed\n")
        with self.assertRaisesRegex(DevFlowError, "start-time version of app.txt does not match the baseline record"):
            self.checkpoint(baseline)

    def test_a_change_between_hashing_and_copying_is_detected(self):
        self.init_git()
        self.write("app.txt", "committed\n")
        self.commit_all()
        self.write("app.txt", "changed before the run\n")
        original = snapshot_module._git_held

        def change_after(*args):
            held = original(*args)
            self.write("app.txt", "changed while copying\n")
            return held
        with mock.patch.object(snapshot_module, "_git_held", side_effect=change_after):
            with self.assertRaisesRegex(DevFlowError, "File changed during snapshot: app.txt"):
                self.start(GIT)

    def test_copies_are_on_disk_before_they_are_published(self):
        self.write("app.txt", "start\n")
        with mock.patch("devflow_core.common.storage.os.fsync", wraps=os.fsync) as fsync:
            self.start()
        fsync.assert_called()

    def test_git_submodule_rejected_without_fetching(self):
        self.init_git()
        self.write("app.txt", "hello")
        self.commit_all()
        head = self.git("rev-parse", "HEAD")
        self.git("update-index", "--add", "--cacheinfo", f"160000,{head},external")
        with self.assertRaises(DevFlowError):
            capture(self.project, GIT)

    def test_git_ignored_untracked_omitted_but_tracked_ignored_file_kept(self):
        self.init_git()
        self.write(".gitignore", "ignored*\n")
        self.write("ignored-tracked.txt", "tracked")
        self.git("add", "--force", "ignored-tracked.txt")
        self.git("add", ".gitignore")
        self.git("commit", "--quiet", "-m", "fixture")
        self.write("ignored-untracked.txt", "not scanned")
        result = capture(self.project, GIT)
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
            result = capture(self.project, GIT)
        self.assertNotIn("personal-notes.txt", result["files"])
        self.assertIn("app.py", result["files"])
        self.assertFalse(result["git"]["dirty"])

    def test_git_rename_reports_source_and_destination(self):
        self.init_git()
        self.write("old.txt", "hello")
        self.commit_all()
        self.git("mv", "old.txt", "new.txt")
        result = capture(self.project, GIT)
        self.assertTrue(result["git"]["dirty"])
        record = result["git"]["status"][0]
        self.assertEqual(record["path"], "new.txt")
        self.assertEqual(record["source"], "old.txt")

    def test_git_deleted_file_is_absent_and_diff_records_deletion(self):
        self.init_git()
        self.write("gone.txt", "hello")
        self.commit_all()
        before = self.start(GIT)
        (self.project / "gone.txt").unlink()
        record, text = self.checkpoint(before, GIT)
        self.assertNotIn("gone.txt", capture(self.project, GIT)["files"])
        self.assertEqual(record["changes"][0]["change"], "deleted")
        self.assertIn("-hello\n", text)


    def test_between_compares_two_checkpoint_records_without_contents(self):
        self.write("kept.txt", "same\n")
        self.write("edited.txt", "one\n")
        self.write("reverted.txt", "start\n")
        self.write("removed.txt", "bye\n")
        baseline = self.start()
        # Checkpoint 1: edited, reverted and removed changed; added is new.
        self.write("edited.txt", "two\n")
        self.write("reverted.txt", "changed\n")
        self.write("added.txt", "new\n")
        first = changes(baseline, self.snapshot())
        # Checkpoint 2: edited again, reverted back to its start, removed deleted.
        self.write("edited.txt", "three\n")
        self.write("reverted.txt", "start\n")
        (self.project / "removed.txt").unlink()
        second = changes(baseline, self.snapshot())
        self.assertEqual(between(first, second), [{"path": "edited.txt", "change": "modified"},
                                                  {"path": "removed.txt", "change": "deleted"},
                                                  {"path": "reverted.txt", "change": "modified"}])
        self.assertEqual(between(second, first), [{"path": "edited.txt", "change": "modified"},
                                                  {"path": "removed.txt", "change": "added"},
                                                  {"path": "reverted.txt", "change": "modified"}])
        self.assertEqual(between(first, first), [])
        # From the start (no changes) to checkpoint 1 is checkpoint 1's own record.
        start = changes(baseline, baseline)
        self.assertEqual(between(start, first), [{"path": change["path"], "change": change["change"]}
                                                 for change in first["changes"]])
        # A mode change alone is a modification, as in changes().
        self.write("kept.txt", "same\n")
        os.chmod(self.project / "kept.txt", 0o755)
        third = changes(baseline, self.snapshot())
        if os.name != "nt":
            self.assertIn({"path": "kept.txt", "change": "modified"}, between(second, third))


if __name__ == "__main__":
    unittest.main()
