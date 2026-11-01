"""Real CLI argument/error/JSON behavior without authentication or model calls."""
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

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "devflow" / "components" / "planflow"
SCRIPT = PLUGIN / "scripts" / "planflow.py"
sys.path.insert(0, str(PLUGIN / "scripts"))

from planflow_core.cli import main


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="planflow cli ")
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve()
        # Make the intended root explicit; never inherit a parent temp run.
        (self.project / ".git").mkdir()

    def cli(self, *args, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdin", io.StringIO(stdin)), redirect_stdout(out), redirect_stderr(err):
            result = main(["--project", str(self.project), *args])
        return result, out.getvalue(), err.getvalue()

    def payload(self, **extra):
        return {"hook_event_name": "UserPromptSubmit", "cwd": str(self.project), "session_id": "cli-session",
                "transcript_path": str(self.project / "transcript.jsonl"), **extra}

    def start(self):
        code, out, err = self.cli("hook", stdin=json.dumps(self.payload(prompt="/devflow:plan CLI test request")))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        code, out, err = self.cli("--session", "cli-session", "status")
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_runtime_error_is_structured_and_nonretryable(self):
        code, out, err = self.cli("status")
        self.assertEqual(code, 2)
        value = json.loads(out)
        self.assertEqual(value["error"], "FlowError")
        self.assertFalse(value["automatic_retry"])
        self.assertIn("No recorded run", value["message"])
        self.assertEqual(err, "")

    def test_traversal_run_id_error_is_structured(self):
        code, out, err = self.cli("--run", "../../private", "status")
        self.assertEqual(code, 2)
        self.assertIn("Invalid run ID", json.loads(out)["message"])
        self.assertEqual(err, "")

    def test_offline_doctor_does_not_launch_any_process(self):
        with patch("subprocess.Popen") as processes, patch("planflow_core.adapters.run_review") as reviews, patch("planflow_core.config.shutil.which", return_value=None):
            code, out, err = self.cli("doctor")
        self.assertEqual(code, 0)
        value = json.loads(out)
        self.assertEqual(value["config"]["max_reviews"], 6)
        self.assertTrue(value["checks_not_performed"])
        self.assertIn("Offline", value["note"])
        self.assertFalse(value["binaries"]["codex"]["found"])
        processes.assert_not_called()
        reviews.assert_not_called()
        self.assertEqual(err, "")
        self.assertFalse((self.project / ".devflow").exists())

    def test_invalid_config_reports_json(self):
        (self.project / "devflow.config.json").write_text('{"max_reviews":7}', encoding="utf-8")
        code, out, err = self.cli("doctor")
        self.assertEqual(code, 2)
        self.assertIn("hard ceiling", json.loads(out)["message"])

    def test_unrelated_hook_is_inert(self):
        code, out, err = self.cli("hook", stdin=json.dumps(self.payload(prompt="Ordinary unrelated conversation")))
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertFalse((self.project / ".devflow").exists())

    def test_malformed_hook_fails_closed(self):
        code, out, err = self.cli("hook", stdin="not valid JSON")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("PlanFlow hook failed", err)

    def test_stop_hook_error_cannot_cause_infinite_retry_loop(self):
        code, out, err = self.cli("hook", stdin=json.dumps({"hook_event_name": "Stop"}))
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("PlanFlow hook failed", err)

    def test_hook_start_then_status_and_owner_error(self):
        state = self.start()
        self.assertEqual(state["status"], "interview")
        self.assertEqual(state["review_count"], 0)
        code, out, err = self.cli("--session", "wrong-session", "--run", state["run_id"], "status")
        self.assertEqual(code, 2)
        self.assertIn("no longer owns", json.loads(out)["message"])

    def test_explicit_cross_session_resume_refuses_unsealed_interview(self):
        state = self.start()
        code, out, err = self.cli("--session", "new-session", "--run", state["run_id"], "resume")
        self.assertEqual(code, 2)
        self.assertIn("original Claude session", json.loads(out)["message"])

    def test_resume_requires_explicit_run_and_session(self):
        self.start()
        code, out, err = self.cli("--session", "cli-session", "resume")
        self.assertEqual(code, 2)
        self.assertIn("requires explicit", json.loads(out)["message"])

    def test_real_entrypoint_help_and_version(self):
        for arguments, expected in ((["--help"], "PlanFlow"), (["--version"], "planflow 0.1.2")):
            with self.subTest(arguments=arguments):
                result = subprocess.run([sys.executable, "-B", str(SCRIPT), *arguments], cwd=str(self.project), capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, result.stdout)
                self.assertEqual(result.stderr, "")
        self.assertFalse((self.project / ".devflow").exists())

    def test_real_entrypoint_missing_run_is_json(self):
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), "--project", str(self.project), "status"], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["error"], "FlowError")
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
