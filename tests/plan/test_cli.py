"""Real runner argument/error/JSON behavior for the plan workflow, without authentication or model calls."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from devflow_core import cli
from devflow_core.common import paths, sessions
from devflow_core.workflows import WORKFLOWS

SCRIPT = paths.RUNNER


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="devflow plan cli ")
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve()
        # Make the intended root explicit; never inherit a parent temp run.
        (self.project / ".git").mkdir()

    def cli(self, *args, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdin", io.StringIO(stdin)), redirect_stdout(out), redirect_stderr(err):
            result = cli.main(["--project", str(self.project), *args])
        return result, out.getvalue(), err.getvalue()

    def payload(self, session="cli-session", **extra):
        return {"hook_event_name": "UserPromptSubmit", "cwd": str(self.project), "session_id": session,
                "transcript_path": str(self.project / "transcript.jsonl"), **extra}

    def start(self):
        code, out, err = self.cli("hook", stdin=json.dumps(self.payload(prompt="/devflow:plan CLI test request")))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        code, out, err = self.cli("--session", "cli-session", "status")
        self.assertEqual(code, 0, err)
        value = json.loads(out)
        self.assertEqual((value["status"], value["owner_matches"], value["run"]["flow"]), ("bound", True, "plan"))
        code, out, err = self.cli("--session", "cli-session", "--run", value["run"]["run_id"], "status")
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def assertJsonError(self, out, error="DevFlowError"):
        value = json.loads(out)
        self.assertEqual(set(value), {"error", "message", "automatic_retry"})
        self.assertEqual(value["error"], error)
        self.assertFalse(value["automatic_retry"])
        return value["message"]

    def test_runtime_error_is_structured_and_nonretryable(self):
        code, out, err = self.cli("plan", "status")
        self.assertEqual(code, 2)
        self.assertIn("No plan run is bound", self.assertJsonError(out))
        self.assertEqual(err, "")

    def test_traversal_run_id_error_is_structured(self):
        for args in (("plan", "status"), ("status",)):
            with self.subTest(args=args):
                code, out, err = self.cli("--run", "../../private", *args)
                self.assertEqual(code, 2)
                self.assertIn("run ID", self.assertJsonError(out))
                self.assertEqual(err, "")

    def test_usage_errors_are_structured_json(self):
        for args in (("plan", "seal"), ("plan", "review", "--model", "another"), ("plan", "unknown-action")):
            with self.subTest(args=args):
                code, out, err = self.cli("--session", "cli-session", *args)
                self.assertEqual(code, 2)
                self.assertJsonError(out, "UsageError")
                self.assertEqual(err, "")
        self.assertFalse((self.project / ".devflow").exists())

    def test_offline_doctor_does_not_launch_any_process(self):
        with patch("subprocess.Popen") as processes, patch("devflow_core.plan.engine.run_review") as reviews, \
                patch("devflow_core.plan.config.shutil.which", return_value=None):
            code, out, err = self.cli("plan", "doctor")
        self.assertEqual(code, 0)
        value = json.loads(out)
        self.assertEqual(value["config"]["max_reviews"], 6)
        self.assertTrue(value["checks_not_performed"])
        self.assertIn("Offline", value["note"])
        self.assertFalse(value["binaries"]["codex"]["found"])
        self.assertIsNone(value["run_id"])
        processes.assert_not_called()
        reviews.assert_not_called()
        self.assertEqual(err, "")
        self.assertFalse((self.project / ".devflow").exists())

    def test_invalid_config_reports_json(self):
        (self.project / "devflow.config.json").write_text('{"max_reviews":7}', encoding="utf-8")
        code, out, err = self.cli("plan", "doctor")
        self.assertEqual(code, 2)
        self.assertIn("hard ceiling", self.assertJsonError(out))

    def test_unrelated_hook_is_inert(self):
        code, out, err = self.cli("hook", stdin=json.dumps(self.payload(prompt="Ordinary unrelated conversation")))
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertFalse((self.project / ".devflow").exists())

    def test_malformed_hook_fails_closed(self):
        code, out, err = self.cli("hook", stdin="not valid JSON")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("DevFlow hook failed", err)

    def test_stop_hook_error_cannot_cause_infinite_retry_loop(self):
        code, out, err = self.cli("hook", stdin=json.dumps({"hook_event_name": "Stop"}))
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("DevFlow hook failed", err)

    def test_hook_start_then_status_and_owner_error(self):
        state = self.start()
        self.assertEqual(state["flow"], "plan")
        self.assertEqual(state["status"], "interview")
        self.assertEqual(state["review_count"], 0)
        code, out, err = self.cli("--session", "cli-session", "plan", "status")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["run_id"], state["run_id"])
        # Reading is open to any session; changing the run requires ownership.
        code, out, err = self.cli("--session", "wrong-session", "--run", state["run_id"], "plan", "status")
        self.assertEqual(code, 0, out)
        code, out, err = self.cli("--session", "wrong-session", "--run", state["run_id"], "plan", "cancel")
        self.assertEqual(code, 2)
        self.assertIn("does not own", self.assertJsonError(out))

    def test_explicit_cross_session_resume_refuses_unsealed_interview(self):
        state = self.start()
        prompt = "/devflow:resume " + state["run_id"]
        code, out, err = self.cli("hook", stdin=json.dumps(self.payload(session="new-session", prompt=prompt)))
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("original Claude session", err)
        self.assertIsNone(sessions.read_record(self.project, "new-session", WORKFLOWS))
        code, out, err = self.cli("--session", "cli-session", "plan", "status")
        self.assertEqual(json.loads(out)["owner_session"], "cli-session")

    def test_real_entrypoint_plan_help_and_missing_run_json(self):
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), "--project", str(self.project), "plan", "--help"],
                                cwd=str(self.project), capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: devflow plan", result.stdout)
        for action in ("seal", "review", "resolve", "recover"):
            self.assertIn(action, result.stdout)
        self.assertNotIn("resume", result.stdout)
        self.assertEqual(result.stderr, "")
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), "--project", str(self.project), "plan", "status"],
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["error"], "DevFlowError")
        self.assertEqual(result.stderr, "")
        self.assertFalse((self.project / ".devflow").exists())

    def test_resume_is_only_the_explicit_hook_command(self):
        state = self.start()
        # There is no runner resume action; ownership moves only in the hook.
        code, out, err = self.cli("--session", "new-session", "--run", state["run_id"], "plan", "resume")
        self.assertEqual(code, 2)
        self.assertJsonError(out, "UsageError")
        # The resume command needs exactly one run ID.
        for prompt in ("/devflow:resume", "/devflow:resume ../" + state["run_id"]):
            with self.subTest(prompt=prompt):
                code, out, err = self.cli("hook", stdin=json.dumps(self.payload(prompt=prompt)))
                self.assertEqual((code, out), (2, ""))
                self.assertIn("run ID", err)
        code, out, err = self.cli("--session", "cli-session", "plan", "status")
        value = json.loads(out)
        self.assertEqual(value["owner_session"], "cli-session")
        self.assertEqual(value["status"], "interview")


if __name__ == "__main__":
    unittest.main()
