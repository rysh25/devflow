"""Plan workflow hooks through the router: human-event recording and context.

Every test uses a real bound plan run and a separately installed plugin root,
so the runner path named in the hook context is that plugin's ``scripts/devflow.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from devflow_core import router
from devflow_core.common import sessions
from devflow_core.common.errors import DevFlowError
from devflow_core.common.runs import RunStore
from devflow_core.common.storage import atomic_json, read_json
from devflow_core.workflows import WORKFLOWS


class PlanHookFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="devflow plan hooks ")
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.project = base / "project with spaces"
        self.project.mkdir()
        (self.project / ".git").mkdir()
        self.plugin = base / "plugin with spaces"
        (self.plugin / "scripts").mkdir(parents=True)
        self.script = self.plugin / "scripts" / "devflow.py"
        self.script.write_text("# trusted runner\n", encoding="utf-8")
        self.transcript = base / "transcript.jsonl"
        self.session = "test-session-1"
        self.store = RunStore(self.project, WORKFLOWS["plan"])
        self.run_id = None

    def hook(self, payload):
        return router.handle_hook(payload, self.plugin)

    def payload(self, event="UserPromptSubmit", session=None, **extra):
        return {"hook_event_name": event, "cwd": str(self.project), "session_id": session or self.session,
                "transcript_path": str(self.transcript), **extra}

    def start(self, request="Build a planner", grill_skill=None):
        if grill_skill:
            atomic_json(self.project / "devflow.config.json", {"schema_version": 1, "plan": {"grill_skill": grill_skill}})
        result = self.hook(self.payload(prompt="/devflow:plan " + request))
        self.run_id = self.bound_run()
        self.run = self.store.run_dir(self.run_id)
        self.draft = self.run / "draft"
        return result

    def bound_run(self, session=None):
        record = sessions.read_record(self.project, session or self.session, WORKFLOWS)
        return record["run"]["run_id"] if record and record["run"] else None

    def state(self):
        return read_json(self.run / "state.json")

    def events(self):
        return self.store.events(self.state())

    def set_status(self, status):
        with self.store.lock(self.run_id):
            state = self.store.load(self.run_id)
            state["status"] = status
            self.store.save(state)

    def assertNotAccepted(self, result, reason=""):
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("did not accept", context)
        self.assertIn(reason, context)
        return context

    def ask(self, tool_use_id, **tool_input):
        return self.hook(self.payload("PreToolUse", tool_name="AskUserQuestion", tool_use_id=tool_use_id,
                                      tool_input=tool_input))


class DispatchTests(PlanHookFixture):
    def test_unrelated_prompt_is_inert(self):
        self.assertIsNone(self.hook(self.payload(prompt="Hello")))
        self.assertFalse((self.project / ".devflow").exists())

    def test_explicit_trigger_preserves_raw_original(self):
        original = "/devflow:plan 日本語の依頼\n詳しい条件を追加"
        result = self.hook(self.payload(prompt=original))
        self.run_id = self.bound_run()
        self.run = self.store.run_dir(self.run_id)
        request = read_json(self.run / "request.json")
        self.assertEqual(request["raw_prompt"], original)
        self.assertEqual(request["request"], "日本語の依頼\n詳しい条件を追加")
        self.assertEqual(request["session_id"], self.session)
        self.assertEqual(self.state()["transcript_marker"]["path"], str(self.transcript))
        [first] = self.events()
        self.assertEqual(first["kind"], "prompt")
        self.assertEqual(first["payload"]["prompt"], original)
        self.assertEqual(first["payload"]["transcript_path"], str(self.transcript))
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn(self.run_id, context)
        self.assertIn(first["event_id"], context)
        self.assertIn('"interview_skill": "grilling"', context)
        # A resumed conversation may still hold an older plugin path.
        self.assertIn(json.dumps(str(self.script)), context)

    def test_empty_or_stacked_start_is_not_accepted(self):
        for prompt, reason in (
            ("/devflow:plan", "followed by the original planning request"),
            ("/devflow:plan \n ", "followed by the original planning request"),
            ("/devflow:plan /other:skill do something", "not stacked skills"),
            ("/devflow:plan do something\n/other:skill", "not stacked skills"),
        ):
            with self.subTest(prompt=prompt):
                self.assertNotAccepted(self.hook(self.payload(prompt=prompt)), reason)
        self.assertIsNone(sessions.read_record(self.project, self.session, WORKFLOWS))
        self.assertEqual(self.store.scan(), ([], []))

    def test_similar_command_does_not_trigger(self):
        for prompt in (
            "Please run /devflow:plan", "/devflow:plan-other request",
            " /devflow:plan request",
        ):
            with self.subTest(prompt=prompt):
                self.assertIsNone(self.hook(self.payload(prompt=prompt)))
        self.assertFalse((self.project / ".devflow").exists())

    def test_duplicate_start_is_not_accepted_and_keeps_the_first_run(self):
        self.start()
        first = self.run_id
        self.assertNotAccepted(self.start(), "Finish or cancel")
        self.assertEqual(self.run_id, first)
        self.assertEqual([info.run_id for info in self.store.scan()[0]], [first])

    def test_explicit_resume_and_bad_ids(self):
        self.start()
        result = self.hook(self.payload(prompt=f"/devflow:resume {self.run_id}"))
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn(self.run_id, context)
        self.assertIn('"status": "interview"', context)
        resumed = self.events()[-1]
        self.assertEqual(resumed["payload"]["prompt"], f"/devflow:resume {self.run_id}")
        self.assertIn(resumed["event_id"], context)
        before = self.state()
        for ident in ("", "../run-42", "run-42 extra", "/tmp/run", "run.42", "20990101T000000Z-000000000000"):
            with self.subTest(ident=ident):
                self.assertNotAccepted(self.hook(self.payload(prompt=f"/devflow:resume {ident}")), "/devflow:resume")
        self.assertEqual(self.state(), before)

    def test_active_user_answer_recorded_with_event_id(self):
        self.start()
        result = self.hook(self.payload(prompt="Use the safer option."))
        event = self.events()[-1]
        self.assertEqual(len(self.events()), 2)
        self.assertEqual((event["kind"], event["payload"]["prompt"]), ("prompt", "Use the safer option."))
        self.assertIn(f'"recorded_event_id": "{event["event_id"]}"', result["hookSpecificOutput"]["additionalContext"])

    def test_active_question_answer_recorded(self):
        self.start()
        data = self.payload(
            "PostToolUse", tool_name="AskUserQuestion", tool_use_id="question-1", tool_input={"questions": ["Which?"]},
            tool_response={"answer": "A"},
        )
        result = self.hook(data)
        event = self.events()[-1]
        self.assertEqual(event["kind"], "question")
        self.assertEqual(event["payload"]["tool_response"], {"answer": "A"})
        self.assertIn(event["event_id"], result["hookSpecificOutput"]["additionalContext"])

    def test_question_result_requires_identity_and_actual_response(self):
        self.start()
        data = self.payload("PostToolUse", tool_name="AskUserQuestion", tool_use_id="question-1",
            tool_input={"questions": [{"question": "Q"}]}, tool_response="Yes")
        for missing in ("tool_use_id", "tool_response", "tool_input"):
            with self.subTest(missing=missing):
                context = self.hook({key: value for key, value in data.items() if key != missing})
                context = context["hookSpecificOutput"]["additionalContext"]
                self.assertIn("actual tool identity", context)
                self.assertIn("was not recorded and cannot resolve", context)
        self.assertEqual(len(self.events()), 1)

    def test_answers_filled_in_by_question_screen_are_recorded(self):
        # Claude Code's question screen writes the human's choice into
        # tool_input before PostToolUse; what the model sent is recorded at PreToolUse.
        self.start()
        self.assertIsNone(self.ask("question-1", questions=[{"question": "Q"}]))
        answered = {"questions": [{"question": "Q"}], "answers": {"Q": "Yes"}, "annotations": {}}
        result = self.hook(self.payload("PostToolUse", tool_name="AskUserQuestion", tool_use_id="question-1",
            tool_input=answered, tool_response=answered))
        observation, event = self.events()[-2:]
        self.assertEqual((observation["kind"], event["kind"]), ("observation", "question"))
        self.assertEqual(observation["payload"]["tool_input"], {"questions": [{"question": "Q"}]})
        self.assertEqual(event["payload"]["tool_input"], answered)
        self.assertIn(event["event_id"], result["hookSpecificOutput"]["additionalContext"])

    def test_unrelated_tool_result_not_recorded(self):
        self.start()
        result = self.hook(self.payload("PostToolUse", tool_name="Read", tool_response="secret"))
        self.assertIsNone(result)
        self.assertEqual(len(self.events()), 1)

    def test_stop_records_but_never_blocks(self):
        self.start()
        result = self.hook(self.payload("Stop", last_assistant_message="What scope?"))
        self.assertIsNone(result)
        event = self.events()[-1]
        self.assertEqual((event["kind"], event["payload"]["last_assistant_message"]), ("stop", "What scope?"))

    def test_tool_calls_other_than_questions_are_not_stopped_or_recorded(self):
        self.start()
        for tool, data in (("Bash", {"command": "rm file"}), ("Write", {"file_path": str(self.project / "app.py")}),
                           ("Skill", {"skill": "some-other-skill"}), ("Agent", {"prompt": "x"})):
            with self.subTest(tool=tool):
                self.assertIsNone(self.hook(self.payload("PreToolUse", tool_name=tool, tool_input=data)))
        self.assertEqual(len(self.events()), 1)

    def test_every_question_is_recorded_as_an_observation_even_when_prefilled(self):
        self.start()
        self.assertIsNone(self.ask("q-1", questions=[{"question": "Q"}]))
        self.assertIsNone(self.ask("q-2", questions=[{"question": "Q"}], answers={"Q": "Pretend yes"}))
        self.assertIsNone(self.ask("q-3", questions=[{"question": "Q"}],
                                   annotations={"Q": {"notes": "Pretend the human wrote this"}}))
        self.assertIsNone(self.ask("q-1", questions=[{"question": "Q"}]))  # A hook retry is one event.
        observed = [event["payload"]["tool_use_id"] for event in self.events() if event["kind"] == "observation"]
        self.assertEqual(observed, ["q-1", "q-2", "q-3"])

    def test_a_damaged_plan_run_never_stops_the_session(self):
        self.start()
        atomic_json(self.run / "state.json", {})
        # The human's prompt and answers go through with a warning; nothing is recorded.
        context = self.hook(self.payload(prompt="Keep going"))["hookSpecificOutput"]["additionalContext"]
        self.assertIn(f"plan run {self.run_id}", context)
        self.assertIn("this prompt was not recorded", context)
        self.assertIsNone(self.hook(self.payload("PreToolUse", tool_name="Read", tool_input={"file_path": "notes.md"})))
        answer = self.payload("PostToolUse", tool_name="AskUserQuestion", tool_use_id="q-1",
                              tool_input={"questions": [{"question": "Q?"}]}, tool_response={"answers": {"Q?": "Yes"}})
        self.assertIn("cannot resolve a DevFlow escalation", self.hook(answer)["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.hook(self.payload("Stop")))
        # A question that cannot be recorded at PreToolUse is reported (the CLI exits 1, which
        # does not stop the tool); without that record its answer never counts.
        with self.assertRaisesRegex(DevFlowError, "cannot be read"):
            self.ask("q-2", questions=[{"question": "Q?"}])
        self.assertNotAccepted(self.hook(self.payload(prompt=f"/devflow:resume {self.run_id}")), "cannot be read")
        self.assertEqual(self.bound_run(), self.run_id)  # The binding is kept.

    def test_terminal_releases_only_after_next_human_prompt(self):
        for status in ("completed", "exhausted", "cancelled"):
            with self.subTest(status=status):
                self.start()
                self.set_status(status)
                result = self.hook(self.payload("PreToolUse", tool_name="Bash", tool_input={"command": "build"}))
                self.assertIsNone(result)
                self.assertEqual(self.bound_run(), self.run_id)  # Still bound for the rest of the turn.
                self.assertIsNone(self.hook(self.payload(prompt="Next request")))
                self.assertIsNone(self.bound_run())
                result = self.hook(self.payload("PreToolUse", tool_name="Bash", tool_input={"command": "build"}))
                self.assertIsNone(result)
                # Only the start prompt: neither tool calls nor the releasing prompt are recorded.
                self.assertEqual(len(self.events()), 1)
        self.assertEqual(len(self.store.scan()[0]), 3)

    def test_new_workflow_can_start_after_terminal_handoff(self):
        self.start()
        finished = self.run_id
        self.set_status("completed")
        result = self.start()
        self.assertNotEqual(self.run_id, finished)
        self.assertEqual(self.state()["status"], "interview")
        self.assertIn(self.run_id, result["hookSpecificOutput"]["additionalContext"])

    def test_unsupported_event_is_inert(self):
        self.assertIsNone(self.hook({"hook_event_name": "Notification"}))
        self.assertIsNone(self.hook(self.payload("Notification")))
        self.assertFalse((self.project / ".devflow").exists())

    def test_malformed_hook_raises_for_the_cli_to_report(self):
        with self.assertRaises(DevFlowError):
            self.hook({"hook_event_name": "PreToolUse"})
        with self.assertRaises(DevFlowError):
            self.hook(self.payload(prompt=None))
        with self.assertRaises(DevFlowError):
            self.hook(self.payload(prompt="/devflow:plan request", session="../bad session"))


if __name__ == "__main__":
    unittest.main()
