"""Router and CLI tests: offline fixtures, no authentication or model calls."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from devflow_core import __version__, cli, router as runtime
from devflow_core.common import sessions
from devflow_core.common.errors import DevFlowError
from devflow_core.common.runs import SCHEMA_VERSION, RunStore, make_event
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

    def bind_to_old_run(self, session=None, pending=False, identifier="20261101T123456Z-123456abcdef",
                        flow="implement", version=2):
        # An intentional old-run fixture: the version is always below SCHEMA_VERSION.
        self.raw_run(flow, identifier, status="implementing" if flow == "implement" else "planning",
                     schema_version=version)
        record = sessions.empty_record(session or self.session)
        if pending:
            record = dict(record, pending_start={"flow": flow, "run_id": identifier, "args": "x",
                                                 "created_at": "t",
                                                 "event": make_event({"prompt": f"/devflow:{flow} x"}, "prompt")})
        else:
            record = dict(record, run={"flow": flow, "run_id": identifier})
        sessions.write_record(self.project, record, WORKFLOWS)
        return identifier

    def ask(self, tool_use_id, session=None, **tool_input):
        """PreToolUse of an AskUserQuestion, as Claude sent it."""
        return runtime.handle_hook(self.payload(event="PreToolUse", session=session, tool_name="AskUserQuestion",
                                                tool_use_id=tool_use_id, tool_input=tool_input), PLUGIN)

    def assert_not_accepted(self, result, reason=""):
        """A /devflow: command DevFlow did not act on: context for Claude, never an exception."""
        self.assertIsInstance(result, dict, result)
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("did not accept", context)
        self.assertIn(reason, context)
        return context


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
        self.assertEqual((state["schema_version"], state["owner_session"]), (SCHEMA_VERSION, self.session))
        self.assertEqual(len(state["event_ids"]), 1)  # The start prompt itself.

    def test_implementation_start_only_captures_pending_start_until_cli_start(self):
        self.prompt('/devflow:implement "feature.md"')
        pending = self.record()["pending_start"]
        self.assertEqual(pending["flow"], "implement")
        self.assertEqual(pending["args"], str(self.plan_file))
        self.assertFalse((self.project / ".devflow" / "runs").exists())

    def test_active_plan_does_not_accept_implementation_or_record_it_as_answer(self):
        identifier = self.plan_start()
        before = read_json(self.run_dir("plan", identifier) / "state.json")
        self.assert_not_accepted(self.prompt("/devflow:implement feature.md"), "Finish or cancel")
        after = read_json(self.run_dir("plan", identifier) / "state.json")
        self.assertEqual(before, after)
        self.assertEqual(self.record()["run"]["run_id"], identifier)

    def test_one_unfinished_workflow_per_project_across_sessions(self):
        self.plan_start()
        for command in ("/devflow:plan Another request", "/devflow:implement feature.md"):
            with self.subTest(command=command):
                self.assert_not_accepted(self.prompt(command, session="second-human-session"),
                                         "Another unfinished plan run")
        self.assertIsNone(self.record("second-human-session"))

    def test_pending_implementation_is_not_accepted_from_a_different_session(self):
        self.prompt("/devflow:implement feature.md")
        for command in ("/devflow:plan Another request", "/devflow:implement feature.md"):
            with self.subTest(command=command):
                self.assert_not_accepted(self.prompt(command, session="other-session"), "pending implement start")

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

    def test_finished_run_stays_bound_for_the_turn_then_next_prompt_switches(self):
        identifier = self.plan_start()
        self.engine("plan").cancel(identifier)
        self.assertIsNone(self.tool(file_path=str(self.project / "source.py")))
        self.assertEqual(self.record()["run"], {"flow": "plan", "run_id": identifier})
        self.prompt("/devflow:implement feature.md")
        record = self.record()
        self.assertIsNone(record["run"])
        self.assertEqual(record["pending_start"]["flow"], "implement")

    def test_terminal_next_ordinary_prompt_releases_binding_without_new_run(self):
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
        # The finished run stays bound until the next prompt; no tool is stopped meanwhile.
        self.assertIsNone(self.tool(file_path=str(self.project / "source.py")))
        self.assertEqual(self.record()["run"], {"flow": "implement", "run_id": identifier})
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
        self.assert_not_accepted(self.prompt("/devflow:resume " + identifier), "ambiguous")
        with self.assertRaisesRegex(DevFlowError, "ambiguous"):
            runtime.status(self.project, None, identifier)
        with self.assertRaises(DevFlowError):
            runtime.resolve_run(self.project, "20261101T123456Z-aaaaaaaaaaaa", runtime.stores(self.project))

    def test_invalid_start_or_resume_does_not_create_workspace(self):
        for prompt in ("/devflow:plan", "/devflow:implement", "/devflow:resume ../bad",
                       "/devflow:implement --plan other", "/devflow:plan /other:skill prompt"):
            with self.subTest(prompt=prompt):
                self.assert_not_accepted(self.prompt(prompt))
            self.assertFalse((self.project / ".devflow").exists())

    def test_stale_dispatch_lock_is_not_deleted_or_ignored(self):
        lock = self.project / ".devflow" / "dispatch.lock"
        write_new(lock, b'{"token":"existing-lock"}\n')
        self.assert_not_accepted(self.prompt("/devflow:plan REVIEW-10 の計画を作成して"), "locked")
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

    def test_runner_start_cannot_target_an_existing_run(self):
        self.prompt("/devflow:implement feature.md")
        code, output, _ = self.invoke("--session", self.session, "--run", OTHER_ID, "implement", "start",
                                      "--plan", "feature.md")
        self.assertEqual(code, 2)
        self.assertIn("cannot target an existing run", json.loads(output)["message"])

    def test_unsealed_interview_cannot_move_even_after_a_pause(self):
        identifier = self.plan_start()
        reason = self.run_dir("plan", identifier) / "draft" / "pause.json"
        atomic_json(reason, {"reason": "Need the human first", "questions": ["Which scope?"]})
        self.assertEqual(self.engine("plan").pause(identifier, reason)["status"], "awaiting_human")
        self.assert_not_accepted(self.prompt("/devflow:resume " + identifier, session="other-session"),
                                 "unsealed interview")
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
        code, output, error = self.invoke("--session", self.session, "--run", identifier, "status")
        self.assertEqual((code, error), (0, ""), output)
        value = json.loads(output)
        self.assertEqual((value["flow"], value["owner_session"], value["review_count"]),
                         ("implement", self.session, 0))
        code, output, _ = self.invoke("--session", self.session, "--run", identifier, "implement", "resume")
        self.assertEqual((code, json.loads(output)["error"]), (2, "UsageError"))


class ToolCallsAreNeverStopped:
    """DevFlow never stops a tool call: Claude Code's settings and permission mode decide.

    Mixed into one test class per workflow; PreToolUse returns None for every
    call below whether the session owns the run, does not own it, has only a
    pending start, or is bound to a run of an unsupported (older) format.
    """
    flow = ""

    def calls(self):
        return (("Write", {"file_path": str(self.project / "src" / "feature.py"), "content": "x"}),
                ("Bash", {"command": "rm -rf x"}),
                ("PowerShell", {"command": "Remove-Item -Recurse -Force x"}),
                ("Agent", {"subagent_type": "general-purpose", "prompt": "Implement it", "isolation": "worktree"}),
                ("mcp__files__write", {"path": "x", "content": "y"}),
                ("Skill", {"skill": "some-other-skill"}))

    def assert_nothing_stopped(self, session=None):
        for tool, data in self.calls():
            with self.subTest(tool=tool, session=session):
                self.assertIsNone(self.tool(tool, session=session, **data))

    def start(self):
        return self.plan_start() if self.flow == "plan" else self.implement_start()

    def cancel(self, identifier):
        if self.flow == "plan":
            self.engine("plan").cancel(identifier)
        else:
            self.engine("implement").cancel(identifier, self.session, "Human ended this attempt")

    def event_ids(self, identifier):
        return read_json(self.run_dir(self.flow, identifier) / "state.json")["event_ids"]

    def test_the_owner_is_never_stopped_and_its_tool_calls_are_not_recorded(self):
        identifier = self.start()
        before = self.event_ids(identifier)
        self.assert_nothing_stopped()
        self.assertEqual(self.event_ids(identifier), before)
        self.cancel(identifier)
        # A finished run stays bound for the rest of the turn; still nothing is stopped.
        self.assertEqual(self.record()["run"], {"flow": self.flow, "run_id": identifier})
        self.assert_nothing_stopped()

    def test_a_session_that_does_not_own_the_run_is_never_stopped(self):
        identifier = self.start()
        sessions.write_record(self.project, dict(sessions.empty_record("other-session"),
                                                 run={"flow": self.flow, "run_id": identifier}), WORKFLOWS)
        self.assertEqual(runtime.status(self.project, "other-session", None)["status"], "stale")
        before = self.event_ids(identifier)
        self.assert_nothing_stopped("other-session")
        self.assertIsNone(self.ask("other-question", session="other-session", questions=[{"question": "Q?"}]))
        self.assertEqual(self.event_ids(identifier), before)

    def test_a_pending_start_is_never_stopped(self):
        if self.flow == "implement":
            self.prompt("/devflow:implement feature.md")
        else:
            # Plan creates its run inside the prompt hook; a pending start remains only if that hook dies.
            pending = {"flow": "plan", "run_id": OTHER_ID, "args": "Interrupted request",
                       "created_at": "2026-11-01T00:00:00+00:00",
                       "event": make_event(self.payload(prompt="/devflow:plan Interrupted request"), "prompt")}
            (self.project / ".devflow").mkdir()
            sessions.write_record(self.project, dict(sessions.empty_record(self.session), pending_start=pending),
                                  WORKFLOWS)
        self.assertEqual(self.record()["pending_start"]["flow"], self.flow)
        self.assert_nothing_stopped()
        self.assertIsNone(self.ask("pending-question", questions=[{"question": "Q?"}]))
        self.assertFalse((self.project / ".devflow" / "runs").exists())  # Nothing to record into.

    def test_a_session_bound_to_an_unsupported_run_is_never_stopped(self):
        identifier = self.bind_to_old_run(flow=self.flow, version=4)  # Old-run fixture: format 4 was never released.
        self.assertEqual(runtime.status(self.project, self.session, None)["status"], "unsupported")
        before = read_json(self.run_dir(self.flow, identifier) / "state.json")
        self.assert_nothing_stopped()
        self.assertIsNone(self.ask("old-question", questions=[{"question": "Q?"}]))
        self.assertEqual(read_json(self.run_dir(self.flow, identifier) / "state.json"), before)

    def test_every_question_of_the_owner_is_recorded_as_an_observation(self):
        identifier = self.start()
        store = RunStore(self.project, WORKFLOWS[self.flow])
        plain = {"questions": [{"question": "Scope?"}]}
        asked = (("q-plain", plain), ("q-prefilled", dict(plain, answers={"Scope?": "All of it"})),
                 ("q-annotated", dict(plain, annotations={"Scope?": {"notes": "Written by the model"}})),
                 ("q-empty", dict(plain, answers={}, annotations={})))
        for use_id, data in asked:
            with self.subTest(tool_use_id=use_id):
                self.assertIsNone(self.ask(use_id, **data))
        observed = [event for event in store.events(store.read(identifier)) if event["kind"] == "observation"]
        self.assertEqual([(event["payload"]["tool_use_id"], event["payload"]["tool_input"]) for event in observed],
                         list(asked))
        # A hook retry with the same tool_use_id is the same event; changed content is an error.
        count = len(self.event_ids(identifier))
        self.assertIsNone(self.ask("q-plain", **plain))
        self.assertEqual(len(self.event_ids(identifier)), count)
        with self.assertRaisesRegex(DevFlowError, "different content"):
            self.ask("q-plain", questions=[{"question": "Another?"}])
        self.assertEqual(len(self.event_ids(identifier)), count)


class PlanToolCallTests(ToolCallsAreNeverStopped, RuntimeFixture):
    flow = "plan"


class ImplementToolCallTests(ToolCallsAreNeverStopped, RuntimeFixture):
    flow = "implement"


class CommandErrorTests(RuntimeFixture):
    def test_a_devflow_command_with_an_error_is_context_never_an_exception(self):
        for prompt, reason in (("/devflow:plan", "Use /devflow:plan followed by"),
                               ("/devflow:plan request\n/other:skill", "not stacked skills"),
                               ("/devflow:implement", "Use /devflow:implement followed by"),
                               ("/devflow:implement --plan x", "Supply a plan file"),
                               ("/devflow:resume ../bad", "")):
            with self.subTest(prompt=prompt):
                context = self.assert_not_accepted(self.prompt(prompt), reason)
                self.assertIn("Check the runner's status before continuing", context)
        self.assertFalse((self.project / ".devflow").exists())
        identifier = self.plan_start()
        for prompt in ("/devflow:plan Another request", "/devflow:implement feature.md"):
            with self.subTest(prompt=prompt):
                self.assert_not_accepted(self.prompt(prompt, session="second-session"), "Another unfinished plan run")
                self.assert_not_accepted(self.prompt(prompt), "Finish or cancel this session's active workflow")
        self.assertIsNone(self.record("second-session"))
        self.assertEqual(self.record()["run"], {"flow": "plan", "run_id": identifier})
        self.assertEqual([info.run_id for info in RunStore(self.project, WORKFLOWS["plan"]).scan()[0]], [identifier])


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


class PendingStartTests(RuntimeFixture):
    def setUp(self):
        super().setUp()
        self.prompt("/devflow:implement feature.md")

    def test_plan_crash_window_before_creation_leaves_a_pending_start_that_stops_nothing(self):
        # A Plan start that dies between the pending record and the run (here a
        # SystemExit, which _start does not catch) leaves the pending record;
        # no tool is stopped in that window and the next prompt drops it.
        with patch.object(runtime, "_create", side_effect=SystemExit(1)):
            with self.assertRaises(SystemExit):
                self.prompt("/devflow:plan REVIEW-10 の計画を作成して")
        self.assertEqual(self.record()["pending_start"]["flow"], "plan")
        self.assertIsNone(self.tool("Write", file_path=str(self.project / "source.py")))
        self.assertIsNone(self.tool("Read", file_path=str(self.plan_file)))
        self.assertIsNone(self.prompt("Never mind"))
        self.assertIsNone(self.record())

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
        self.assert_not_accepted(self.prompt("/devflow:resume " + identifier), "0.1.x")
        self.plan_start()  # An unsupported run never blocks a new one.

    def test_broken_frozen_configuration_is_listed_as_invalid_without_breaking_status(self):
        identifier = self.plan_start()
        self.engine("plan").cancel(identifier)
        path = self.run_dir("plan", identifier) / "state.json"
        good = read_json(path)
        for config in ("broken", {}, {"max_reviews": 6}, dict(good["config"], reviewers=[]),
                       dict(good["config"], max_reviews=True)):
            with self.subTest(config=config):
                atomic_json(path, dict(good, config=config))
                listed = runtime.status(self.project, "other-session", None)["runs"]
                self.assertEqual(listed[0]["format"], "invalid")
                self.assertIn("cannot be read", " ".join(runtime.blocking_warnings(self.project)))

    def test_a_summary_failure_marks_only_that_run_invalid(self):
        identifier = self.plan_start()
        with patch.object(WORKFLOWS["plan"], "summary", side_effect=KeyError("missing")):
            listed = runtime.status(self.project, "other-session", None)["runs"]
        self.assertEqual((listed[0]["run_id"], listed[0]["format"]), (identifier, "invalid"))
        self.assertIn("Cannot summarize run", listed[0]["error"])

    def test_old_run_with_a_leftover_transaction_does_not_block(self):
        # Runs of 0.5.x and earlier could leave a transaction.json; it is never read or touched.
        identifier = self.raw_run("plan", status="reviewing")
        atomic_json(self.run_dir("plan", identifier) / "transaction.json",
                    {"schema_version": 1, "base_revision": 0, "state": {}, "writes": {}})
        listed = runtime.status(self.project, self.session, None)["runs"][0]
        self.assertEqual(listed["format"], "unsupported")
        self.assertNotIn("recovery_required", listed)
        self.assertNotIn(identifier, " ".join(runtime.blocking_warnings(self.project)))
        self.plan_start()
        self.assertTrue((self.run_dir("plan", identifier) / "transaction.json").exists())  # Never touched.

    def test_runs_from_0_2_are_unsupported_and_never_block(self):
        identifier = self.raw_run("plan", status="reviewing", schema_version=2)
        info = RunStore(self.project, WORKFLOWS["plan"]).inspect(identifier)
        self.assertEqual((info.format, info.schema_version), ("unsupported", 2))
        selected = runtime.status(self.project, None, identifier)
        self.assertEqual(selected["message"], "Started with DevFlow 0.2.x; this version cannot continue it")
        self.assert_not_accepted(self.prompt("/devflow:resume " + identifier),
                                 f"Run {identifier} was started with DevFlow 0.2.x; this version cannot continue it")
        self.plan_start()
        current = RunStore(self.project, WORKFLOWS["plan"]).inspect(self.record()["run"]["run_id"])
        self.assertEqual(current.schema_version, SCHEMA_VERSION)

    @staticmethod
    def old_id(flow, version):
        return f"20261101T12345{version}Z-{'a' if flow == 'plan' else 'b'}23456abcdef"

    def test_session_bound_to_an_old_run_is_released_by_the_next_prompt(self):
        for flow in ("plan", "implement"):
            for version, release in ((2, "0.2.x"), (3, "0.3.x"), (4, "an older version (run format 4)"),
                                     (5, "an older version (run format 5)"), (6, "0.4.x")):
                with self.subTest(flow=flow, version=version):
                    identifier = self.bind_to_old_run(identifier=self.old_id(flow, version), flow=flow,
                                                      version=version)
                    self.assertIsNone(self.tool("Write", file_path=str(self.project / "x.py")))
                    value = runtime.status(self.project, self.session, None)
                    self.assertEqual((value["status"], value["owner_matches"]), ("unsupported", False))
                    self.assertIn(release, value["message"])
                    self.assertIsNone(self.prompt("hello"))
                    self.assertIsNone(self.record())  # Released by the next prompt.
                    self.assertFalse(runtime.status(self.project, None, None)["sessions"]["pending_starts"])
                    self.assertTrue(self.run_dir(flow, identifier).exists())  # Never touched.

    def test_unreleased_and_last_released_formats_are_unsupported(self):
        # Formats 4 and 5 were never released (0.4.0 development), format 3 is 0.3.x,
        # format 6 is 0.4.x and format 7 is 0.5.x: none is continued.
        for version, release in ((3, "0.3.x"), (4, "an older version (run format 4)"),
                                 (5, "an older version (run format 5)"), (6, "0.4.x"), (7, "0.5.x")):
            for flow in ("plan", "implement"):
                with self.subTest(version=version, flow=flow):
                    identifier = self.bind_to_old_run(identifier=self.old_id(flow, version), flow=flow,
                                                      version=version)
                    info = RunStore(self.project, WORKFLOWS[flow]).inspect(identifier)
                    self.assertEqual((info.format, info.schema_version), ("unsupported", version))
                    self.assertEqual(runtime.status(self.project, None, identifier)["message"],
                                     f"Started with DevFlow {release}; this version cannot continue it")
                    # Resuming it is not accepted, and the binding is released anyway.
                    self.assert_not_accepted(self.prompt("/devflow:resume " + identifier),
                                             f"Run {identifier} was started with DevFlow {release}")
                    self.assertIsNone(self.record())
                    # It never blocks a new start, in this session or another.
                    current = self.plan_start(session="new-" + flow + str(version))
                    self.engine("plan").cancel(current)
                    self.assertTrue(self.run_dir(flow, identifier).exists())

    def test_session_bound_to_a_0_2_run_reports_unsupported_until_released(self):
        identifier = self.bind_to_old_run()
        value = runtime.status(self.project, self.session, None)
        self.assertEqual((value["status"], value["owner_matches"]), ("unsupported", False))
        self.assertEqual(value["message"], f"Run {identifier} was started with DevFlow 0.2.x; "
                                           "this version cannot continue it")
        code, output, _ = self.invoke("--session", self.session, "implement", "status")
        self.assertEqual(code, 2)
        self.assertIn("0.2.x", json.loads(output)["message"])
        self.assertIsNone(self.prompt("hello"))
        self.assertIsNone(self.record())  # Released by the next prompt.

    def test_resuming_a_0_2_run_is_not_accepted_and_releases_the_binding(self):
        identifier = self.bind_to_old_run()
        self.assert_not_accepted(self.prompt("/devflow:resume " + identifier), "0.2.x")
        self.assertIsNone(self.record())

    def test_pending_starts_of_0_2_runs(self):
        self.bind_to_old_run(session="someone-else", pending=True)
        self.plan_start()  # Another session's created 0.2.x start never blocks.
        identifier = self.bind_to_old_run(session="third-session", pending=True, identifier=OTHER_ID)
        self.assertIsNone(self.prompt("hi", session="third-session"))
        self.assertIsNone(self.record("third-session"))  # Its own unapplied start is dropped.
        self.assertTrue(self.run_dir("implement", identifier).exists())

    def test_damaged_runs_are_shown_individually_and_block_starts(self):
        for index, (version, extra) in enumerate(((None, {}), (True, {}), ("2", {}), (0, {}), (SCHEMA_VERSION + 1, {}),
                                                  (1, {"run_id": OTHER_ID}), (1, {"flow": "implement"}),
                                                  (1, {"flow": None}), (2, {"run_id": OTHER_ID}),
                                                  (2, {"project": "/elsewhere"}))):
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
                self.assert_not_accepted(self.prompt("/devflow:plan Another request", session="new-session"),
                                         "cannot be read")
                self.assertIsNone(self.record("new-session"))
                path.unlink()
                path.parent.rmdir()

    def test_old_per_workflow_session_directories_do_not_block_starts(self):
        old = self.project / ".devflow" / "sessions"
        (old / "plan").mkdir(parents=True)
        # A 0.1.x per-workflow session record; its content is never read.
        atomic_json(old / "plan" / ("a" * 64 + ".json"), {"run_id": "x"})
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

        with patch.object(sessions, "write_record", side_effect=fail_on_binding):
            self.assert_not_accepted(self.prompt("/devflow:plan Interrupted request"), "simulated stop")
        identifier = self.record()["pending_start"]["run_id"]
        atomic_json(self.run_dir("plan", identifier) / "state.json", {"schema_version": SCHEMA_VERSION, "status": []})
        overview = runtime.status(self.project, "other-session", None)
        self.assertEqual(overview["runs"][0]["format"], "invalid")
        self.assertEqual(overview["sessions"]["pending_starts"], [])
        self.assertIn(f"Run plan/{identifier} cannot be read", " ".join(runtime.blocking_warnings(self.project)))
        probe, interview, claude, which = UnifiedCliTests.doctor_patches(self)
        with probe, interview, claude, which:
            code, output, _ = self.invoke("doctor")
        self.assertEqual(code, 0, output)

    def test_oversized_start_prompt_is_not_accepted_before_anything_is_written(self):
        self.assert_not_accepted(self.prompt("/devflow:plan " + "x" * (4 * 1024 * 1024)), "4 MiB")
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
            self.assert_not_accepted(self.prompt("/devflow:plan Interrupted request"), "simulated stop")
        pending = self.record()["pending_start"]
        self.assertTrue(self.run_dir("plan", pending["run_id"]).is_dir())
        self.assertIsNone(self.tool(file_path=str(self.project / "x.md")))  # Pending: nothing stopped or recorded.
        self.prompt("first answer")
        self.assertEqual(self.record()["run"], {"flow": "plan", "run_id": pending["run_id"]})

    def test_failed_in_hook_creation_clears_its_pending_start(self):
        atomic_json(self.project / "devflow.config.json", {"schema_version": 1, "plan": {"max_reviews": 7}})
        self.assert_not_accepted(self.prompt("/devflow:plan Request with an invalid configuration"), "max_reviews")
        self.assertIsNone(self.record())
        self.assertEqual(runtime.status(self.project, "other", None)["sessions"]["pending_starts"], [])

    def test_start_killed_before_creation_is_abandoned_on_the_next_prompt(self):
        # A killed process leaves its pending start behind (no cleanup runs).
        pending = {"flow": "plan", "run_id": "20261101T123456Z-aaaaaaaaaaaa", "args": "Interrupted request",
                   "created_at": "2026-11-01T00:00:00+00:00",
                   "event": make_event(self.payload(prompt="/devflow:plan Interrupted request"), "prompt")}
        (self.project / ".devflow").mkdir()
        sessions.write_record(self.project, dict(sessions.empty_record(self.session), pending_start=pending), WORKFLOWS)
        self.assert_not_accepted(self.prompt("/devflow:plan Another request", session="other-session"),
                                 "pending plan start")
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
            self.assert_not_accepted(self.prompt("/devflow:resume " + identifier, session="new-session"),
                                     "simulated stop")
        self.assertEqual(self.record("new-session")["run"]["run_id"], identifier)
        self.assertEqual(self.engine("implement").status(identifier)["owner_session"], self.session)
        # Not the owner yet: no tool is stopped and nothing is recorded.
        self.assertIsNone(self.tool(file_path=str(self.project / "x.py"), session="new-session"))
        context = self.prompt("continue", session="new-session")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("/devflow:resume " + identifier, context)
        self.assertIsNone(self.tool("Read", file_path=str(self.plan_file)))  # The original owner keeps working.
        self.prompt("/devflow:resume " + identifier, session="new-session")
        self.assertEqual(self.engine("implement").status(identifier)["owner_session"], "new-session")

    def test_same_session_resume_interrupted_keeps_ownership(self):
        identifier = self.implement_start()
        with patch.object(RunStore, "transfer", side_effect=OSError("simulated stop")):
            self.assert_not_accepted(self.prompt("/devflow:resume " + identifier), "simulated stop")
        self.assertEqual(self.record()["run"]["run_id"], identifier)
        self.assertIsNone(self.tool("Read", file_path=str(self.plan_file)))

    def test_resume_is_exclusive(self):
        finished = self.plan_start()
        self.engine("plan").cancel(finished)
        self.prompt("next")
        active = self.implement_start()
        self.assert_not_accepted(self.prompt("/devflow:resume " + finished, session="other-session"),
                                 "unfinished implement")
        self.assert_not_accepted(self.prompt("/devflow:resume " + finished), "Finish or cancel")
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
                self.assert_not_accepted(self.prompt("/devflow:plan New request"), "invalid")
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
        events = len(read_json(self.run_dir("implement", identifier) / "state.json")["event_ids"])
        context = self.prompt("What does the plan say?")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("no longer owns DevFlow run " + identifier, context)
        self.assertEqual(len(read_json(self.run_dir("implement", identifier) / "state.json")["event_ids"]), events)
        # A stale session is never stopped and never recorded, not even its questions or stops.
        self.assertIsNone(self.tool("Read", file_path=str(self.plan_file)))
        self.assertIsNone(self.tool("Bash", command="grep -rn VALUE ."))
        self.assertIsNone(self.tool("Write", file_path=str(self.project / "x.py")))
        self.assertIsNone(self.ask("stale-q", questions=[{"question": "q?"}]))
        self.assertIsNone(runtime.handle_hook(self.payload(event="PostToolUse", tool_name="AskUserQuestion",
                                                           tool_use_id="stale-q", tool_input={"questions": []},
                                                           tool_response="Yes"), PLUGIN))
        self.assertIsNone(runtime.handle_hook(self.payload(event="Stop"), PLUGIN))
        self.assertEqual(len(read_json(self.run_dir("implement", identifier) / "state.json")["event_ids"]), events)


class ProjectResolutionTests(RuntimeFixture):
    """Hooks find the project from CLAUDE_PROJECT_DIR (the session's start directory)."""

    def hook_from(self, cwd, environ, event="UserPromptSubmit", **fields):
        payload = self.payload(event=event, **fields)
        payload["cwd"] = str(cwd)
        return runtime.handle_hook(payload, PLUGIN, environ)

    def elsewhere(self):
        other = self.base / "another repository"
        (other / ".git").mkdir(parents=True, exist_ok=True)
        return other

    def recorded(self, identifier):
        return len(read_json(self.run_dir("plan", identifier) / "state.json")["event_ids"])

    def test_the_project_directory_wins_over_a_cwd_in_another_repository(self):
        identifier = self.plan_start()
        before = self.recorded(identifier)
        context = self.hook_from(self.elsewhere(), {"CLAUDE_PROJECT_DIR": str(self.project)},
                                 prompt="Continue planning")
        self.assertIn("recorded_event_id", context["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.recorded(identifier), before + 1)
        # A question asked from that cwd is recorded in the project's run too.
        self.assertIsNone(self.hook_from(self.elsewhere(), {"CLAUDE_PROJECT_DIR": str(self.project)},
                                         event="PreToolUse", tool_name="AskUserQuestion", tool_use_id="q-elsewhere",
                                         tool_input={"questions": [{"question": "Scope?"}]}))
        self.assertEqual(self.recorded(identifier), before + 2)
        # Without the variable, the foreign cwd is a different (inert) project.
        self.assertIsNone(self.hook_from(self.elsewhere(), {}, prompt="Continue planning"))

    def test_unset_or_empty_falls_back_to_the_cwd(self):
        identifier = self.plan_start()
        for environ in ({}, {"CLAUDE_PROJECT_DIR": ""}):
            with self.subTest(environ=environ):
                before = self.recorded(identifier)
                self.assertIsNotNone(self.hook_from(self.project, environ, prompt="More detail"))
                self.assertEqual(self.recorded(identifier), before + 1)

    def test_an_invalid_value_is_an_error(self):
        afile = self.base / "a file"
        afile.write_text("x", encoding="utf-8")
        for value in ("relative/project", str(self.base / "missing"), str(afile)):
            with self.subTest(value=value), self.assertRaisesRegex(DevFlowError, "CLAUDE_PROJECT_DIR"):
                self.hook_from(self.project, {"CLAUDE_PROJECT_DIR": value}, prompt="hello")

    def test_a_subdirectory_resolves_to_the_project_root(self):
        identifier = self.plan_start()
        sub = self.project / "src" / "deep"
        sub.mkdir(parents=True)
        before = self.recorded(identifier)
        self.assertIsNotNone(self.hook_from(sub, {"CLAUDE_PROJECT_DIR": str(sub)}, prompt="Look here"))
        self.assertEqual(self.recorded(identifier), before + 1)

    def test_a_hook_that_cannot_find_its_project_or_session_record_exits_1_and_stops_nothing(self):
        self.plan_start()
        record = next((self.project / ".devflow" / "sessions").glob("*.json"))
        original = record.read_bytes()

        def run_hook(env: dict) -> subprocess.CompletedProcess:
            payload = json.dumps(dict(self.payload(prompt="Hello"), cwd=str(self.project)))
            return subprocess.run([sys.executable, "-B", str(SCRIPT), "hook"], input=payload, capture_output=True,
                                  encoding="utf-8", timeout=30, shell=False, env=dict(os.environ, **env))

        cases = (("a broken session record", {"CLAUDE_PROJECT_DIR": str(self.project)}, b"{", record.name),
                 ("a relative CLAUDE_PROJECT_DIR", {"CLAUDE_PROJECT_DIR": "relative/dir"}, None,
                  "CLAUDE_PROJECT_DIR must be an absolute directory"),
                 ("a missing CLAUDE_PROJECT_DIR", {"CLAUDE_PROJECT_DIR": str(self.project / "missing")}, None,
                  "CLAUDE_PROJECT_DIR does not name an existing directory"))
        for name, env, damaged, reason in cases:
            with self.subTest(case=name):
                record.write_bytes(damaged if damaged is not None else original)
                try:
                    result = run_hook(env)
                finally:
                    record.write_bytes(original)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn("DevFlow hook failed", result.stderr)
                self.assertIn(reason, result.stderr)
        # Each case starts from the intact record: the same hook succeeds with it.
        self.assertEqual(run_hook({"CLAUDE_PROJECT_DIR": str(self.project)}).returncode, 0)

    def test_the_real_hook_entry_point_reads_the_environment(self):
        identifier = self.plan_start()
        payload = json.dumps(dict(self.payload(prompt="From a worktree"), cwd=str(self.elsewhere())))
        env = dict(os.environ, CLAUDE_PROJECT_DIR=str(self.project))
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), "hook"], input=payload, capture_output=True,
                                encoding="utf-8", timeout=30, shell=False, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("recorded_event_id", json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"])
        self.assertTrue(self.recorded(identifier) >= 2)


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

    def test_real_hook_failure_exits_1_never_2(self):
        # Exit status 2 would make Claude Code block the tool or the prompt.
        for payload in ("not json", "[]", json.dumps({"hook_event_name": "PreToolUse"}),
                        json.dumps(self.payload(prompt=None)),
                        json.dumps(self.payload(event="PreToolUse", session="../bad session", tool_name="Write"))):
            with self.subTest(payload=payload):
                failed = self.process("hook", payload=payload)
                self.assertEqual((failed.returncode, failed.stdout), (1, ""))
                self.assertIn("DevFlow hook failed", failed.stderr)

    def test_in_process_hook_failure_exits_1_never_2(self):
        for payload in ("{", json.dumps({"hook_event_name": "PostToolUse"}),
                        json.dumps({"hook_event_name": "UserPromptSubmit", "cwd": 3})):
            with self.subTest(payload=payload):
                out, err = io.StringIO(), io.StringIO()
                with patch.object(sys, "stdin", io.StringIO(payload)), redirect_stdout(out), redirect_stderr(err):
                    code = cli.main(["hook"])
                self.assertEqual((code, out.getvalue()), (1, ""))
                self.assertIn("DevFlow hook failed", err.getvalue())

    def test_an_event_the_hook_does_not_handle_is_silent(self):
        for payload in ({"hook_event_name": "Stop"}, {"hook_event_name": "Stop", "cwd": 3},
                        {"hook_event_name": "SessionStart"}):
            with self.subTest(payload=payload):
                result = self.process("hook", payload=json.dumps(payload))
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertFalse((self.project / ".devflow").exists())

    def test_real_hook_reports_a_command_error_as_context_with_exit_0(self):
        result = self.process("hook", payload=json.dumps(self.payload(prompt="/devflow:implement")))
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertIn("DevFlow did not accept /devflow:implement",
                      json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"])
        self.assertFalse((self.project / ".devflow").exists())

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
                patch.object(cli, "claude_version", return_value={"supported": supported, "version": "2.1.316"}),
                patch.object(cli.shutil, "which", side_effect=which or (lambda name: "/synthetic/available")))

    def test_doctor_is_offline_and_reports_minimum_supported_claude(self):
        probe, interview, claude, which = self.doctor_patches()
        with probe as preflight, interview, claude, which:
            code, output, error = self.invoke("doctor")
        self.assertEqual((code, error), (0, ""))
        report = json.loads(output)
        self.assertEqual(report["minimum_claude_code"], "2.1.316")
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
        result = subprocess.CompletedProcess(["claude", "--version"], 0, "2.1.316 (Claude Code)\n", "")
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
            statuses = frozenset({"working", "completed", "exhausted", "cancelled", "accepted"})

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
