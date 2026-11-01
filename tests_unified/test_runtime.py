"""Unified entry point tests: offline fixtures, no authentication or model calls."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "devflow"
SCRIPT = PLUGIN / "scripts" / "devflow.py"
sys.path.insert(0, str(PLUGIN / "scripts"))

from devflow_core import cli, runtime


class RuntimeFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="devflow unified ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.project = self.base / "project with spaces"
        self.project.mkdir()
        self.session = "unified-user-session"
        self.transcript = self.base / "conversation.jsonl"
        self.plan_file = self.project / "feature.md"
        self.plan_file.write_text("# Feature\nImplement VALUE without changing public behavior.\n", encoding="utf-8")
        self.store = runtime.storage(PLUGIN)
        self.store.atomic_json(self.project / "devflow.config.json", {
            "schema_version": 1, "implement": {"snapshot": {"mode": "files"}},
        })

    def payload(self, event="UserPromptSubmit", session=None, **fields):
        return {"hook_event_name": event, "cwd": str(self.project),
                "session_id": session or self.session, "transcript_path": str(self.transcript), **fields}

    def prompt(self, text, session=None):
        return runtime.handle_hook(self.payload(prompt=text, session=session), PLUGIN)

    def tool(self, name="Write", **data):
        return runtime.handle_hook(self.payload(event="PreToolUse", tool_name=name, tool_input=data), PLUGIN)

    def plan_start(self):
        self.prompt("/devflow:plan REVIEW-10 の計画を作成して")
        binding = runtime.active_binding(self.project, self.session, PLUGIN)
        self.assertEqual(binding["flow"], "plan")
        return binding["run_id"]

    def engine(self, flow):
        return runtime.component(PLUGIN, flow, "engine").Engine(self.project, runtime.component_root(PLUGIN, flow))

    def command(self, action="status", run_id=None):
        args = [*runtime.RUNNER_PREFIX, str(SCRIPT), "--project", str(self.project), "--session", self.session]
        if run_id:
            args += ["--run", run_id]
        return " ".join(shlex.quote(x) for x in args + [action])

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--project", str(self.project), *args])
        return code, out.getvalue(), err.getvalue()

    def raw_run(self, flow, identifier="20261101T123456Z-123456abcdef", status="interview"):
        state = {"schema_version": 1, "flow": flow, "run_id": identifier,
                 "status": status, "project": str(self.project)}
        self.store.atomic_json(runtime.run_path(self.project, flow, identifier) / "state.json", state)
        return identifier

    def assert_denied(self, output):
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny", output)


class UnifiedDispatchTests(RuntimeFixture):
    def test_unrelated_hooks_are_inert_and_create_no_workspace(self):
        self.assertIsNone(self.prompt("Explain this code"))
        self.assertIsNone(self.tool("Bash", command="ordinary user tool"))
        self.assertIsNone(runtime.handle_hook(self.payload(event="Stop"), PLUGIN))
        self.assertIsNone(runtime.handle_hook({"hook_event_name": "UnknownEvent"}, PLUGIN))
        self.assertFalse((self.project / ".devflow").exists())

    def test_plan_start_uses_unified_paths_and_preserves_request(self):
        identifier = self.plan_start()
        run = runtime.run_path(self.project, "plan", identifier)
        request = self.store.read_json(run / "request.json")
        self.assertEqual(request["raw_prompt"], "/devflow:plan REVIEW-10 の計画を作成して")
        self.assertEqual(request["request"], "REVIEW-10 の計画を作成して")
        self.assertFalse((self.project / ".planflow").exists())
        self.assertFalse((self.project / ".devflow" / "runs" / "implement").exists())

    def test_implementation_start_only_captures_intent_until_cli_start(self):
        self.prompt('/devflow:implement "feature.md"')
        binding = runtime.active_binding(self.project, self.session, PLUGIN)
        self.assertEqual(binding["flow"], "implement")
        self.assertTrue(binding["pending"])
        self.assertEqual(binding["record"]["intent"]["args"], "feature.md")
        self.assertFalse((self.project / ".devflow" / "runs").exists())

    def test_active_plan_blocks_implementation_without_recording_it_as_answer(self):
        identifier = self.plan_start()
        before = self.store.read_json(runtime.run_path(self.project, "plan", identifier) / "state.json")
        with self.assertRaises(runtime.DevFlowError):
            self.prompt("/devflow:implement feature.md")
        after = self.store.read_json(runtime.run_path(self.project, "plan", identifier) / "state.json")
        self.assertEqual(before, after)
        self.assertFalse(runtime.session_path(self.project, "implement", self.session).exists())

    def test_one_unfinished_workflow_per_project_across_sessions(self):
        self.plan_start()
        for command in ("/devflow:plan Another request", "/devflow:implement feature.md"):
            with self.subTest(command=command), self.assertRaises(runtime.DevFlowError):
                self.prompt(command, session="second-human-session")

    def test_pending_implementation_blocks_a_different_session(self):
        self.prompt("/devflow:implement feature.md")
        with self.assertRaisesRegex(runtime.DevFlowError, "pending implementation"):
            self.prompt("/devflow:plan Another request", session="other-session")

    def test_new_prompt_can_abandon_pending_implementation_before_plan(self):
        self.prompt("/devflow:implement feature.md")
        identifier = self.plan_start()
        bindings = runtime.bindings(self.project, self.session, PLUGIN)
        self.assertEqual([(x["flow"], x["run_id"]) for x in bindings], [("plan", identifier)])
        stored = self.store.read_json(runtime.session_path(self.project, "implement", self.session))
        self.assertIsNone(stored["intent"])
        self.assertEqual(len(stored["events"]), 1)  # No fabricated event during release.

    def test_terminal_guard_survives_current_turn_then_next_prompt_switches(self):
        identifier = self.plan_start()
        self.engine("plan").cancel(identifier)
        self.assert_denied(self.tool(file_path=str(self.project / "source.py")))
        self.prompt("/devflow:implement feature.md")
        binding = runtime.active_binding(self.project, self.session, PLUGIN)
        self.assertEqual(binding["flow"], "implement")
        self.assertTrue(binding["pending"])
        old = self.store.read_json(runtime.session_path(self.project, "plan", self.session))
        self.assertFalse(old["guard_active"])

    def test_terminal_next_ordinary_prompt_releases_guard_without_new_run(self):
        identifier = self.plan_start()
        self.engine("plan").cancel(identifier)
        self.assertIsNone(self.prompt("Explain what happened"))
        self.assertIsNone(runtime.active_binding(self.project, self.session, PLUGIN))
        self.assertIsNone(self.tool(file_path=str(self.project / "source.py")))

    def test_cancelled_implementation_can_switch_to_plan_on_next_prompt(self):
        self.prompt("/devflow:implement feature.md")
        adapters = runtime.component(PLUGIN, "implement", "adapters")
        with patch.object(adapters, "preflight", return_value={"ready": True}):
            started = self.engine("implement").start(self.session, self.plan_file)
        self.engine("implement").cancel(started["run_id"], self.session, "Human ended this attempt")
        self.assert_denied(self.tool(file_path=str(self.project / "source.py")))
        identifier = self.plan_start()
        bindings = runtime.bindings(self.project, self.session, PLUGIN)
        self.assertEqual([(item["flow"], item["run_id"]) for item in bindings], [("plan", identifier)])
        old = self.store.read_json(runtime.session_path(self.project, "implement", self.session))
        self.assertIsNone(old["active_run"])
        self.assertEqual(len(old["events"]), 1)

    def test_resume_routes_only_to_selected_plan_component(self):
        identifier = self.plan_start()
        plan_hooks = runtime.component(PLUGIN, "plan", "hooks")
        build_hooks = runtime.component(PLUGIN, "implement", "hooks")
        with patch.object(plan_hooks, "handle_hook", wraps=plan_hooks.handle_hook) as plan_call, \
             patch.object(build_hooks, "handle_hook", wraps=build_hooks.handle_hook) as build_call:
            self.prompt("/devflow:resume " + identifier)
        self.assertEqual(plan_call.call_count, 1)
        build_call.assert_not_called()
        self.assertEqual(self.engine("plan").status(identifier)["review_count"], 0)

    def test_resume_routes_only_to_implementation_for_selected_run(self):
        identifier = self.raw_run("implement", status="implementing")
        plan_hooks = runtime.component(PLUGIN, "plan", "hooks")
        build_hooks = runtime.component(PLUGIN, "implement", "hooks")
        with patch.object(plan_hooks, "handle_hook", return_value={}) as plan_call, \
             patch.object(build_hooks, "handle_hook", return_value={}) as build_call:
            self.prompt("/devflow:resume " + identifier)
        self.assertEqual(build_call.call_count, 1)
        plan_call.assert_not_called()

    def test_missing_or_ambiguous_run_id_is_not_guessed(self):
        identifier = self.raw_run("plan")
        self.raw_run("implement", identifier, "implementing")
        with self.assertRaisesRegex(runtime.DevFlowError, "ambiguous"):
            self.prompt("/devflow:resume " + identifier)
        with self.assertRaises(runtime.DevFlowError):
            runtime.resolve_run(self.project, "20261101T123456Z-aaaaaaaaaaaa", PLUGIN)

    def test_invalid_start_or_resume_does_not_create_workspace(self):
        for prompt in ("/devflow:plan", "/devflow:implement", "/devflow:resume ../bad",
                       "/devflow:implement --plan other", "/devflow:plan /other:skill prompt"):
            with self.subTest(prompt=prompt), self.assertRaises(Exception):
                self.prompt(prompt)
            self.assertFalse((self.project / ".devflow").exists())

    def test_stale_dispatch_lock_is_not_deleted_or_ignored(self):
        lock = self.project / ".devflow" / "dispatch.lock"
        self.store.write_new(lock, b'{"token":"existing-lock"}\n')
        with self.assertRaises(Exception):
            self.plan_start()
        self.assertEqual(lock.read_bytes(), b'{"token":"existing-lock"}\n')
        self.assertFalse((self.project / ".devflow" / "runs").exists())

    def test_active_legacy_run_is_not_imported(self):
        identifier = "20261101T123456Z-123456abcdef"
        name = runtime.session_path(self.project, "plan", self.session).name
        old = self.project / ".planflow"
        self.store.atomic_json(old / "sessions" / name, {"run_id": identifier, "guard_active": True})
        state = old / "runs" / identifier / "state.json"
        self.store.atomic_json(state, {"status": "interview"})
        before = state.read_bytes()
        with self.assertRaisesRegex(runtime.DevFlowError, "legacy"):
            self.plan_start()
        self.assertEqual(state.read_bytes(), before)
        self.assertFalse((self.project / ".devflow" / "runs").exists())

    def test_global_status_during_active_run_reports_selected_workflow(self):
        identifier = self.plan_start()
        state = runtime.status(self.project, self.session, None, PLUGIN)
        self.assertEqual((state["flow"], state["run_id"], state["review_count"]), ("plan", identifier, 0))
        self.assertIsNone(self.tool("Bash", command=self.command("status", identifier)))

    def test_idle_status_is_nonmutating_and_pending_status_is_explicit(self):
        value = runtime.status(self.project, self.session, None, PLUGIN)
        self.assertEqual(value["status"], "idle")
        self.assertFalse((self.project / ".devflow").exists())
        self.prompt("/devflow:implement feature.md")
        value = runtime.status(self.project, self.session, None, PLUGIN)
        self.assertEqual(value["status"], "pending_start")
        self.assertEqual(value["pending_intent"]["args"], "feature.md")

    def test_global_runner_exemption_has_exact_context_and_no_shell_escape(self):
        identifier = self.plan_start()
        for action in ("status", "doctor"):
            safe = self.command(action, identifier)
            self.assertIsNone(self.tool("Bash", command=safe))
            for command in (safe + "; echo bad", safe + " | other", safe + " > state.json",
                            safe.replace("--offline ", ""), safe.replace(self.session, "someone-else"),
                            safe.replace(str(SCRIPT), str(self.project / "evil.py")),
                            safe.replace(str(self.project), str(self.base)),
                            safe.replace(identifier, "20261101T123456Z-bbbbbbbbbbbb"),
                            safe + " --extra", safe.replace("--no-python-downloads", "--python 3.11")):
                with self.subTest(command=command):
                    self.assert_denied(self.tool("Bash", command=command))
            self.assert_denied(self.tool("Bash", command=safe, run_in_background=True))
            self.assert_denied(self.tool("Bash", command=safe, dangerouslyDisableSandbox=True))

    def test_global_runner_exemption_does_not_grant_permissions_for_pending_implement(self):
        self.prompt("/devflow:implement feature.md")
        self.assertIsNone(self.tool("PowerShell", command=self.command("status")))
        self.assert_denied(self.tool("Bash", command=self.command("status") + " && other"))
        self.assert_denied(self.tool("Bash", command=self.command("status", "20261101T123456Z-bbbbbbbbbbbb")))

    def test_public_implement_resume_status_first_in_fresh_session(self):
        self.prompt("/devflow:implement feature.md")
        adapters = runtime.component(PLUGIN, "implement", "adapters")
        with patch.object(adapters, "preflight", return_value={"ready": True}):
            state = self.engine("implement").start(self.session, self.plan_file)
        identifier = state["run_id"]
        self.session = "fresh-resume-session"
        self.prompt("/devflow:resume " + identifier)
        binding = runtime.active_binding(self.project, self.session, PLUGIN)
        self.assertTrue(binding["pending"])
        self.assertIsNone(binding["run_id"])
        # Follow the public SKILL exactly: global --run status before resume.
        self.assertIsNone(self.tool("Bash", command=self.command("status", identifier)))
        code, output, error = self.invoke("--session", self.session, "--run", identifier, "status")
        self.assertEqual((code, error), (0, ""), output)
        self.assertEqual(json.loads(output)["flow"], "implement")
        self.assert_denied(self.tool("Bash", command=self.command("status", "20261101T123456Z-bbbbbbbbbbbb")))
        args = [*runtime.RUNNER_PREFIX, str(SCRIPT), "--project", str(self.project),
                "--session", self.session, "--run", identifier, "implement", "resume"]
        allowed = self.tool("Bash", command=" ".join(shlex.quote(x) for x in args))
        self.assertNotEqual((allowed or {}).get("hookSpecificOutput", {}).get("permissionDecision"), "deny")
        code, output, error = self.invoke("--session", self.session, "--run", identifier, "implement", "resume")
        self.assertEqual((code, error), (0, ""), output)
        after = runtime.active_binding(self.project, self.session, PLUGIN)
        self.assertEqual(after["run_id"], identifier)
        self.assertFalse(after["pending"])
        self.assertEqual(json.loads(output)["review_count"], 0)

    def test_pending_resume_does_not_authorize_an_ambiguous_id(self):
        identifier = self.raw_run("implement", status="implementing")
        self.prompt("/devflow:resume " + identifier)
        self.raw_run("plan", identifier, "interview")
        self.assert_denied(self.tool("Bash", command=self.command("status", identifier)))


class UnifiedCliTests(RuntimeFixture):
    def process(self, *args, payload=""):
        return subprocess.run([sys.executable, "-B", str(SCRIPT), "--project", str(self.project), *args],
                              input=payload, capture_output=True, encoding="utf-8", timeout=30, shell=False)

    def test_help_version_and_component_help_are_available(self):
        for args, expected in ((("--version",), "devflow 0.1.3"), (("--help",), "bounded reviews"),
                               (("plan", "--help"), "PlanFlow"), (("implement", "--help"), "bounded independent reviews")):
            with self.subTest(args=args):
                result = self.process(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, result.stdout)

    def test_direct_component_hook_bypass_and_late_globals_are_rejected(self):
        for args in (("plan", "hook"), ("implement", "hook"),
                     ("plan", "--project", str(self.base), "hook"),
                     ("implement", "--session", "other", "start"),
                     ("status", "--run", "arbitrary")):
            with self.subTest(args=args):
                code, output, _ = self.invoke(*args)
                self.assertEqual(code, 2)
                self.assertFalse(json.loads(output)["automatic_retry"])

    def test_global_status_on_idle_project_is_json_and_nonmutating(self):
        code, output, error = self.invoke("status")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(output)["status"], "idle")
        self.assertFalse((self.project / ".devflow").exists())

    def test_real_hook_failure_is_closed_but_stop_does_not_loop(self):
        malformed = self.process("hook", payload="not json")
        self.assertEqual(malformed.returncode, 2)
        self.assertIn("DevFlow hook failed", malformed.stderr)
        stopped = self.process("hook", payload=json.dumps({"hook_event_name": "Stop"}))
        self.assertEqual(stopped.returncode, 0)
        self.assertIn("DevFlow hook failed", stopped.stderr)

    def test_real_unrelated_hook_is_silent(self):
        result = self.process("hook", payload=json.dumps(self.payload(prompt="ordinary conversation")))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertFalse((self.project / ".devflow").exists())

    def test_component_forwarding_creates_implementation_only_from_captured_intent(self):
        self.prompt("/devflow:implement feature.md")
        adapters = runtime.component(PLUGIN, "implement", "adapters")
        with patch.object(adapters, "preflight", return_value={"ready": True}):
            code, output, error = self.invoke("--session", self.session, "implement", "start", "--plan", str(self.plan_file))
        self.assertEqual((code, error), (0, ""), output)
        state = json.loads(output)
        self.assertEqual((state["flow"], state["status"]), ("implement", "implementing"))
        self.assertEqual(Path(state["run_dir"]).parent, self.project / ".devflow" / "runs" / "implement")
        self.assertFalse((self.project / ".devflow" / "runs" / "plan").exists())

    def test_cli_start_cannot_cross_unfinished_project_workflow(self):
        self.plan_start()
        adapters = runtime.component(PLUGIN, "implement", "adapters")
        with patch.object(adapters, "preflight") as probe:
            code, output, _ = self.invoke("--session", "second-session", "implement", "start", "--plan", str(self.plan_file))
        self.assertEqual(code, 2)
        self.assertIn("unfinished plan", output)
        probe.assert_not_called()

    def test_doctor_is_offline_and_reports_minimum_supported_claude(self):
        adapters = runtime.component(PLUGIN, "implement", "adapters")
        interview = runtime.component(PLUGIN, "plan", "interview")
        skill_report = {"configured_name": "grilling", "status": "available", "static_ready": True,
                        "runtime_verified": False, "reason": "Fixture metadata permits invocation"}
        with patch.object(adapters, "preflight", return_value={"offline": True}) as probe, \
             patch.object(interview, "inspect_interview_skill", return_value=skill_report), \
             patch.object(cli, "claude_version", return_value={"supported": True, "version": "2.1.295"}), \
             patch.object(cli.shutil, "which", return_value="/synthetic/available"):
            code, output, error = self.invoke("doctor")
        self.assertEqual((code, error), (0, ""))
        report = json.loads(output)
        self.assertEqual(report["minimum_claude_code"], "2.1.295")
        self.assertTrue(report["ready"])
        self.assertEqual(report["workflows"]["plan"]["interview_skill"], skill_report)
        self.assertEqual(report["workflows"]["plan"]["configuration_source"], "project_settings")
        self.assertFalse(report["python"]["fixed_minor"])
        self.assertEqual(probe.call_count, 2)  # Shared provider probes are cached.
        self.assertFalse((self.project / ".devflow").exists())

    def test_doctor_missing_interview_does_not_block_implementation_readiness(self):
        adapters = runtime.component(PLUGIN, "implement", "adapters")
        interview = runtime.component(PLUGIN, "plan", "interview")
        for status in ("missing", "blocked", "ambiguous", "invalid", "unverified"):
            skill_report = {"configured_name": "grilling", "status": status, "static_ready": False,
                            "runtime_verified": False, "reason": "Fixture " + status}
            with self.subTest(status=status), \
                 patch.object(adapters, "preflight", return_value={"offline": True}), \
                 patch.object(interview, "inspect_interview_skill", return_value=skill_report), \
                 patch.object(cli, "claude_version", return_value={"supported": True, "version": "2.1.295"}), \
                 patch.object(cli.shutil, "which", return_value="/synthetic/available"):
                code, output, error = self.invoke("doctor")
            self.assertEqual((code, error), (0, ""))
            report = json.loads(output)
            self.assertFalse(report["ready"])
            self.assertFalse(report["workflows"]["plan"]["ready"])
            self.assertTrue(report["workflows"]["plan"]["reviewers_ready"])
            self.assertTrue(report["workflows"]["implement"]["ready"])
            self.assertNotIn("interview_skill", report["workflows"]["implement"])

    def test_doctor_old_claude_is_not_reported_ready(self):
        adapters = runtime.component(PLUGIN, "implement", "adapters")
        with patch.object(adapters, "preflight", return_value={"offline": True}), \
             patch.object(cli, "claude_version", return_value={"supported": False, "version": "2.1.294"}):
            code, output, _ = self.invoke("doctor")
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(output)["ready"])

    def test_doctor_missing_uv_is_not_reported_ready(self):
        adapters = runtime.component(PLUGIN, "implement", "adapters")
        with patch.object(adapters, "preflight", return_value={"offline": True}), \
             patch.object(cli, "claude_version", return_value={"supported": True, "version": "2.1.295"}), \
             patch.object(cli.shutil, "which", side_effect=lambda name: None if name == "uv" else "/synthetic/tool"):
            code, output, _ = self.invoke("doctor")
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(output)["ready"])

    def test_claude_version_probe_never_authenticates_or_uses_shell(self):
        result = subprocess.CompletedProcess(["claude", "--version"], 0, "2.1.295 (Claude Code)\n", "")
        with patch.object(cli.shutil, "which", return_value="/safe/claude"), \
             patch.object(cli.subprocess, "run", return_value=result) as execute:
            report = cli.claude_version()
        self.assertTrue(report["supported"])
        self.assertEqual(execute.call_args.args[0], ["/safe/claude", "--version"])
        self.assertFalse(execute.call_args.kwargs["shell"])


if __name__ == "__main__":
    unittest.main()
