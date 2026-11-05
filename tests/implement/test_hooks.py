"""Offline, cross-platform tests of the implement workflow's session-scoped hooks.

DevFlow never stops a tool call; the hooks record human events and add
context. Every test runs real hooks through the router against real runs
started the documented way (prompt hook, then the runner's start).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

from devflow_core import router
from devflow_core.common import sessions
from devflow_core.common.errors import DevFlowError
from devflow_core.common.runs import TERMINAL, RunStore
from devflow_core.common.storage import atomic_write_json, read_json
from devflow_core.implement import WORKFLOW
from devflow_core.implement.engine import Engine
from devflow_core.workflows import WORKFLOWS

PACKAGE = Path(__file__).resolve().parents[2] / "plugins" / "devflow" / "scripts" / "devflow_core" / "implement"
RUN_ID = "20261101T123456Z-123456abcdef"
EVENT = "a" * 32


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="devflow implement hooks ")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.project = self.base / "project with spaces"
        self.project.mkdir()
        (self.project / "devflow.config.json").write_text(
            '{"schema_version":1,"implement":{"snapshot":{"mode":"files"}}}\n', encoding="utf-8",
        )
        # A fake installed plugin: the hook context names its runner path.
        self.plugin = self.base / "plugin with spaces"
        self.script = self.plugin / "scripts" / "devflow.py"
        self.script.parent.mkdir(parents=True)
        self.script.write_text("# trusted runner\n", encoding="utf-8")
        self.plan = self.project / "feature.md"
        self.plan.write_text("Implement a tested feature.\n", encoding="utf-8")
        self.session_id = "claude-session-123"
        self.run = self.project / ".devflow" / "runs" / "implement" / RUN_ID
        self.draft = self.run / "draft"

    def payload(self, event="PreToolUse", tool="Read", **tool_input):
        return {
            "hook_event_name": event, "cwd": str(self.project), "session_id": self.session_id,
            "tool_name": tool, "tool_input": tool_input,
        }

    def runner(self, *arguments, flow="implement"):
        arguments = list(arguments)
        selectors = []
        while arguments and arguments[0] in {"--run", "--session", "--project"}:
            selectors += arguments[:2]
            arguments = arguments[2:]
        return " ".join(shlex.quote(value) for value in [
            "uv", "run", "--offline", "--no-project", "--no-config", "--no-python-downloads",
            str(self.script), "--project", str(self.project), "--session", self.session_id,
            *selectors, *([flow] if flow else []), *arguments,
        ])

    # -- real hooks and runs --------------------------------------------------------------
    def hook(self, payload):
        return router.handle_hook(payload, self.plugin)

    def submit(self, prompt, session=None):
        return self.hook({"hook_event_name": "UserPromptSubmit", "cwd": str(self.project),
                          "session_id": session or self.session_id, "prompt": prompt})

    def tool(self, tool="Write", session=None, **tool_input):
        payload = self.payload(tool=tool, **tool_input)
        payload["session_id"] = session or self.session_id
        return self.hook(payload)

    def record(self, session=None):
        path = sessions.session_path(self.project, session or self.session_id)
        return read_json(path) if path.exists() else None

    def bind(self, session=None, submit=True):
        """Start a real run for the session; point the fixture paths at it."""
        identity = session or self.session_id
        if submit:
            self.submit("/devflow:implement feature.md", identity)
        status = router.runner_start(self.project, identity, "implement", "feature.md")
        self.run_id = status["run_id"]
        self.run = Path(status["run_dir"])
        self.draft = Path(status["draft_dir"])
        self.store = RunStore(self.project, WORKFLOW)
        return status

    def stored(self):
        return self.store.read(self.run_id)

    def events(self):
        return self.store.events(self.stored())

    def set_status(self, status):
        """Fixture-only state change through the run store's own save."""
        with self.store.lock(self.run_id):
            state = self.store.load(self.run_id)
            state["status"] = status
            state["inflight"] = ({"kind": "check", "number": 1, "reviewer_id": None, "target": "fixture",
                                  "started_at": "2026-11-01T12:34:56+00:00"}
                                 if status in WORKFLOW.inflight_statuses else None)
            self.store.save(state)

    def assertUnrecorded(self, result, reason):
        """DevFlow never blocks: a call it cannot record goes through with a warning."""
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("was not recorded", context)
        self.assertIn(reason, context)

    def assertNotAccepted(self, result, reason=""):
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("did not accept", context)
        self.assertIn(reason, context)
        return context

    def escalate(self):
        path = self.draft / "questions.json"
        atomic_write_json(path, {"reason": "A product decision is required.", "questions": ["Continue?"]})
        return Engine(self.project).escalate(self.run_id, self.session_id, path)


