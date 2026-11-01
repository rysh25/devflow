"""Router and CLI tests: offline fixtures, no authentication or model calls."""
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

from devflow_core import __version__, cli, router as runtime
from devflow_core.common import guard, sessions
from devflow_core.common.errors import DevFlowError
from devflow_core.common.runs import RunStore, make_event
from devflow_core.common.storage import atomic_json, read_json, write_new
from devflow_core.workflows import WORKFLOWS

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "devflow"
SCRIPT = PLUGIN / "scripts" / "devflow.py"
OTHER_ID = "20261101T123456Z-bbbbbbbbbbbb"


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
        atomic_json(self.project / "devflow.config.json", {"schema_version": 1, "implement": {"snapshot": {"mode": "files"}}})
        patcher = patch.object(runtime, "HOOK_WAIT", 0.2)
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- helpers ---------------------------------------------------------------------------
    def payload(self, event="UserPromptSubmit", session=None, **fields):
        return {"hook_event_name": event, "cwd": str(self.project),
                "session_id": session or self.session, "transcript_path": str(self.transcript), **fields}

    def prompt(self, text, session=None):
        return runtime.handle_hook(self.payload(prompt=text, session=session), PLUGIN)

    def tool(self, name="Write", session=None, **data):
        return runtime.handle_hook(self.payload(event="PreToolUse", session=session, tool_name=name, tool_input=data), PLUGIN)

    def record(self, session=None):
        return sessions.read_record(self.project, session or self.session, WORKFLOWS)

    def run_dir(self, flow, run_id):
        return self.project / ".devflow" / "runs" / flow / run_id

    def plan_start(self, session=None):
        self.prompt("/devflow:plan REVIEW-10 の計画を作成して", session=session)
        record = self.record(session)
        self.assertEqual(record["run"]["flow"], "plan")
        return record["run"]["run_id"]

    def implement_start(self, session=None):
        session = session or self.session
        self.prompt("/devflow:implement feature.md", session=session)
        return runtime.runner_start(self.project, session, "implement", "feature.md")["run_id"]

    def engine(self, flow):
        return WORKFLOWS[flow].engine(self.project)

    def command(self, action="status", run_id=None, session=None, flow=None):
        args = [*guard.RUNNER_PREFIX, str(SCRIPT), "--project", str(self.project), "--session", session or self.session]
        if run_id:
            args += ["--run", run_id]
        args += ([flow] if flow else []) + action.split()
        return " ".join(shlex.quote(x) for x in args)

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--project", str(self.project), *args])
        return code, out.getvalue(), err.getvalue()

    def raw_run(self, flow, identifier="20261101T123456Z-123456abcdef", status="interview", **extra):
        """A run directory with only the given state fields (0.1.x-style unless overridden)."""
        state = {"schema_version": 1, "flow": flow, "run_id": identifier, "status": status,
                 "project": str(self.project), **extra}
        atomic_json(self.run_dir(flow, identifier) / "state.json", state)
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
        request = read_json(self.run_dir("plan", identifier) / "request.json")
        self.assertEqual(request["raw_prompt"], "/devflow:plan REVIEW-10 の計画を作成して")
        self.assertEqual(request["request"], "REVIEW-10 の計画を作成して")
        self.assertFalse((self.project / ".devflow" / "runs" / "implement").exists())
        state = read_json(self.run_dir("plan", identifier) / "state.json")
        self.assertEqual((state["schema_version"], state["owner_session"]), (2, self.session))
        self.assertEqual(len(state["event_ids"]), 1)  # The start prompt itself.

    def test_implementation_start_only_captures_pending_start_until_cli_start(self):
        self.prompt('/devflow:implement "feature.md"')
        pending = self.record()["pending_start"]
        self.assertEqual(pending["flow"], "implement")
        self.assertEqual(pending["args"], str(self.plan_file))
        self.assertFalse((self.project / ".devflow" / "runs").exists())

    def test_active_plan_blocks_implementation_without_recording_it_as_answer(self):
        identifier = self.plan_start()
        before = read_json(self.run_dir("plan", identifier) / "state.json")
        with self.assertRaises(DevFlowError):
            self.prompt("/devflow:implement feature.md")
        after = read_json(self.run_dir("plan", identifier) / "state.json")
        self.assertEqual(before, after)
        self.assertEqual(self.record()["run"]["run_id"], identifier)

    def test_one_unfinished_workflow_per_project_across_sessions(self):
        self.plan_start()
        for command in ("/devflow:plan Another request", "/devflow:implement feature.md"):
            with self.subTest(command=command), self.assertRaises(DevFlowError):
                self.prompt(command, session="second-human-session")

    def test_pending_implementation_blocks_a_different_session(self):
        self.prompt("/devflow:implement feature.md")
        for command in ("/devflow:plan Another request", "/devflow:implement feature.md"):
            with self.subTest(command=command), self.assertRaisesRegex(DevFlowError, "pending implement start"):
                self.prompt(command, session="other-session")

    def test_new_prompt_can_abandon_pending_implementation_before_plan(self):
        self.prompt("/devflow:implement feature.md")
        identifier = self.plan_start()
        record = self.record()
        self.assertEqual(record["run"], {"flow": "plan", "run_id": identifier})
        self.assertIsNone(record["pending_start"])
        self.assertFalse((self.project / ".devflow" / "runs" / "implement").exists())

    def test_ordinary_prompt_abandons_pending_start_and_reimplement_replaces_it(self):
        self.prompt("/devflow:implement feature.md")
        self.assertIsNone(self.prompt("Actually, wait."))
        self.assertIsNone(self.record())
        other = self.project / "other.md"
        other.write_text("Other plan.\n", encoding="utf-8")
        self.prompt("/devflow:implement feature.md")
        self.prompt("/devflow:implement other.md")
        self.assertEqual(self.record()["pending_start"]["args"], str(other))

    def test_terminal_guard_survives_current_turn_then_next_prompt_switches(self):
        identifier = self.plan_start()
        self.engine("plan").cancel(identifier)
        self.assert_denied(self.tool(file_path=str(self.project / "source.py")))
        self.prompt("/devflow:implement feature.md")
        record = self.record()
        self.assertIsNone(record["run"])
        self.assertEqual(record["pending_start"]["flow"], "implement")

    def test_terminal_next_ordinary_prompt_releases_guard_without_new_run(self):
        identifier = self.plan_start()
        self.engine("plan").cancel(identifier)
        before = read_json(self.run_dir("plan", identifier) / "state.json")
        self.assertIsNone(self.prompt("Explain what happened"))
        self.assertIsNone(self.record())
        self.assertIsNone(self.tool(file_path=str(self.project / "source.py")))
        # The releasing prompt is not recorded into the finished run.
        self.assertEqual(read_json(self.run_dir("plan", identifier) / "state.json"), before)

    def test_cancelled_implementation_can_switch_to_plan_on_next_prompt(self):
        identifier = self.implement_start()
        self.engine("implement").cancel(identifier, self.session, "Human ended this attempt")
        self.assert_denied(self.tool(file_path=str(self.project / "source.py")))
        plan = self.plan_start()
        self.assertEqual(self.record()["run"], {"flow": "plan", "run_id": plan})

    def test_resume_selects_the_workflow_that_owns_the_run(self):
        plan = self.plan_start()
        result = self.prompt("/devflow:resume " + plan)
        self.assertIn("DevFlow plan is active", result["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.engine("plan").status(plan)["review_count"], 0)
        self.engine("plan").cancel(plan)
        self.prompt("next")
        implement = self.implement_start()
        result = self.prompt("/devflow:resume " + implement, session="fresh-session")
        self.assertIn("DevFlow implement is active", result["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.record("fresh-session")["run"], {"flow": "implement", "run_id": implement})
        self.assertEqual(self.engine("implement").status(implement)["owner_session"], "fresh-session")

    def test_missing_or_ambiguous_run_id_is_not_guessed(self):
        identifier = self.raw_run("plan")
        self.raw_run("implement", identifier, "implementing")
        with self.assertRaisesRegex(DevFlowError, "ambiguous"):
            self.prompt("/devflow:resume " + identifier)
        with self.assertRaisesRegex(DevFlowError, "ambiguous"):
            runtime.status(self.project, None, identifier)
        with self.assertRaises(DevFlowError):
            runtime.resolve_run(self.project, "20261101T123456Z-aaaaaaaaaaaa", runtime.stores(self.project))

    def test_invalid_start_or_resume_does_not_create_workspace(self):
        for prompt in ("/devflow:plan", "/devflow:implement", "/devflow:resume ../bad",
                       "/devflow:implement --plan other", "/devflow:plan /other:skill prompt"):
            with self.subTest(prompt=prompt), self.assertRaises(Exception):
                self.prompt(prompt)
            self.assertFalse((self.project / ".devflow").exists())

    def test_stale_dispatch_lock_is_not_deleted_or_ignored(self):
        lock = self.project / ".devflow" / "dispatch.lock"
        write_new(lock, b'{"token":"existing-lock"}\n')
        with self.assertRaisesRegex(DevFlowError, "locked"):
            self.plan_start()
        self.assertEqual(lock.read_bytes(), b'{"token":"existing-lock"}\n')
        self.assertFalse((self.project / ".devflow" / "runs").exists())

    def test_global_status_during_active_run_reports_selected_workflow(self):
        identifier = self.plan_start()
        value = runtime.status(self.project, self.session, None)
        self.assertEqual((value["status"], value["run"], value["owner_matches"], value["pending_start"]),
                         ("bound", {"flow": "plan", "run_id": identifier}, True, None))
        self.assertNotIn("run_status", value)
        listed = value["runs"][0]
        self.assertEqual((listed["run_id"], listed["status"], listed["review_count"]), (identifier, "interview", 0))
        self.assertIsNone(self.tool("Bash", command=self.command("status", identifier)))

    def test_idle_status_is_nonmutating_and_pending_status_is_explicit(self):
        value = runtime.status(self.project, self.session, None)
        self.assertEqual(value["status"], "idle")
        self.assertFalse((self.project / ".devflow").exists())
        self.prompt("/devflow:implement feature.md")
        value = runtime.status(self.project, self.session, None)
        self.assertEqual(value["status"], "pending_start")
        self.assertEqual(value["pending_start"]["args"], str(self.plan_file))
        self.assertEqual(value["sessions"]["pending_starts"], [])  # Not blocking its own session.
        other = runtime.status(self.project, "someone-else", None)
        self.assertEqual(other["sessions"]["pending_starts"][0]["flow"], "implement")

    def test_global_runner_exemption_has_exact_context_and_no_shell_escape(self):
        identifier = self.plan_start()
        for action in ("status", "doctor"):
            safe = self.command(action, identifier)
            self.assertIsNone(self.tool("Bash", command=safe))
            for command in (safe + "; echo bad", safe + " | other", safe + " > state.json",
                            safe.replace("--offline ", ""), safe.replace(self.session, "someone-else"),
                            safe.replace(shlex.quote(str(SCRIPT)), shlex.quote(str(self.project / "evil.py"))),
                            safe.replace(shlex.quote(str(self.project)), shlex.quote(str(self.base))),
                            safe.replace(identifier, OTHER_ID),
                            safe + " --extra", safe.replace("--no-python-downloads", "--python 3.11")):
                with self.subTest(command=command):
                    self.assert_denied(self.tool("Bash", command=command))
            self.assert_denied(self.tool("Bash", command=safe, run_in_background=True))
            self.assert_denied(self.tool("Bash", command=safe, dangerouslyDisableSandbox=True))

    def test_global_runner_exemption_does_not_grant_permissions_for_pending_implement(self):
        self.prompt("/devflow:implement feature.md")
        self.assertIsNone(self.tool("PowerShell", command=self.command("status")))
        self.assert_denied(self.tool("Bash", command=self.command("status") + " && other"))
        self.assert_denied(self.tool("Bash", command=self.command("status", OTHER_ID)))

    def test_unsealed_interview_cannot_move_even_after_a_pause(self):
        identifier = self.plan_start()
        reason = self.run_dir("plan", identifier) / "draft" / "pause.json"
        atomic_json(reason, {"reason": "Need the human first", "questions": ["Which scope?"]})
        self.assertEqual(self.engine("plan").pause(identifier, reason)["status"], "awaiting_human")
        with self.assertRaisesRegex(DevFlowError, "unsealed interview"):
            self.prompt("/devflow:resume " + identifier, session="other-session")
        self.assertEqual(self.engine("plan").status(identifier)["owner_session"], self.session)

    def test_resume_of_a_finished_run_records_the_request(self):
        identifier = self.plan_start()
        self.engine("plan").cancel(identifier)
        before = len(read_json(self.run_dir("plan", identifier) / "state.json")["event_ids"])
        self.prompt("/devflow:resume " + identifier, session="reader-session")
        state = read_json(self.run_dir("plan", identifier) / "state.json")
        self.assertEqual((len(state["event_ids"]), state["owner_session"]), (before + 1, "reader-session"))

    def test_resume_in_fresh_session_binds_immediately_without_runner_resume(self):
        identifier = self.implement_start()
        self.session = "fresh-resume-session"
        self.prompt("/devflow:resume " + identifier)
        self.assertEqual(self.record()["run"], {"flow": "implement", "run_id": identifier})
        self.assertIsNone(self.tool("Bash", command=self.command("status", identifier)))
        code, output, error = self.invoke("--session", self.session, "--run", identifier, "status")
        self.assertEqual((code, error), (0, ""), output)
        value = json.loads(output)
        self.assertEqual((value["flow"], value["owner_session"], value["review_count"]),
                         ("implement", self.session, 0))
        self.assert_denied(self.tool("Bash", command=self.command("status", OTHER_ID)))
        code, output, _ = self.invoke("--session", self.session, "--run", identifier, "implement", "resume")
        self.assertEqual((code, json.loads(output)["error"]), (2, "UsageError"))


class FrozenConfigurationTests(RuntimeFixture):
    def test_editing_project_settings_after_start_does_not_break_the_run(self):
        identifier = self.plan_start()
        atomic_json(self.project / "devflow.config.json", {"schema_version": 1, "typo": {}})
        for args in (("plan", "status"), ("plan", "doctor"), ("plan", "cancel")):
            with self.subTest(args=args):
                code, output, _ = self.invoke("--session", self.session, *args)
                self.assertEqual(code, 0, output)
        self.assertEqual(self.engine("plan").status(identifier)["status"], "cancelled")
        code, output, _ = self.invoke("--session", "other", "plan", "doctor")  # Reads project settings.
        self.assertEqual(code, 2)
        self.assertIn("Unknown devflow.config.json keys", output)


class PendingStartGuardTests(RuntimeFixture):
    def setUp(self):
        super().setUp()
        self.prompt("/devflow:implement feature.md")

    def test_only_diagnostics_read_only_work_and_the_exact_start_are_permitted(self):
        for name, data in (("Read", {"file_path": str(self.plan_file)}), ("Bash", {"command": "grep -rn 'a|b' ."}),
                           ("Bash", {"command": self.command("status")}), ("Bash", {"command": self.command("doctor")}),
                           ("Bash", {"command": self.command("start --help", flow="implement")}),
                           ("Bash", {"command": self.command("hook --help")}),
                           ("Bash", {"command": self.command("start --plan feature.md", flow="implement")}),
                           ("Bash", {"command": self.command("start --plan ./feature.md", flow="implement")}),
                           ("Bash", {"command": self.command("start", flow="implement") + " --plan " + shlex.quote(str(self.plan_file))})):
            with self.subTest(name=name, data=data):
                self.assertIsNone(self.tool(name, **data))
        other = self.project / "other.md"
        other.write_text("x\n", encoding="utf-8")
        for name, data in (("AskUserQuestion", {"questions": [{"question": "Q?"}]}),
                           ("Write", {"file_path": str(self.project / "source.py")}),
                           ("Bash", {"command": "rm -rf build"}),
                           ("Bash", {"command": self.command("start --plan other.md", flow="implement")}),
                           ("Bash", {"command": self.command("checkpoint --summary-file x.md", flow="implement")}),
                           ("Skill", {"skill": "grilling"})):
            with self.subTest(name=name, data=data):
                self.assert_denied(self.tool(name, **data))

    def test_stop_and_answers_are_not_recorded_while_pending(self):
        self.assertIsNone(runtime.handle_hook(self.payload(event="Stop"), PLUGIN))
        self.assertIsNone(runtime.handle_hook(self.payload(event="PostToolUse", tool_name="AskUserQuestion",
                                                           tool_use_id="q", tool_input={}, tool_response="A"), PLUGIN))
        self.assertEqual(self.record()["pending_start"]["flow"], "implement")
        self.assertFalse((self.project / ".devflow" / "runs").exists())

    def test_runner_start_must_match_the_pending_input(self):
        other = self.project / "other.md"
        other.write_text("Other plan.\n", encoding="utf-8")
        with self.assertRaisesRegex(DevFlowError, "does not match"):
            runtime.runner_start(self.project, self.session, "implement", "other.md")
        with self.assertRaisesRegex(DevFlowError, "explicit /devflow:implement"):
            runtime.runner_start(self.project, "another-session", "implement", "feature.md")
        state = runtime.runner_start(self.project, self.session, "implement", "./feature.md")
        self.assertEqual(self.record()["run"], {"flow": "implement", "run_id": state["run_id"]})
        first = read_json(self.run_dir("implement", state["run_id"]) / "request.json")
        self.assertEqual(first["event"]["payload"]["prompt"], "/devflow:implement feature.md")


class RunFormatTests(RuntimeFixture):
    def test_valid_old_run_is_listed_as_unsupported_and_does_not_block(self):
        identifier = self.raw_run("plan", status="paused_error")
        overview = runtime.status(self.project, self.session, None)
        self.assertEqual(overview["runs"][0]["format"], "unsupported")
        selected = runtime.status(self.project, None, identifier)
        self.assertEqual(selected["format"], "unsupported")
        self.assertIn("0.1.x", selected["message"])
        self.assertEqual(selected["project"], str(self.project))
        with self.assertRaisesRegex(DevFlowError, "0.1.x"):
            self.prompt("/devflow:resume " + identifier)
        self.plan_start()  # An unsupported run never blocks a new one.

    def test_old_run_with_a_leftover_transaction_does_not_block(self):
        identifier = self.raw_run("plan", status="reviewing")
        atomic_json(self.run_dir("plan", identifier) / "transaction.json",
                    {"schema_version": 1, "base_revision": 0, "state": {}, "writes": {}})
        listed = runtime.status(self.project, self.session, None)["runs"][0]
        self.assertEqual((listed["format"], listed["recovery_required"]), ("unsupported", True))
        self.assertNotIn(identifier, " ".join(runtime.blocking_warnings(self.project)))
        self.plan_start()
        self.assertTrue((self.run_dir("plan", identifier) / "transaction.json").exists())  # Never touched.

    def test_damaged_runs_are_shown_individually_and_block_starts(self):
        for index, (version, extra) in enumerate(((None, {}), (True, {}), ("2", {}), (0, {}), (3, {}),
                                                  (1, {"run_id": OTHER_ID}), (1, {"flow": "implement"}),
                                                  (1, {"flow": None}))):
            with self.subTest(version=version, extra=extra):
                identifier = f"20261101T12345{index}Z-123456abcdef"
                path = self.run_dir("plan", identifier) / "state.json"
                state = {"flow": "plan", "run_id": identifier, "status": "interview", "project": str(self.project), **extra}
                if version is not None:
                    state["schema_version"] = version
                atomic_json(path, state)
                overview = runtime.status(self.project, self.session, None)
                listed = {item["run_id"]: item for item in overview["runs"]}
                self.assertEqual(listed[identifier]["format"], "invalid")
                self.assertIn("error", listed[identifier])
                with self.assertRaises(DevFlowError):
                    runtime.status(self.project, None, identifier)
                with self.assertRaisesRegex(DevFlowError, "cannot be read"):
                    self.prompt("/devflow:plan Another request", session="new-session")
                path.unlink()
                path.parent.rmdir()

    def test_pending_transaction_is_reported_blocks_starts_and_resume_completes_it(self):
        identifier = self.plan_start()
        store = RunStore(self.project, WORKFLOWS["plan"])
        with store.lock(identifier):
            state = store.load(identifier)
            state["last_error"] = {"message": "interrupted"}
            desired = dict(state, state_revision=state["state_revision"] + 1)
            atomic_json(self.run_dir("plan", identifier) / "transaction.json",
                        {"schema_version": 2, "base_revision": state["state_revision"], "state": desired, "writes": {}})
        before = sorted(p.name for p in self.run_dir("plan", identifier).iterdir())
        overview = runtime.status(self.project, "other-session", None)
        self.assertTrue(overview["runs"][0]["recovery_required"])
        self.assertTrue(runtime.status(self.project, None, identifier)["recovery_required"])
        self.assertEqual(sorted(p.name for p in self.run_dir("plan", identifier).iterdir()), before)
        with self.assertRaisesRegex(DevFlowError, "interrupted transaction"):
            self.prompt("/devflow:implement feature.md", session="other-session")
        self.prompt("/devflow:resume " + identifier)
        self.assertFalse((self.run_dir("plan", identifier) / "transaction.json").exists())
        self.assertEqual(self.engine("plan").status(identifier)["last_error"], {"message": "interrupted"})

    def test_cancel_also_completes_a_pending_transaction(self):
        identifier = self.plan_start()
        store = RunStore(self.project, WORKFLOWS["plan"])
        with store.lock(identifier):
            state = store.load(identifier)
            desired = dict(state, state_revision=state["state_revision"] + 1, last_error={"message": "x"})
            atomic_json(self.run_dir("plan", identifier) / "transaction.json",
                        {"schema_version": 2, "base_revision": state["state_revision"], "state": desired, "writes": {}})
        self.assertEqual(self.engine("plan").cancel(identifier)["status"], "cancelled")
        self.assertFalse((self.run_dir("plan", identifier) / "transaction.json").exists())

    def test_corrupt_journal_is_not_resumed(self):
        identifier = self.plan_start()
        (self.run_dir("plan", identifier) / "transaction.json").write_text('{"schema_version": 2}', encoding="utf-8")
        with self.assertRaisesRegex(DevFlowError, "pending transaction"):
            self.prompt("/devflow:resume " + identifier)
        self.assertTrue((self.run_dir("plan", identifier) / "transaction.json").exists())

    def test_old_per_workflow_session_directories_do_not_block_starts(self):
        old = self.project / ".devflow" / "sessions"
        (old / "plan").mkdir(parents=True)
        atomic_json(old / "plan" / ("a" * 64 + ".json"), {"run_id": "x", "guard_active": True})
        (old / "implement").mkdir()
        self.assertEqual(runtime.status(self.project, self.session, None)["sessions"]["invalid_records"], [])
        self.plan_start()
        self.assertTrue((old / "plan" / ("a" * 64 + ".json")).exists())  # Never read, never removed.

    def test_damaged_created_run_of_a_pending_start_is_shown_without_failing_status(self):
        original = sessions.write_record

        def fail_on_binding(project, record, workflows):
            if record["run"] is not None:
                raise OSError("simulated stop after creation")
            return original(project, record, workflows)

        with patch.object(sessions, "write_record", side_effect=fail_on_binding), self.assertRaises(OSError):
            self.prompt("/devflow:plan Interrupted request")
        identifier = self.record()["pending_start"]["run_id"]
        atomic_json(self.run_dir("plan", identifier) / "state.json", {"schema_version": 2, "status": []})
        overview = runtime.status(self.project, "other-session", None)
        self.assertEqual(overview["runs"][0]["format"], "invalid")
        self.assertEqual(overview["sessions"]["pending_starts"], [])
        self.assertIn(f"Run plan/{identifier} cannot be read", " ".join(runtime.blocking_warnings(self.project)))
        probe, interview, claude, which = UnifiedCliTests.doctor_patches(self)
        with probe, interview, claude, which:
            code, output, _ = self.invoke("doctor")
        self.assertEqual(code, 0, output)

    def test_oversized_start_prompt_is_rejected_before_anything_is_written(self):
        with self.assertRaisesRegex(DevFlowError, "4 MiB"):
            self.prompt("/devflow:plan " + "x" * (4 * 1024 * 1024))
        self.assertFalse((self.project / ".devflow").exists())

    def test_incomplete_creation_is_listed_but_not_a_run(self):
        leftover = self.project / ".devflow" / "runs" / "plan" / ".creating-20261101T123456Z-123456abcdef"
        leftover.mkdir(parents=True)
        overview = runtime.status(self.project, self.session, None)
        self.assertEqual(overview["incomplete_creations"], ["plan/" + leftover.name])
        self.assertEqual(overview["runs"], [])
        self.assertIn("Incomplete run creation", " ".join(runtime.blocking_warnings(self.project)))
        self.plan_start()


class InterruptionTests(RuntimeFixture):
    def test_start_interrupted_before_binding_completes_on_the_next_prompt(self):
        original = sessions.write_record
        calls = []

        def fail_on_binding(project, record, workflows):
            calls.append(record)
            if record["run"] is not None:
                raise OSError("simulated stop after the run was created")
            return original(project, record, workflows)

        with patch.object(sessions, "write_record", side_effect=fail_on_binding):
            with self.assertRaises(OSError):
                self.prompt("/devflow:plan Interrupted request")
        pending = self.record()["pending_start"]
        self.assertTrue(self.run_dir("plan", pending["run_id"]).is_dir())
        self.assert_denied(self.tool(file_path=str(self.project / "x.md")))  # Pending: still guarded.
        self.prompt("first answer")
        self.assertEqual(self.record()["run"], {"flow": "plan", "run_id": pending["run_id"]})

    def test_failed_in_hook_creation_clears_its_pending_start(self):
        atomic_json(self.project / "devflow.config.json", {"schema_version": 1, "plan": {"max_reviews": 7}})
        with self.assertRaises(DevFlowError):
            self.prompt("/devflow:plan Request with an invalid configuration")
        self.assertIsNone(self.record())
        self.assertEqual(runtime.status(self.project, "other", None)["sessions"]["pending_starts"], [])

    def test_start_killed_before_creation_is_abandoned_on_the_next_prompt(self):
        # A killed process leaves its pending start behind (no cleanup runs).
        pending = {"flow": "plan", "run_id": "20261101T123456Z-aaaaaaaaaaaa", "args": "Interrupted request",
                   "created_at": "2026-11-01T00:00:00+00:00",
                   "event": make_event(self.payload(prompt="/devflow:plan Interrupted request"), "prompt")}
        (self.project / ".devflow").mkdir()
        sessions.write_record(self.project, dict(sessions.empty_record(self.session), pending_start=pending), WORKFLOWS)
        with self.assertRaisesRegex(DevFlowError, "pending plan start"):
            self.prompt("/devflow:plan Another request", session="other-session")
        self.assertIsNone(self.prompt("Hello again"))
        self.assertIsNone(self.record())

    def test_orphaned_started_run_can_be_taken_over_and_the_original_pending_is_dropped(self):
        original = sessions.write_record

        def fail_on_binding(project, record, workflows):
            if record["run"] is not None and record["session_id"] == self.session:
                raise OSError("simulated stop")
            return original(project, record, workflows)

        self.prompt("/devflow:implement feature.md")
        with patch.object(sessions, "write_record", side_effect=fail_on_binding):
            with self.assertRaises(OSError):
                runtime.runner_start(self.project, self.session, "implement", "feature.md")
        identifier = self.record()["pending_start"]["run_id"]
        self.prompt("/devflow:resume " + identifier, session="rescue-session")
        self.assertEqual(self.engine("implement").status(identifier)["owner_session"], "rescue-session")
        self.prompt("I am back")
        self.assertIsNone(self.record())

    def test_resume_interrupted_before_ownership_moves_asks_to_resend(self):
        identifier = self.implement_start()
        with patch.object(RunStore, "transfer", side_effect=OSError("simulated stop")):
            with self.assertRaises(OSError):
                self.prompt("/devflow:resume " + identifier, session="new-session")
        self.assertEqual(self.record("new-session")["run"]["run_id"], identifier)
        self.assertEqual(self.engine("implement").status(identifier)["owner_session"], self.session)
        self.assert_denied(self.tool(file_path=str(self.project / "x.py"), session="new-session"))
        with self.assertRaisesRegex(DevFlowError, "/devflow:resume"):
            self.prompt("continue", session="new-session")
        self.assertIsNone(self.tool("Read", file_path=str(self.plan_file)))  # The original owner keeps working.
        self.prompt("/devflow:resume " + identifier, session="new-session")
        self.assertEqual(self.engine("implement").status(identifier)["owner_session"], "new-session")

    def test_same_session_resume_interrupted_keeps_ownership(self):
        identifier = self.implement_start()
        with patch.object(RunStore, "transfer", side_effect=OSError("simulated stop")):
            with self.assertRaises(OSError):
                self.prompt("/devflow:resume " + identifier)
        self.assertEqual(self.record()["run"]["run_id"], identifier)
        self.assertIsNone(self.tool("Read", file_path=str(self.plan_file)))

    def test_resume_is_exclusive(self):
        finished = self.plan_start()
        self.engine("plan").cancel(finished)
        self.prompt("next")
        active = self.implement_start()
        with self.assertRaisesRegex(DevFlowError, "unfinished implement"):
            self.prompt("/devflow:resume " + finished, session="other-session")
        with self.assertRaisesRegex(DevFlowError, "Finish or cancel"):
            self.prompt("/devflow:resume " + finished)
        self.assertEqual(self.engine("plan").status(finished)["owner_session"], self.session)
        self.assertIsNone(self.record("other-session"))
        self.assertEqual(self.record()["run"]["run_id"], active)


class SessionRecordTests(RuntimeFixture):
    def test_invalid_records_are_listed_and_block_starts(self):
        directory = self.project / ".devflow" / "sessions"
        good = sessions.empty_record("someone")
        pending = {"flow": "implement", "run_id": OTHER_ID, "args": "x", "created_at": "t",
                   "event": make_event({"prompt": "/devflow:implement x"}, "prompt")}
        cases = {
            "version": dict(good, schema_version=1, run={"flow": "plan", "run_id": OTHER_ID}),
            "both": dict(good, run={"flow": "plan", "run_id": OTHER_ID}, pending_start=pending),
            "flow": dict(good, run={"flow": "deploy", "run_id": OTHER_ID}),
            "run_id": dict(good, run={"flow": "plan", "run_id": "../x"}),
            "args": dict(good, pending_start=dict(pending, args=3)),
            "event": dict(good, pending_start=dict(pending, event=dict(pending["event"], event_id="evt-1"))),
        }
        for name, record in cases.items():
            with self.subTest(case=name):
                path = sessions.session_path(self.project, "someone")
                atomic_json(path, record)
                overview = runtime.status(self.project, self.session, None)
                self.assertEqual(overview["sessions"]["invalid_records"][0]["record"], path.name)
                with self.assertRaisesRegex(DevFlowError, "invalid"):
                    self.prompt("/devflow:plan New request")
                path.unlink()
        misplaced = directory / ("0" * 64 + ".json")
        atomic_json(misplaced, good | {"run": {"flow": "plan", "run_id": OTHER_ID}})
        self.assertIn("does not match", runtime.status(self.project, self.session, None)["sessions"]["invalid_records"][0]["error"])

    def test_record_with_non_string_flow_is_listed_as_invalid(self):
        pending = {"flow": [], "run_id": OTHER_ID, "args": "x", "created_at": "t",
                   "event": make_event({"prompt": "/devflow:plan x"}, "prompt")}
        for record in (dict(sessions.empty_record("someone"), run={"flow": [], "run_id": OTHER_ID}),
                       dict(sessions.empty_record("someone"), run={"flow": {}, "run_id": OTHER_ID}),
                       dict(sessions.empty_record("someone"), pending_start=pending)):
            with self.subTest(record=record):
                path = sessions.session_path(self.project, "someone")
                atomic_json(path, record)
                overview = runtime.status(self.project, self.session, None)
                self.assertEqual(overview["sessions"]["invalid_records"][0]["record"], path.name)
                path.unlink()

    def test_pending_start_whose_run_path_is_a_link_does_not_break_status(self):
        target = self.base / "elsewhere"
        target.mkdir()
        runs = self.project / ".devflow" / "runs" / "plan"
        runs.mkdir(parents=True)
        try:
            (runs / OTHER_ID).symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Creating symlinks requires OS permission")
        pending = {"flow": "plan", "run_id": OTHER_ID, "args": "x", "created_at": "t",
                   "event": make_event({"prompt": "/devflow:plan x"}, "prompt")}
        sessions.write_record(self.project, dict(sessions.empty_record("someone"), pending_start=pending), WORKFLOWS)
        overview = runtime.status(self.project, self.session, None)
        self.assertEqual([item["format"] for item in overview["runs"]], ["invalid"])
        self.assertEqual(overview["sessions"]["pending_starts"], [])
        runtime.blocking_warnings(self.project)

    def test_stale_binding_reports_owner_mismatch(self):
        identifier = self.implement_start()
        self.prompt("/devflow:resume " + identifier, session="other-session")
        value = runtime.status(self.project, self.session, None)
        self.assertEqual((value["status"], value["owner_matches"]), ("stale", False))
        self.assert_denied(self.tool("Read", file_path=str(self.plan_file)))


class UnifiedCliTests(RuntimeFixture):
    def process(self, *args, payload=""):
        return subprocess.run([sys.executable, "-B", str(SCRIPT), "--project", str(self.project), *args],
                              input=payload, capture_output=True, encoding="utf-8", timeout=30, shell=False)

    def test_help_version_and_workflow_help_are_text(self):
        for args, expected in ((("--version",), "devflow " + __version__), (("--help",), "DevFlow"),
                               (("plan", "--help"), "seal"), (("implement", "--help"), "checkpoint"),
                               (("implement", "start", "--help"), "--plan")):
            with self.subTest(args=args):
                result = self.process(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, result.stdout)

    def test_usage_errors_and_late_globals_are_json(self):
        for args in (("plan", "hook"), ("implement", "hook"), ("plan",), ("implement", "checkpoint"),
                     ("plan", "--project", str(self.base), "status"),
                     ("implement", "--session", "other", "start"),
                     ("status", "--run", "arbitrary"), ("deploy",), ("plan", "seal", "--bogus", "x")):
            with self.subTest(args=args):
                code, output, error = self.invoke(*args)
                self.assertEqual((code, error), (2, ""))
                value = json.loads(output)
                self.assertEqual(value["error"], "UsageError")
                self.assertFalse(value["automatic_retry"])

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

    def test_runner_start_creates_implementation_only_from_the_pending_start(self):
        self.prompt("/devflow:implement feature.md")
        code, output, error = self.invoke("--session", self.session, "implement", "start", "--plan", str(self.plan_file))
        self.assertEqual((code, error), (0, ""), output)
        state = json.loads(output)
        self.assertEqual((state["flow"], state["status"]), ("implement", "implementing"))
        self.assertEqual(Path(state["run_dir"]).parent, self.project / ".devflow" / "runs" / "implement")
        self.assertFalse((self.project / ".devflow" / "runs" / "plan").exists())

    def test_cli_start_cannot_cross_unfinished_project_workflow(self):
        self.plan_start()
        code, output, _ = self.invoke("--session", "second-session", "implement", "start", "--plan", str(self.plan_file))
        self.assertEqual(code, 2)
        self.assertIn("explicit /devflow:implement", output)
        self.assertFalse((self.project / ".devflow" / "runs" / "implement").exists())

    def doctor_patches(self, skill_report=None, supported=True, which=None):
        skill_report = skill_report or {"configured_name": "grilling", "status": "available", "static_ready": True,
                                        "runtime_verified": False, "reason": "Fixture metadata permits invocation"}
        return (patch.object(cli, "preflight", return_value={"offline": True}),
                patch("devflow_core.plan.interview.inspect_interview_skill", return_value=skill_report),
                patch.object(cli, "claude_version", return_value={"supported": supported, "version": "2.1.295"}),
                patch.object(cli.shutil, "which", side_effect=which or (lambda name: "/synthetic/available")))

    def test_doctor_is_offline_and_reports_minimum_supported_claude(self):
        probe, interview, claude, which = self.doctor_patches()
        with probe as preflight, interview, claude, which:
            code, output, error = self.invoke("doctor")
        self.assertEqual((code, error), (0, ""))
        report = json.loads(output)
        self.assertEqual(report["minimum_claude_code"], "2.1.295")
        self.assertTrue(report["ready"], report)
        self.assertEqual(report["workflows"]["plan"]["interview_skill"]["status"], "available")
        self.assertEqual(report["workflows"]["plan"]["configuration_source"], "project_settings")
        self.assertFalse(report["python"]["fixed_minor"])
        self.assertEqual(preflight.call_count, 2)  # Shared provider probes are cached.
        self.assertFalse((self.project / ".devflow").exists())

    def test_doctor_missing_interview_does_not_block_implementation_readiness(self):
        for status in ("missing", "blocked", "ambiguous", "invalid", "unverified"):
            skill_report = {"configured_name": "grilling", "status": status, "static_ready": False,
                            "runtime_verified": False, "reason": "Fixture " + status}
            probe, interview, claude, which = self.doctor_patches(skill_report)
            with self.subTest(status=status), probe, interview, claude, which:
                code, output, error = self.invoke("doctor")
            self.assertEqual((code, error), (0, ""))
            report = json.loads(output)
            self.assertFalse(report["ready"])
            self.assertFalse(report["workflows"]["plan"]["ready"])
            self.assertTrue(report["workflows"]["plan"]["reviewers_ready"])
            self.assertTrue(report["workflows"]["implement"]["ready"])
            self.assertNotIn("interview_skill", report["workflows"]["implement"])

    def test_doctor_old_claude_or_missing_uv_is_not_ready(self):
        for supported, which in ((False, None), (True, lambda name: None if name == "uv" else "/synthetic/tool")):
            probe, interview, claude, which_patch = self.doctor_patches(supported=supported, which=which)
            with self.subTest(supported=supported), probe, interview, claude, which_patch:
                code, output, _ = self.invoke("doctor")
            self.assertEqual(code, 0)
            self.assertFalse(json.loads(output)["ready"])

    def test_doctor_warns_about_what_blocks_starts(self):
        self.prompt("/devflow:implement feature.md", session="lost-session")
        probe, interview, claude, which = self.doctor_patches()
        with probe, interview, claude, which:
            code, output, _ = self.invoke("doctor")
        self.assertIn("Pending implement start", " ".join(json.loads(output)["warnings"]))

    def test_claude_version_probe_never_authenticates_or_uses_shell(self):
        result = subprocess.CompletedProcess(["claude", "--version"], 0, "2.1.295 (Claude Code)\n", "")
        with patch.object(cli.shutil, "which", return_value="/safe/claude"), \
             patch.object(cli.subprocess, "run", return_value=result) as execute:
            report = cli.claude_version()
        self.assertTrue(report["supported"])
        self.assertEqual(execute.call_args.args[0], ["/safe/claude", "--version"])
        self.assertFalse(execute.call_args.kwargs["shell"])


class RegistryTests(RuntimeFixture):
    def test_a_registered_workflow_is_routed_without_router_or_cli_changes(self):
        from devflow_core.common.workflow import Workflow

        class Dummy(Workflow):
            name = "dummy"
            statuses = frozenset({"working", "completed", "exhausted", "cancelled"})

            def normalize_args(self, raw, project):
                return raw.strip()

            def register_commands(self, commands):
                commands.add_parser("status")

        with patch.dict(WORKFLOWS, {"dummy": Dummy()}):
            self.assertEqual(runtime.public_command("/devflow:dummy go"), ("dummy", "go"))
            args = cli.parser().parse_args(["dummy", "status"])
            self.assertEqual((args.command, args.action), ("dummy", "status"))
            self.assertIn("dummy", runtime.stores(self.project))
            self.assertEqual(runtime.status(self.project, self.session, None)["runs"], [])


if __name__ == "__main__":
    unittest.main()
