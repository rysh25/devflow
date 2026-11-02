"""The implementation workspace: chosen at start, frozen, and re-verified (real git worktrees)."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from devflow_core import cli, router
from devflow_core.common.errors import DevFlowError
from devflow_core.common.paths import PLUGIN_ROOT, same_path
from devflow_core.common.runs import RunStore
from devflow_core.common.storage import atomic_write_json, read_json
from devflow_core.implement import WORKFLOW, workspace
from devflow_core.implement.engine import Engine

CHECK = "import os, pathlib; print(pathlib.Path(os.getcwd()).resolve())"


@unittest.skipUnless(shutil.which("git"), "Git is not installed")
class WorkspaceFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="devflow workspace ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.project = self.base / "project with spaces"
        self.project.mkdir()
        self.plan = self.base / "plan.md"
        self.plan.write_text("# Feature\n\nImplement VALUE.\n", encoding="utf-8")
        self.git(self.project, "init", "--quiet", "--initial-branch=main")
        for key, value in (("user.name", "DevFlow Tests"), ("user.email", "devflow-tests@example.invalid"),
                           ("commit.gpgsign", "false")):
            self.git(self.project, "config", key, value)
        (self.project / "module.py").write_text("VALUE = 0\n", encoding="utf-8")
        atomic_write_json(self.project / "devflow.config.json", {"schema_version": 1, "implement": {
            "snapshot": {"mode": "git"},
            "checks": [{"id": "where", "argv": [sys.executable, "-c", CHECK], "timeout_seconds": 20}]}})
        self.git(self.project, "add", "--all")
        self.git(self.project, "commit", "--quiet", "-m", "fixture")
        self.worktree = self.base / "feature worktree"
        self.git(self.project, "worktree", "add", "--quiet", "-b", "feature", str(self.worktree))
        self.session = "workspace-session"
        self.engine = Engine(self.project)

    def git(self, root, *args):
        result = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(root), *args],
                                capture_output=True, check=True, shell=False)
        return result.stdout.decode("utf-8").strip()

    def prompt(self, text):
        return router.handle_hook({"hook_event_name": "UserPromptSubmit", "cwd": str(self.project),
                                   "session_id": self.session, "prompt": text}, PLUGIN_ROOT)

    def start(self, workspace_value=None):
        self.prompt(f'/devflow:implement "{self.plan}"')
        options = {"workspace": workspace_value} if workspace_value is not None else {}
        status = router.runner_start(self.project, self.session, "implement", str(self.plan), options)
        self.run_id = status["run_id"]
        self.draft = Path(status["draft_dir"])
        return status

    def checkpoint(self):
        summary = self.draft / "summary.md"
        summary.write_text("Implemented VALUE.\n", encoding="utf-8")
        return self.engine.checkpoint(self.run_id, self.session, summary)


class WorkspaceStartTests(WorkspaceFixture):
    def test_baseline_checkpoint_and_checks_use_the_worktree(self):
        status = self.start(str(self.worktree))
        self.assertTrue(same_path(status["workspace"]["path"], self.worktree))
        self.assertIsNotNone(status["workspace"]["git_dir"])
        # A change in the main checkout is not the implementation.
        (self.project / "module.py").write_text("VALUE = 99\n", encoding="utf-8")
        first = self.checkpoint()
        self.assertEqual(first["candidate"]["snapshot_id"], first["baseline_id"])
        (self.worktree / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
        second = self.checkpoint()
        self.assertNotEqual(second["candidate"]["snapshot_id"], second["baseline_id"])
        checked = self.engine.check(self.run_id, self.session)
        self.assertEqual(checked["status"], "ready_review")
        results = read_json(Path(checked["run_dir"]) / checked["validation"]["path"])["results"]
        self.assertTrue(same_path(results[0]["stdout"].strip(), self.worktree))

    def test_a_relative_workspace_is_resolved_against_the_project(self):
        status = self.start("../feature worktree")
        self.assertTrue(same_path(status["workspace"]["path"], self.worktree))

    def test_the_default_workspace_is_the_project(self):
        status = self.start()
        self.assertTrue(same_path(status["workspace"]["path"], self.project))
        self.assertEqual(read_json(Path(status["run_dir"]) / "request.json")["intent"]["workspace"], None)

    def test_the_cli_passes_the_workspace_option(self):
        self.prompt(f'/devflow:implement "{self.plan}"')
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = cli.main(["--project", str(self.project), "--session", self.session, "implement", "start",
                             "--plan", str(self.plan), "--workspace", str(self.worktree)])
        self.assertEqual(code, 0, out.getvalue())
        self.assertTrue(same_path(json.loads(out.getvalue())["workspace"]["path"], self.worktree))

    def test_invalid_workspaces_are_refused_before_anything_is_created(self):
        unrelated = self.base / "unrelated"
        unrelated.mkdir()
        self.git(unrelated, "init", "--quiet")
        (self.worktree / "sub").mkdir()
        cases = [(str(unrelated), "same repository"), (str(self.worktree / "sub"), "top directory"),
                 (str(self.base / "missing"), "not an existing directory")]
        link = self.base / "link to worktree"
        try:
            link.symlink_to(self.worktree, target_is_directory=True)
            cases.append((str(link), "symlink"))
        except (OSError, NotImplementedError):
            pass
        for value, message in cases:
            with self.subTest(value=value):
                self.prompt(f'/devflow:implement "{self.plan}"')
                with self.assertRaisesRegex(DevFlowError, message):
                    router.runner_start(self.project, self.session, "implement", str(self.plan),
                                        {"workspace": value})
                self.assertFalse(any((self.project / ".devflow" / "runs" / "implement").glob("2*")))

    def test_files_mode_accepts_only_the_project(self):
        with self.assertRaisesRegex(DevFlowError, "files mode"):
            workspace.resolve_workspace(self.project, str(self.worktree), "files")
        self.assertTrue(same_path(workspace.resolve_workspace(self.project, None, "files")["path"],
                                        self.project))

    def test_the_common_directory_matches_from_any_working_directory(self):
        # In the main checkout plain --git-common-dir prints a relative ".git".
        self.assertEqual(self.git(self.project, "rev-parse", "--git-common-dir"), ".git")
        run_dir = self.base / "elsewhere"
        run_dir.mkdir()
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        for cwd in (run_dir, self.project, self.worktree):
            with self.subTest(cwd=cwd):
                os.chdir(cwd)
                value = workspace.resolve_workspace(self.project, str(self.worktree), "git")
                self.assertTrue(same_path(value["git_common_dir"], self.project / ".git"))

    def test_an_old_git_is_a_clear_error(self):
        with patch.object(workspace, "git_version", return_value=(2, 30)), \
                self.assertRaisesRegex(DevFlowError, "Git 2.31 or later"):
            workspace.resolve_workspace(self.project, str(self.worktree), "git")


class WorkspaceVerificationTests(WorkspaceFixture):
    def remove_worktree(self):
        self.git(self.project, "worktree", "remove", "--force", str(self.worktree))

    def test_a_removed_workspace_stops_the_run(self):
        self.start(str(self.worktree))
        self.remove_worktree()
        with self.assertRaisesRegex(DevFlowError, "no longer valid"):
            self.checkpoint()

    def test_a_recreated_workspace_is_detected(self):
        status = self.start(str(self.worktree))
        self.remove_worktree()
        # Occupy recently freed inode numbers so the new directory is unlikely to reuse one.
        for index in range(20):
            (self.base / f"filler-{index}").mkdir()
        self.git(self.project, "worktree", "add", "--quiet", str(self.worktree), "feature")
        if workspace.identity(self.worktree) == status["workspace"]["identity"]:
            self.skipTest("The file system reused the directory's inode number")
        with self.assertRaisesRegex(DevFlowError, "removed and recreated"):
            self.checkpoint()

    def test_the_final_report_records_a_lost_workspace_instead_of_capturing(self):
        self.start(str(self.worktree))
        self.remove_worktree()
        cancelled = self.engine.cancel(self.run_id, self.session, "The worktree is gone")
        report = cancelled["report"]
        self.assertIn("no longer valid", report["workspace_capture_error"])
        self.assertIsNone(report["workspace_snapshot_id_at_report"])
        self.assertIsNone(report["workspace_matches_checkpoint_at_report"])
        self.assertEqual(report["workspace"]["path"], cancelled["workspace"]["path"])

    def test_without_a_stable_inode_the_identity_is_not_compared(self):
        with patch.object(workspace, "identity", return_value=None):
            status = self.start(str(self.worktree))
        self.assertIsNone(status["workspace"]["identity"])
        context = self.prompt("Keep going")["hookSpecificOutput"]["additionalContext"]
        self.assertIn('"identity_verified": false', context)
        # Paths and the git directory are still checked.
        self.assertEqual(self.checkpoint()["status"], "ready_check")
        self.remove_worktree()
        with self.assertRaisesRegex(DevFlowError, "no longer valid"):
            self.checkpoint()


class RewrittenStateTests(WorkspaceFixture):
    """D1: the implementer can rewrite the run state. Every later check compares
    the workspace with the real project, not only with the state."""

    def rewrite_workspace(self, value):
        store = RunStore(self.project, WORKFLOW)
        with store.lock(self.run_id):
            state = store.load(self.run_id)
            state["workspace"] = value
            store.save(state)

    def describe(self, path, mode):
        if mode == "files":
            return {"path": str(path), "git_common_dir": None, "git_dir": None, "identity": workspace.identity(path)}
        _, common, git_dir = workspace._describe(path)
        return {"path": str(path), "git_common_dir": common, "git_dir": git_dir, "identity": workspace.identity(path)}

    def test_git_mode_cannot_be_pointed_at_another_repository(self):
        self.start()
        unrelated = self.base / "unrelated"
        unrelated.mkdir()
        self.git(unrelated, "init", "--quiet")
        (unrelated / "outside.py").write_text("OUTSIDE = 1\n", encoding="utf-8")
        self.rewrite_workspace(self.describe(unrelated, "git"))
        with self.assertRaisesRegex(DevFlowError, "no longer valid.*same repository"):
            self.checkpoint()

    def test_files_mode_cannot_be_pointed_outside_the_project(self):
        atomic_write_json(self.project / "devflow.config.json",
                          {"schema_version": 1, "implement": {"snapshot": {"mode": "files"}}})
        self.start()
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "outside.py").write_text("OUTSIDE = 1\n", encoding="utf-8")
        self.rewrite_workspace(self.describe(outside, "files"))
        with self.assertRaisesRegex(DevFlowError, "no longer valid.*files mode"):
            self.checkpoint()

    def test_another_worktree_of_the_same_repository_is_the_documented_limit(self):
        # Only the repository is checked against the project, not which of its
        # worktrees was chosen; this is outside what DevFlow can enforce (D1).
        self.start()
        self.rewrite_workspace(workspace.resolve_workspace(self.project, str(self.worktree), "git"))
        (self.worktree / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
        result = self.checkpoint()
        self.assertNotEqual(result["candidate"]["snapshot_id"], result["baseline_id"])


class IdentityTests(unittest.TestCase):
    def test_a_zero_inode_means_the_identity_is_unknown(self):
        real = os.stat

        def without_inode(path, *args, **kwargs):
            info = tuple(real(path, *args, **kwargs))
            return os.stat_result((info[0], 0, *info[2:]))

        here = Path(tempfile.gettempdir())
        with patch.object(workspace.os, "stat", side_effect=without_inode):
            self.assertIsNone(workspace.identity(here))
        if real(here).st_ino:
            self.assertEqual(workspace.identity(here), [real(here).st_dev, real(here).st_ino])


class DirtyStartTests(WorkspaceFixture):
    def test_writes_before_the_start_are_caught_by_the_dirty_check(self):
        # S1: nothing records writes before the start; allow_dirty=false asks the human.
        self.prompt(f'/devflow:implement "{self.plan}"')
        (self.project / "module.py").write_text("VALUE = 5\n", encoding="utf-8")
        status = router.runner_start(self.project, self.session, "implement", str(self.plan), {})
        self.assertEqual(status["status"], "awaiting_human")
        self.assertEqual(status["escalation"]["detail"]["kind"], "dirty_worktree")


if __name__ == "__main__":
    unittest.main()