class ToolCallTests(Fixture):
    """DevFlow never stops a tool call: the session's own settings decide (decision D1)."""

    TOOLS = (
        ("Write", lambda self: {"file_path": str(self.project / "src" / "feature.py")}),
        ("Write", lambda self: {"file_path": str(self.run / "state.json")}),
        ("Write", lambda self: {"file_path": str(self.plan)}),
        ("Edit", lambda self: {"file_path": str(self.project / "CLAUDE.md")}),
        ("Bash", lambda self: {"command": "rm -rf build"}),
        ("Bash", lambda self: {"command": "pnpm test", "dangerouslyDisableSandbox": True}),
        ("Bash", lambda self: {"command": "git commit -am wip", "run_in_background": True}),
        ("Bash", lambda self: {"command": self.runner("--run", self.run_id, "recover", "--confirm-process-stopped")}),
        ("PowerShell", lambda self: {"command": "Get-ChildItem"}),
        ("Agent", lambda self: {"subagent_type": "general-purpose", "prompt": "x"}),
        ("Skill", lambda self: {"skill": "anything"}),
        ("mcp__files__write", lambda self: {"path": "x"}),
        ("EnterWorktree", lambda self: {}),
        ("Monitor", lambda self: {"command": "ls"}),
        ("Unknown", lambda self: {}),
    )

    def every_tool_goes_through(self, session=None):
        for tool, data in self.TOOLS:
            with self.subTest(tool=tool, data=data(self)):
                self.assertIsNone(self.tool(tool, session, **data(self)))

    def test_pending_start_stops_nothing_and_records_nothing(self):
        self.submit("/devflow:implement feature.md")
        self.run_id = self.record()["pending_start"]["run_id"]
        self.every_tool_goes_through()
        self.assertIsNone(self.tool("AskUserQuestion", questions=[{"question": "Q?"}]))
        self.assertIsNone(self.tool("Bash", command=self.runner("start", "--plan", "other.md")))
        # No run exists yet, so nothing can be recorded.
        self.assertFalse((self.project / ".devflow" / "runs" / "implement" / self.run_id).exists())

    def test_owner_is_never_stopped_in_any_status(self):
        self.bind()
        events = self.stored()["event_ids"]
        for status in sorted(WORKFLOW.statuses):
            self.set_status(status)
            with self.subTest(status=status):
                self.every_tool_goes_through()
        self.assertEqual(self.stored()["event_ids"], events)  # Nothing above was recorded.
        self.assertTrue(TERMINAL <= WORKFLOW.statuses)

    def test_a_damaged_run_never_blocks_ordinary_calls(self):
        # Review finding: an allowed write could empty state.json; ordinary calls
        # must not read the run at all, so they keep working.
        self.bind()
        (self.run / "state.json").write_text("{}", encoding="utf-8")
        with patch.object(sessions, "binding", side_effect=AssertionError("the run must not be read")):
            self.every_tool_goes_through()
            self.assertIsNone(self.tool("Read", file_path=str(self.plan)))
        # A question needs the run (its owner and its record). If the run cannot
        # be read, the hook reports it (the CLI exits 1, which does not stop the
        # tool); without that record its answer can never count.
        for extra in ({}, {"answers": {"Q?": "Yes"}}):
            payload = self.payload(tool="AskUserQuestion", questions=[{"question": "Q?"}], **extra)
            payload["tool_use_id"] = "question-on-damaged-run"
            with self.subTest(extra=extra), self.assertRaisesRegex(DevFlowError, "cannot be read"):
                self.hook(payload)
        # The human's prompt goes through with a warning; nothing is recorded.
        context = self.submit("Keep going")["hookSpecificOutput"]["additionalContext"]
        self.assertIn(f"implement run {self.run_id}", context)
        self.assertIn("this prompt was not recorded", context)
        # An answer is not recorded and says so; a stop is silent.
        answer = self.payload(event="PostToolUse", tool="AskUserQuestion", questions=[{"question": "Q?"}])
        answer.update(tool_use_id="answer-on-damaged-run", tool_response={"answers": {"Q?": "Yes"}})
        context = self.hook(answer)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("cannot resolve a DevFlow escalation", context)
        self.assertIsNone(self.hook({"hook_event_name": "Stop", "cwd": str(self.project),
                                     "session_id": self.session_id}))
        # DevFlow commands still say why they cannot act on an unreadable run.
        for command in (f"/devflow:resume {self.run_id}", "/devflow:implement feature.md"):
            with self.subTest(command=command):
                self.assertNotAccepted(self.submit(command), "cannot be read")
        self.assertEqual(self.record()["run"]["run_id"], self.run_id)  # The binding is kept.

    def test_status_context_does_not_announce_a_bash_allowlist(self):
        self.bind()
        context = self.submit("Continue")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("DevFlow does not limit any other tool", context)
        self.assertNotIn("allowed commands", context)
        self.assertIn('"workspace": {"path": ', context)
        self.assertIn(f'"project": {json.dumps(str(self.project))}', context)

    def test_prefilled_question_is_recorded_as_an_observation(self):
        self.bind()
        payload = self.payload(tool="AskUserQuestion", questions=[{"question": "Approve?"}],
                               answers={"Approve?": "Yes"})
        payload["tool_use_id"] = "prefilled-1"
        self.assertIsNone(self.hook(payload))
        self.assertIsNone(self.hook(payload))  # A hook retry is one event.
        events = self.events()
        self.assertEqual([event["kind"] for event in events], ["prompt", "observation"])
        self.assertEqual(events[-1]["payload"]["tool_input"]["answers"], {"Approve?": "Yes"})
        annotated = self.payload(tool="AskUserQuestion", questions=[{"question": "Q?"}],
                                 annotations={"Q?": {"notes": "Approved"}})
        annotated["tool_use_id"] = "prefilled-2"
        self.assertIsNone(self.hook(annotated))
        self.assertEqual(self.events()[-1]["kind"], "observation")
        changed = dict(payload, tool_input={"questions": [{"question": "Approve?"}], "answers": {"Approve?": "No"}})
        with self.assertRaisesRegex(DevFlowError, "different content"):
            self.hook(changed)
        # Both were prefilled by the model; status counts only those.
        self.assertEqual(Engine(self.project).status(self.run_id)["prefilled_questions"], 2)

    def test_pending_resume_is_bound_to_user_selected_run(self):
        # The user's /devflow:resume names the run; the hook moves exactly that run.
        status = self.bind()
        other = "claude-session-456"
        self.assertNotAccepted(self.submit("/devflow:resume " + RUN_ID, other), "missing or ambiguous")
        self.assertIsNone(self.record(other))
        self.assertIsNotNone(self.submit("/devflow:resume " + status["run_id"], other))
        self.assertEqual(self.record(other)["run"], {"flow": "implement", "run_id": status["run_id"]})
        self.assertEqual(self.stored()["owner_session"], other)
        self.assertIsNone(self.tool("Bash", other, command=self.runner("status").replace(self.session_id, other)))


