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

from devflow_core import __version__, cli
from devflow_core.common import notices, paths, sessions
from devflow_core.common.errors import ReviewerError
from devflow_core.workflows import WORKFLOWS

SCRIPT = notices.runner_path(paths.PLUGIN_ROOT)


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
            result = cli.main(["--host", "claude", "--project", str(self.project), *args])
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
        self.assertEqual(set(value), {"error", "message", "automatic_retry", "plugin", "version"})
        self.assertEqual(value["error"], error)
        self.assertFalse(value["automatic_retry"])
        self.assertEqual(value["plugin"], "devflow")
        self.assertEqual(value["version"], __version__)
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

    def test_plan_doctor_checks_each_reviewer_in_the_project_without_a_model_or_a_review(self):
        checked = []

        def check(provider, cwd):
            checked.append((provider["id"], cwd))
            return {"kind": provider["kind"], "version_warnings": []}
        with patch("devflow_core.common.reviewers.preflight", side_effect=check), \
                patch("devflow_core.plan.engine.run_review") as reviews, \
                patch("devflow_core.plan.config.shutil.which", return_value=None):
            code, out, err = self.cli("plan", "doctor")
        self.assertEqual(code, 0)
        value = json.loads(out)
        self.assertEqual(value["config"]["max_reviews"], 6)
        self.assertTrue(value["checks_not_performed"])
        self.assertIn("Normally no model calls", value["note"])
        self.assertFalse(value["binaries"]["codex"]["found"])
        self.assertIsNone(value["run_id"])
        ids = [provider["id"] for provider in value["config"]["reviewers"]]
        self.assertEqual(checked, [(reviewer, self.project) for reviewer in ids])
        self.assertEqual([(item["id"], item["ready"]) for item in value["reviewers"]], [(i, True) for i in ids])
        self.assertEqual(value["reviewer_cwd"], str(self.project))
        reviews.assert_not_called()
        self.assertEqual(err, "")
        self.assertFalse((self.project / ".devflow").exists())

    def test_plan_doctor_is_not_ready_when_a_reviewer_refuses_the_project(self):
        refuse = []

        def check(provider, cwd):
            if provider["kind"] in refuse:
                raise ReviewerError(f"Devin does not trust the reviewer directory {cwd}")
            return {"kind": provider["kind"]}
        skill = {"configured_name": "grilling", "status": "available", "static_ready": True,
                 "runtime_verified": False, "reason": "Fixture metadata permits invocation"}

        def doctor():
            with patch("devflow_core.common.reviewers.preflight", side_effect=check), \
                    patch("devflow_core.plan.config.inspect_interview_skill", return_value=skill), \
                    patch("devflow_core.plan.config.shutil.which", return_value="/bin/found"):
                code, out, err = self.cli("plan", "doctor")
            self.assertEqual(code, 0)
            return json.loads(out)
        # Every other prerequisite is ready, so the reviewer result alone decides.
        self.assertTrue(doctor()["ready"])
        refuse.append("codex")
        value = doctor()
        self.assertFalse(value["ready"])
        refused = [item for item in value["reviewers"] if not item["ready"]]
        self.assertEqual([item["id"] for item in refused], ["codex"])
        self.assertIn("does not trust the reviewer directory", refused[0]["error"])

    def test_invalid_config_reports_json(self):
        (self.project / "devflow.config.json").write_text('{"max_reviews":100}', encoding="utf-8")
        code, out, err = self.cli("plan", "doctor")
        self.assertEqual(code, 2)
        self.assertIn("from 1 to 99", self.assertJsonError(out))

    def test_successful_results_carry_runner_fields(self):
        state = self.start()
        code, out, err = self.cli("--session", "cli-session", "status")
        self.assertEqual(code, 0, err)
        value = json.loads(out)
        self.assertEqual((value["plugin"], value["version"]), ("devflow", __version__))
        self.assertEqual(value["status"], "bound")
        code, out, err = self.cli("--session", "cli-session", "--run", state["run_id"], "status")
        self.assertEqual(code, 0, err)
        value = json.loads(out)
        self.assertEqual((value["plugin"], value["version"]), ("devflow", __version__))
        self.assertEqual(value["run_id"], state["run_id"])
        code, out, err = self.cli("--session", "cli-session", "--run", state["run_id"], "doctor")
        self.assertEqual(code, 0, err)
        value = json.loads(out)
        self.assertEqual((value["plugin"], value["version"]), ("devflow", __version__))
        self.assertIn("ready", value)
        self.assertEqual(value["run_id"], state["run_id"])

    def flow_doctor(self, prompt, settings):
        """``plan doctor`` of a run started by ``prompt``, both as the host calls it (the session's
        binding) and with ``--run``; the two reports must agree."""
        (self.project / "devflow.config.json").write_text(json.dumps({"schema_version": 1, **settings}),
                                                          encoding="utf-8")
        self.flow_runs = getattr(self, "flow_runs", 0) + 1
        session = f"flow-{self.flow_runs}"
        code, _, err = self.cli("hook", stdin=json.dumps(self.payload(session=session, prompt=prompt)))
        self.assertEqual(code, 0, err)
        run_id = sessions.read_record(self.project, session, WORKFLOWS, "claude")["run"]["run_id"]
        return lambda after=None: self.flow_reports(session, run_id, after)

    def flow_reports(self, session, run_id, settings=None):
        if settings is not None:
            (self.project / "devflow.config.json").write_text(json.dumps({"schema_version": 1, **settings}),
                                                              encoding="utf-8")
        reports = []
        for args in (("--session", session, "plan", "doctor"), ("--run", run_id, "plan", "doctor")):
            with patch("devflow_core.common.reviewers.preflight", return_value={"probe": "test"}):
                code, out, err = self.cli(*args)
            self.assertEqual((code, err), (0, ""), out)
            reports.append(json.loads(out))
        self.assertEqual(reports[0].get("flow_warnings"), reports[1].get("flow_warnings"))
        return reports[0].get("flow_warnings")

    def test_plan_doctor_of_a_flow_run_warns_where_the_flow_will_wait_or_stop(self):
        checks = [{"id": "verify", "argv": [sys.executable, "-c", "pass"], "timeout_seconds": 10}]
        quiet = {"review_limit_auto": "continue_accept", "implement": {"checks": checks}}
        reports = self.flow_doctor("/devflow:flow -- request", quiet)
        self.assertEqual(reports(), [])
        self.assertEqual(reports({}), [
            "Implement has no checks configured: every checkpoint will wait for human validation.",
            'Implement review_limit_auto is "ask": the Implement review limit waits for the human.'])
        broken = reports({"review_limit_auto": "continue_accept", "implement": {"checks": "not a list"}})
        self.assertEqual(len(broken), 1)
        self.assertTrue(broken[0].startswith("Implement settings cannot be read ("), broken)
        self.assertTrue(broken[0].endswith("the chained Implement start will fail until they are fixed."))
        # Settings that load but fail Implement doctor's own check (an unsafe snapshot pattern) also warn.
        unsafe = reports({**quiet, "implement": {"checks": checks, "snapshot": {"include": ["../outside"]}}})
        self.assertEqual(len(unsafe), 1)
        self.assertTrue(unsafe[0].startswith("Implement settings cannot be read ("), unsafe)
        # Plan's own warnings come from its frozen settings.
        reports = self.flow_doctor("/devflow:flow -- second request",
                                   {"implement": {"checks": checks, "review_limit_auto": "continue_accept"},
                                    "plan": {"publish": {"enabled": False}}})
        self.assertEqual(reports(), [
            'Plan review_limit_auto is "ask": the Plan review limit waits for the human.',
            "Plan publication is disabled: the flow stops after Plan and does not start Implement."])
        # A /devflow:plan run has none.
        self.assertIsNone(self.flow_doctor("/devflow:plan third request", {})())
        # Implement's warnings use the owner host's settings, also when another host inspects the run.
        reports = self.flow_doctor("/devflow:flow -- fourth request",
                                   {"review_limit_auto": "continue_accept",
                                    "hosts": {"claude": {"implement": {"checks": checks}}}})
        self.assertEqual(reports(), [])
        out = io.StringIO()
        with patch("devflow_core.common.reviewers.preflight", return_value={"probe": "test"}), redirect_stdout(out):
            code = cli.main(["--host", "codex", "--project", str(self.project), "--run",
                             sessions.read_record(self.project, f"flow-{self.flow_runs}", WORKFLOWS, "claude")["run"]["run_id"],
                             "plan", "doctor"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(json.loads(out.getvalue())["flow_warnings"], [])

    def test_doctor_carries_runner_fields(self):
        with patch("devflow_core.common.reviewers.preflight", return_value={"probe": "test"}), \
                patch("devflow_core.cli.host_version", return_value={"version": "0.0.0", "supported": True}):
            code, out, err = self.cli("doctor")
        self.assertEqual(code, 0, err)
        value = json.loads(out)
        self.assertEqual((value["plugin"], value["version"]), ("devflow", __version__))
        self.assertIn("ready", value)

    def test_hook_output_has_no_runner_fields(self):
        code, out, err = self.cli("hook", stdin=json.dumps(self.payload(prompt="/devflow:plan CLI test request")))
        self.assertEqual(code, 0, err)
        value = json.loads(out)
        self.assertIn("hookSpecificOutput", value)
        self.assertNotIn("plugin", value)
        self.assertNotIn("version", value)
        code, out, err = self.cli("hook", stdin=json.dumps(self.payload(prompt="Ordinary unrelated conversation")))
        self.assertEqual((code, err), (0, ""))
        if out:
            value = json.loads(out)
            self.assertNotIn("plugin", value)
            self.assertNotIn("version", value)

    def test_unrelated_hook_is_inert(self):
        code, out, err = self.cli("hook", stdin=json.dumps(self.payload(prompt="Ordinary unrelated conversation")))
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertFalse((self.project / ".devflow").exists())

    def test_malformed_hook_exits_1_without_stdout(self):
        # Exit status 2 would make Claude Code block the prompt or tool; 1 only reports.
        code, out, err = self.cli("hook", stdin="not valid JSON")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("DevFlow hook failed", err)

    def test_a_hook_input_missing_its_required_fields_exits_1_never_2(self):
        code, out, err = self.cli("hook", stdin=json.dumps({"hook_event_name": "UserPromptSubmit"}))
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("DevFlow hook failed", err)

    def test_an_option_missing_its_value_does_not_hide_the_command(self):
        # --session has no value; --project takes "hook" and the command is "doctor".
        code, out, err = self.cli("--session", "--project", "hook", "doctor")
        self.assertEqual(code, 2)
        self.assertJsonError(out, "UsageError")
        self.assertEqual(err, "")

    def test_an_option_missing_its_value_keeps_a_hook_a_hook(self):
        # --session has no value; --run takes "status" and the command is "hook". A malformed
        # hook stays on the hook route: stderr and exit 1, never the JSON exit 2 that blocks.
        code, out, err = self.cli("--session", "--run", "status", "hook")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("DevFlow hook failed", err)

    def test_an_invalid_command_before_hook_is_not_a_hook(self):
        # "statsu" is the first positional, so the invocation is not a hook: the versioned
        # JSON usage error goes to stdout with exit status 2, and nothing to stderr.
        code, out, err = self.cli("statsu", "hook")
        self.assertEqual(code, 2)
        value = json.loads(out)
        self.assertEqual((value["error"], value["plugin"], value["version"]),
                         ("UsageError", "devflow", __version__))
        self.assertEqual(err, "")

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

    def assertNotAccepted(self, out, command, reason):
        context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn(f"DevFlow did not accept /devflow:{command}", context)
        self.assertIn(reason, context)

    def test_explicit_cross_session_resume_of_unsealed_interview_is_not_accepted(self):
        state = self.start()
        prompt = "/devflow:resume " + state["run_id"]
        code, out, err = self.cli("hook", stdin=json.dumps(self.payload(session="new-session", prompt=prompt)))
        # The prompt goes through with a context message; it is never blocked.
        self.assertEqual((code, err), (0, ""))
        self.assertNotAccepted(out, "resume", "original Claude Code session")
        self.assertIsNone(sessions.read_record(self.project, "new-session", WORKFLOWS, host="claude"))
        code, out, err = self.cli("--session", "cli-session", "plan", "status")
        self.assertEqual(json.loads(out)["owner_session"], "cli-session")

    def test_real_entrypoint_plan_help_and_missing_run_json(self):
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), "--project", str(self.project), "plan", "--help"],
                                cwd=str(self.project), capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        # argparse versions differ on including global arguments in subcommand usage.
        self.assertRegex(result.stdout, r"usage: devflow(?: .*?)? plan\b")
        for action in ("seal", "review", "resolve", "recover"):
            self.assertIn(action, result.stdout)
        self.assertNotIn("resume", result.stdout)
        self.assertEqual(result.stderr, "")
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), "--host", "claude", "--project", str(self.project), "plan", "status"],
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
                self.assertEqual((code, err), (0, ""))
                self.assertNotAccepted(out, "resume", "run ID")
        code, out, err = self.cli("--session", "cli-session", "plan", "status")
        value = json.loads(out)
        self.assertEqual(value["owner_session"], "cli-session")
        self.assertEqual(value["status"], "interview")


if __name__ == "__main__":
    unittest.main()