class SessionCaptureTests(Fixture):
    def test_unrelated_session_is_inert_and_creates_no_directory(self):
        self.assertIsNone(self.submit("Please explain this code"))
        self.assertIsNone(self.submit("/" + "plan" + "flow:plan-review a feature"))
        self.assertIsNone(self.submit("/devflow:implementation feature.md"))
        self.assertIsNone(self.hook(self.payload(tool="Bash", command="anything")))
        self.assertIsNone(self.hook(self.payload(event="PostToolUse", tool="AskUserQuestion")))
        self.assertIsNone(self.hook(self.payload(event="Stop")))
        self.assertFalse((self.project / ".devflow").exists())

    def test_explicit_start_records_exact_user_text_and_intent(self):
        prompt = '/devflow:implement "feature.md"'
        output = self.submit(prompt)
        stored = self.record()
        self.assertEqual(stored["schema_version"], 2)
        self.assertIsNone(stored["run"])
        pending = stored["pending_start"]
        self.assertEqual(pending["flow"], "implement")
        self.assertEqual(pending["args"], str(self.plan))
        self.assertEqual(pending["event"]["kind"], "prompt")
        self.assertEqual(pending["event"]["payload"]["prompt"], prompt)
        self.assertRegex(pending["event"]["event_id"], r"\A[a-f0-9]{32}\Z")
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn(str(self.plan), context)
        # A resumed conversation may still hold an older plugin path.
        self.assertIn(str(self.script), context)
        self.assertIn("*", (self.project / ".devflow" / ".gitignore").read_text().splitlines())
        self.assertFalse((self.project / ".devflow" / "runs" / "implement" / pending["run_id"]).exists())
        # No tool is stopped; before the start nothing is recorded either.
        self.assertIsNone(self.hook(self.payload(tool="Write", file_path=str(self.project / "x.py"))))
        self.assertIsNone(self.hook(self.payload(tool="AskUserQuestion", questions=[])))
        # The run created by the runner keeps the hook's original start event.
        router.runner_start(self.project, self.session_id, "implement", "feature.md")
        self.store = RunStore(self.project, WORKFLOW)
        self.run_id = pending["run_id"]
        self.assertEqual(self.stored()["event_ids"], [pending["event"]["event_id"]])
        self.assertEqual(self.events()[0], pending["event"])
        self.assertEqual(self.record()["run"], {"flow": "implement", "run_id": pending["run_id"]})

    def test_plan_path_with_spaces_and_native_separators_preserved(self):
        self.submit('/devflow:implement "C:\\projects\\my plan.md"')
        self.assertEqual(self.record()["pending_start"]["args"],
                         os.path.normpath(str(self.project / "C:\\projects\\my plan.md")))
        self.assertIn("my plan.md", self.record()["pending_start"]["args"])

    def test_claude_file_mention_prefix_is_normalized(self):
        self.submit("/devflow:implement @feature.md")
        self.assertEqual(self.record()["pending_start"]["args"], str(self.plan))
        self.submit("/devflow:implement ./feature.md")
        self.assertEqual(self.record()["pending_start"]["args"], str(self.plan))

    def test_missing_or_malformed_start_is_not_accepted(self):
        for prompt, reason in (("/devflow:implement", "one plan-file path"),
                               ("/devflow:implement --plan other", "not runner options"),
                               ('/devflow:implement "unterminated', "unmatched quotes"),
                               ("/devflow:implement foo\n/other:skill", "one plan-file path")):
            with self.subTest(prompt=prompt):
                self.assertNotAccepted(self.submit(prompt), reason)
        self.assertFalse((self.project / ".devflow").exists())

    def test_resume_records_safe_run_id(self):
        status = self.bind()
        other = "claude-session-456"
        self.submit("/devflow:resume " + status["run_id"], other)
        self.assertEqual(self.record(other), {"schema_version": 2, "session_id": other,
                                              "run": {"flow": "implement", "run_id": status["run_id"]},
                                              "pending_start": None})
        self.assertEqual(self.events()[-1]["payload"]["prompt"], "/devflow:resume " + status["run_id"])
        before = self.stored()
        for argument in ("../other", RUN_ID + "/..", ""):
            with self.subTest(argument=argument):
                self.assertNotAccepted(self.submit("/devflow:resume " + argument, "claude-session-789"))
        self.assertIsNone(self.record("claude-session-789"))
        self.assertEqual(self.stored(), before)

    def test_next_real_prompt_can_abandon_pending_start(self):
        self.submit("/devflow:implement feature.md")
        self.assertIsNotNone(self.record()["pending_start"])
        self.assertIsNone(self.submit("Cancel that; explain instead"))
        self.assertIsNone(self.record())
        self.assertIsNone(self.hook(self.payload(tool="Bash", command="normal command")))
        with self.assertRaises(DevFlowError):
            router.runner_start(self.project, self.session_id, "implement", "feature.md")

    def test_draft_and_source_writes_are_never_stopped_before_or_after_start(self):
        self.submit("/devflow:implement feature.md")
        pending = self.record()["pending_start"]
        draft = self.project / ".devflow" / "runs" / "implement" / pending["run_id"] / "draft" / "decisions.json"
        source = self.project / "src" / "feature.py"
        self.assertIsNone(self.tool("Write", file_path=str(draft)))
        self.assertIsNone(self.tool("Write", file_path=str(source)))
        self.bind(submit=False)
        self.assertEqual(self.draft, draft.parent)
        for status in sorted(WORKFLOW.statuses):
            self.set_status(status)
            with self.subTest(status=status):
                self.assertIsNone(self.tool("Write", file_path=str(draft)))
                self.assertIsNone(self.tool("Write", file_path=str(source)))

    def test_active_session_other_session_remains_unaffected(self):
        self.bind()
        payload = self.payload(tool="Bash", command="normal command")
        payload["session_id"] = "another-session"
        self.assertIsNone(self.hook(payload))
        self.assertIsNone(self.submit("An unrelated prompt", "another-session"))
        self.assertFalse(sessions.session_path(self.project, "another-session").exists())
        self.assertEqual(len(self.stored()["event_ids"]), 1)

    def test_terminal_state_not_released_until_next_real_prompt(self):
        self.bind()
        self.set_status("completed")
        before = self.payload(tool="Write", file_path=str(self.project / "x.py"))
        self.assertIsNone(self.hook(before))
        self.assertEqual(self.record()["run"], {"flow": "implement", "run_id": self.run_id})
        events = self.stored()["event_ids"]
        self.assertIsNone(self.submit("Now explain the implementation"))
        self.assertIsNone(self.record())
        # The prompt that releases a finished run is not recorded into it.
        self.assertEqual(self.stored()["event_ids"], events)
        self.assertIsNone(self.hook(before))

    def test_running_flow_cannot_be_replaced_by_new_start(self):
        self.bind()
        (self.project / "other.md").write_text("Other plan.\n", encoding="utf-8")
        self.assertNotAccepted(self.submit("/devflow:implement other.md"), "Finish or cancel")
        self.assertEqual(self.record()["run"], {"flow": "implement", "run_id": self.run_id})
        self.assertIsNone(self.record()["pending_start"])

    def test_same_running_flow_can_receive_resume_intent(self):
        self.bind()
        self.assertIsNotNone(self.submit("/devflow:resume " + self.run_id))
        self.assertEqual(self.record()["run"], {"flow": "implement", "run_id": self.run_id})
        self.assertEqual(self.stored()["owner_session"], self.session_id)
        self.assertEqual(self.events()[-1]["payload"]["prompt"], "/devflow:resume " + self.run_id)

    def test_user_response_retains_original_text(self):
        self.bind()
        self.escalate()
        original = "既存 API は変更せず、互換レイヤーを追加してください。"
        output = self.submit(original)
        event = self.events()[-1]
        self.assertEqual(event["payload"]["prompt"], original)
        self.assertEqual(event["kind"], "prompt")
        self.assertEqual(len(self.stored()["event_ids"]), 2)
        self.assertIn(event["event_id"], output["hookSpecificOutput"]["additionalContext"])

    def question(self, tool_use_id="tool-123"):
        payload = self.payload(event="PostToolUse", tool="AskUserQuestion", questions=[{"question": "Continue?"}])
        payload.update({"tool_use_id": tool_use_id, "tool_response": {"answers": {"Continue?": "Yes, keep compatibility"}}})
        return payload

    def answer(self, payload):
        """The human's answer as Claude Code delivers it: PreToolUse (the question as
        Claude sent it, without the screen's fields), then PostToolUse."""
        asked = {key: value for key, value in payload["tool_input"].items()
                 if key not in sessions.SCREEN_FILLED_QUESTION_FIELDS}
        before = dict(payload, hook_event_name="PreToolUse", tool_input=asked)
        for key in ("tool_response", "is_error"):
            before.pop(key, None)
        self.assertIsNone(self.hook(before))
        return self.hook(payload)

    def candidates(self):
        return Engine(self.project).status(self.run_id)["human_events"]

    def test_question_response_is_stored_verbatim_and_deduplicated(self):
        self.bind()
        payload = self.question()
        first = self.hook(payload)
        second = self.hook(payload)
        self.assertEqual(first, second)
        self.assertEqual(len(self.stored()["event_ids"]), 2)
        event = self.events()[-1]
        self.assertEqual(event["kind"], "question")
        self.assertEqual(event["payload"]["tool_response"], payload["tool_response"])
        self.assertEqual(event["payload"]["tool_input"], payload["tool_input"])
        self.assertIn(event["event_id"], first["hookSpecificOutput"]["additionalContext"])

    def test_question_replay_with_changed_result_rejected(self):
        self.bind()
        payload = self.question()
        self.hook(payload)
        payload["tool_response"] = "different answer"
        self.assertUnrecorded(self.hook(payload), "different content")
        self.assertEqual(len(self.stored()["event_ids"]), 2)

    def test_answers_filled_in_by_question_screen_are_recorded(self):
        # Claude Code's question screen writes the human's choice into
        # tool_input before PostToolUse; model prefills are recorded at
        # PreToolUse as observations (see ToolCallTests) and never count.
        self.bind()
        self.escalate()
        payload = self.question()
        payload["tool_input"].update({"answers": {"Continue?": "Yes, keep compatibility"}, "annotations": {}})
        self.answer(payload)
        self.assertEqual([event["kind"] for event in self.events()], ["prompt", "observation", "question"])
        self.assertEqual(self.events()[-1]["payload"]["tool_input"], payload["tool_input"])
        self.assertEqual([item["event_id"] for item in self.candidates()], [self.stored()["event_ids"][-1]])

    def prefill(self, tool_use_id):
        payload = self.payload(tool="AskUserQuestion", questions=[{"question": "Continue?"}],
                               answers={"Continue?": "Yes, keep compatibility"})
        payload["tool_use_id"] = tool_use_id
        self.assertIsNone(self.hook(payload))

    def test_prefilled_question_never_becomes_an_answer(self):
        self.bind()
        self.escalate()
        self.prefill("tool-123")
        payload = self.question("tool-123")
        payload["tool_input"]["answers"] = {"Continue?": "Yes, keep compatibility"}
        self.hook(payload)
        self.assertEqual([event["kind"] for event in self.events()][-2:], ["observation", "question"])
        self.assertEqual(self.candidates(), [])
        with self.assertRaisesRegex(DevFlowError, "event ID of a successful AskUserQuestion"):
            Engine(self.project).resolve(self.run_id, self.session_id, self.events()[-1]["event_id"],
                                         "continue", "The model answered itself")

    def test_prefill_recorded_before_the_escalation_still_excludes_the_answer(self):
        self.bind()
        self.prefill("tool-early")
        self.escalate()
        self.hook(self.question("tool-early"))
        self.assertEqual(self.candidates(), [])
        self.answer(self.question("tool-honest"))
        self.assertEqual([item["event_id"] for item in self.candidates()], [self.stored()["event_ids"][-1]])

    def test_subagent_answer_never_counts(self):
        self.bind()
        self.escalate()
        payload = self.question("tool-subagent")
        payload.update({"agent_id": "agent-1", "agent_type": "Explore"})
        self.answer(payload)
        self.assertEqual(self.events()[-1]["kind"], "question")
        self.assertEqual(self.candidates(), [])

    def test_empty_cancelled_or_failed_question_is_not_an_answer(self):
        # Failed or empty questions are kept as evidence of what happened,
        # but never become an answer that can resolve an escalation.
        self.bind()
        self.escalate()
        responses = (None, "", "   ", [], {}, {"answers": {}},
                     {"cancelled": True, "answers": {"Continue?": "yes"}},
                     {"is_error": True}, {"answers": {"Continue?": ""}})
        for index, response in enumerate(responses):
            payload = self.question(f"tool-{index}")
            payload["tool_response"] = response
            with self.subTest(response=response):
                self.answer(payload)
                self.assertEqual(self.events()[-1]["payload"]["tool_response"], response)
                self.assertEqual(self.candidates(), [])
        self.assertEqual(len(self.stored()["event_ids"]), 1 + 2 * len(responses))  # Observation and answer.
        for event_id in self.stored()["event_ids"][1:]:
            with self.assertRaises(DevFlowError):
                Engine(self.project).resolve(self.run_id, self.session_id, event_id, "continue", "Not an answer.")
        self.answer(dict(self.question("tool-text"), tool_response="Yes, keep compatibility"))
        self.assertEqual([item["answer_text"] for item in self.candidates()], ["Yes, keep compatibility"])

    def test_top_level_question_error_cannot_capture_or_replay_an_answer(self):
        self.bind()
        self.escalate()
        failed = dict(self.question("failed-call"), is_error=True)
        self.answer(failed)
        self.assertEqual(self.candidates(), [])
        # A failed call cannot be replayed as a successful answer.
        succeeded = dict(failed)
        succeeded.pop("is_error")
        self.assertUnrecorded(self.hook(succeeded), "different content")
        self.assertEqual(self.candidates(), [])
        # Nor can a successful answer be replayed as a failure.
        answered = self.question("answered-call")
        self.answer(answered)
        self.assertUnrecorded(self.hook(dict(answered, is_error=True)), "different content")
        self.assertEqual(len(self.candidates()), 1)
        self.assertEqual(len(self.stored()["event_ids"]), 5)  # The prompt, then an observation and answer each.

    def test_nonempty_question_text_blocks_preserved(self):
        self.bind()
        payload = self.question()
        payload["tool_response"] = [{"type": "text", "text": "Keep the existing API."}]
        self.hook(payload)
        self.assertEqual(self.events()[-1]["payload"]["tool_response"], payload["tool_response"])

    def test_oversize_event_not_silently_truncated(self):
        self.bind()
        with patch("devflow_core.common.runs.MAX_EVENT_BYTES", 30):
            self.assertUnrecorded(self.submit("A message beyond the configured capture budget"), "exceeds")
        self.assertEqual(len(self.stored()["event_ids"]), 1)

    def test_stored_run_identity_must_match_session(self):
        self.bind()
        # Another session explicitly resumes the run; this session's pointer goes stale.
        self.submit("/devflow:resume " + self.run_id, "claude-session-456")
        self.assertEqual(self.stored()["owner_session"], "claude-session-456")
        # The stale session is neither stopped nor recorded: PreToolUse,
        # PostToolUse and Stop all return None.
        self.assertIsNone(self.hook(self.payload()))
        self.assertIsNone(self.hook(self.payload(tool="Write", file_path=str(self.project / "x.py"))))
        context = self.submit("Continue the work")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("no longer owns DevFlow run", context)
        self.assertIn("/devflow:resume " + self.run_id, context)
        self.assertIsNone(self.hook(self.question()))
        self.assertIsNone(self.hook(self.payload(event="Stop")))
        self.assertEqual(len(self.stored()["event_ids"]), 2)

    def test_unknown_events_and_nonquestion_results_inert(self):
        self.assertIsNone(self.hook(self.payload(event="Stop")))
        self.assertIsNone(self.hook(self.payload(event="PostToolUse", tool="Write")))
        self.assertIsNone(self.hook(self.payload(event="SessionStart")))
        self.bind()
        self.assertIsNone(self.hook(self.payload(event="PostToolUse", tool="Write")))
        self.assertIsNone(self.hook(self.payload(event="Stop")))
        self.assertEqual(len(self.stored()["event_ids"]), 1)

    def test_hook_configuration_uses_exec_form_and_never_blocks(self):
        config = json.loads((PACKAGE.parents[2] / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(set(config["hooks"]), {"UserPromptSubmit", "PreToolUse", "PostToolUse"})
        for event, matchers in config["hooks"].items():
            for matcher in matchers:
                # Tool hooks run only for AskUserQuestion; other tool calls start no process.
                if event in {"PreToolUse", "PostToolUse"}:
                    self.assertEqual(matcher["matcher"], "AskUserQuestion")
                else:
                    self.assertNotIn("matcher", matcher)
                for hook in matcher["hooks"]:
                    self.assertEqual(hook["command"], "uv")
                    self.assertIsInstance(hook["args"], list)
                    self.assertNotIn("onFailure", hook)
                    self.assertEqual(hook["timeout"], 30)
                    self.assertEqual(hook["args"][-1], "hook")
                    self.assertIn("${CLAUDE_PLUGIN_ROOT}/scripts/devflow.py", hook["args"])
                    self.assertIn("--no-python-downloads", hook["args"])
                    self.assertNotIn("--python", hook["args"])


class ApprovalTests(Fixture):
    """Which AskUserQuestion answers can resolve an escalation (sessions.approval_candidates).

    An answer counts only when PreToolUse recorded the question (an observation
    with the same session and tool_use_id) and Claude did not fill in the
    screen's fields itself.
    """

    def setUp(self):
        super().setUp()
        self.bind()
        self.escalate()

    def ask(self, tool_use_id, **fields):
        payload = self.payload(tool="AskUserQuestion", questions=[{"question": "Continue?"}], **fields)
        payload["tool_use_id"] = tool_use_id
        self.assertIsNone(self.hook(payload))

    def reply(self, tool_use_id, **extra):
        payload = self.payload(event="PostToolUse", tool="AskUserQuestion", questions=[{"question": "Continue?"}],
                               answers={"Continue?": "Yes"})
        payload.update(tool_use_id=tool_use_id, tool_response={"answers": {"Continue?": "Yes"}}, **extra)
        context = self.hook(payload)["hookSpecificOutput"]["additionalContext"]
        event_id = self.events()[-1]["event_id"]
        self.assertIn(event_id, context)
        return event_id

    def candidates(self):
        return [item["event_id"] for item in Engine(self.project).status(self.run_id)["human_events"]]

    def assert_cannot_resolve(self, event_id):
        with self.assertRaisesRegex(DevFlowError, "event ID of a successful AskUserQuestion"):
            Engine(self.project).resolve(self.run_id, self.session_id, event_id, "continue", "The human agreed.")
        self.assertEqual(self.stored()["status"], "awaiting_human")

    def test_an_answer_without_a_recorded_question_is_not_a_candidate(self):
        event_id = self.reply("unobserved")
        self.assertEqual(self.events()[-1]["kind"], "question")
        self.assertEqual(self.candidates(), [])
        self.assert_cannot_resolve(event_id)

    def test_an_answer_to_a_prefilled_question_is_not_a_candidate(self):
        for use_id, fields in (("prefilled-answers", {"answers": {"Continue?": "Yes"}}),
                               ("prefilled-notes", {"annotations": {"Continue?": {"notes": "Approved"}}})):
            with self.subTest(fields=fields):
                self.ask(use_id, **fields)
                event_id = self.reply(use_id)
                self.assertEqual(self.candidates(), [])
                self.assert_cannot_resolve(event_id)

    def test_an_observation_of_another_question_does_not_count(self):
        self.ask("asked")
        self.assertEqual(self.candidates(), [])
        self.reply("not-asked")
        self.assertEqual(self.candidates(), [])

    def test_an_answer_to_a_recorded_question_is_a_candidate_and_resolves(self):
        self.ask("asked", answers={}, annotations={})  # Empty screen fields are not a prefill.
        event_id = self.reply("asked")
        self.assertEqual([event["kind"] for event in self.events()[-2:]], ["observation", "question"])
        self.assertEqual(self.candidates(), [event_id])
        result = Engine(self.project).resolve(self.run_id, self.session_id, event_id, "continue", "The human agreed.")
        self.assertNotEqual(result["status"], "awaiting_human")

    def test_a_subagent_answer_is_not_a_candidate(self):
        self.ask("subagent-question")
        event_id = self.reply("subagent-question", agent_id="agent-1", agent_type="Explore")
        self.assertEqual(self.candidates(), [])
        self.assert_cannot_resolve(event_id)


if __name__ == "__main__":
    unittest.main()
