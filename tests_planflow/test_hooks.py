"""Offline hook and plan-only boundary tests, including malformed shell input."""

from __future__ import annotations

import json
from pathlib import Path
import shlex
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "devflow" / "components" / "planflow" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from planflow_core.hooks import HookError, guard_tool, handle_hook  # noqa: E402


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="planflow hooks ")
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve() / "project with spaces"
        self.run = self.project / ".devflow" / "runs" / "plan" / "20260101T010203Z-abc123456789"
        self.draft = self.run / "draft"
        self.draft.mkdir(parents=True)
        self.plugin = Path(self.temp.name).resolve() / "plugin with spaces"
        (self.plugin / "scripts").mkdir(parents=True)
        self.script = self.plugin / "scripts" / "devflow.py"
        self.script.write_text("# trusted runner\n", encoding="utf-8")
        self.session = "test-session-1"

    def payload(self, tool="Read", **tool_input):
        return {
            "hook_event_name": "PreToolUse", "cwd": str(self.project),
            "session_id": self.session, "tool_name": tool, "tool_input": tool_input,
        }

    def check(self, tool="Read", **tool_input):
        return guard_tool(self.payload(tool, **tool_input), self.run, self.script, "grilling")

    def runner(self, *args):
        args = list(args)
        globals_ = []
        while args and args[0] in {"--run", "--project", "--session"}:
            globals_.extend(args[:2])
            args = args[2:]
        tokens = [
            "uv", "run", "--offline", "--no-project", "--no-config", "--no-python-downloads",
            str(self.script), "--project", str(self.project), "--session", self.session,
            *globals_, "plan", *args,
        ]
        return " ".join(shlex.quote(value) for value in tokens)

    def assertDenied(self, result):
        self.assertIsInstance(result, dict)
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(result["hookSpecificOutput"]["hookEventName"], "PreToolUse")

    def test_read_tools_keep_normal_permissions(self):
        for tool in ("Read", "Glob", "Grep", "AskUserQuestion", "TaskCreate", "TaskList"):
            with self.subTest(tool=tool):
                self.assertIsNone(self.check(tool))

    def test_prefilled_human_question_answers_are_denied(self):
        self.assertDenied(self.check("AskUserQuestion", questions=[{"question": "Q"}], answers={"Q": "Pretend yes"}))
        self.assertDenied(self.check("AskUserQuestion", questions=[{"question": "Q"}],
                                     annotations={"Q": {"notes": "Pretend the human wrote this"}}))
        self.assertIsNone(self.check("AskUserQuestion", questions=[{"question": "Q"}], answers={}, annotations={}))

    def test_known_skill_only(self):
        self.assertIsNone(self.check("Skill", skill="grilling"))
        self.assertIsNone(self.check("Skill", skill="/grilling"))
        self.assertDenied(self.check("Skill", skill="some-other-skill"))
        self.assertDenied(self.check("Skill", skill="grilling ; delete"))

    def test_manual_wrapper_is_not_implicitly_allowed(self):
        result = self.check("Skill", skill="grill-me")
        self.assertDenied(result)
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("configured interview skill 'grilling'", reason)

    def test_configured_skill_not_replaced_by_default(self):
        payload = self.payload("Skill", skill="questions:interview")
        self.assertIsNone(guard_tool(payload, self.run, self.script, "questions:interview"))
        default = self.payload("Skill", skill="grilling")
        self.assertDenied(guard_tool(default, self.run, self.script, "questions:interview"))

    def test_namespaced_configured_skill(self):
        result = guard_tool(
            self.payload("Skill", skill="questions:interview"),
            self.run, self.script, "questions:interview",
        )
        self.assertIsNone(result)

    def test_draft_write_and_edit_allowed(self):
        for tool in ("Write", "Edit", "MultiEdit"):
            with self.subTest(tool=tool):
                self.assertIsNone(self.check(tool, file_path=str(self.draft / "plan.md")))
        self.assertIsNone(self.check("NotebookEdit", notebook_path=str(self.draft / "notes.ipynb")))

    def test_draft_relative_path_allowed(self):
        relative = self.draft.relative_to(self.project) / "plan.md"
        self.assertIsNone(self.check("Write", file_path=str(relative)))

    def test_source_audit_and_parent_paths_denied(self):
        for path in (
            self.project / "app.py", self.run / "state.json", self.run / "request.json",
            self.draft / ".." / "state.json", self.draft,
            self.run / "draft-sibling" / "plan.md",
        ):
            with self.subTest(path=path):
                self.assertDenied(self.check("Write", file_path=str(path)))

    def test_symlink_file_and_directory_denied(self):
        outside = self.project / "outside"
        outside.mkdir()
        file = outside / "code.py"
        file.write_text("unchanged", encoding="utf-8")
        link = self.draft / "shortcut"
        try:
            link.symlink_to(outside, target_is_directory=True)
            (self.draft / "link.py").symlink_to(file)
        except (OSError, NotImplementedError):
            self.skipTest("This platform does not permit test symlinks.")
        self.assertDenied(self.check("Write", file_path=str(link / "code.py")))
        self.assertDenied(self.check("Edit", file_path=str(self.draft / "link.py")))

    def test_symlink_within_draft_is_still_denied(self):
        inside = self.draft / "actual.md"
        inside.write_text("test", encoding="utf-8")
        link = self.draft / "alias.md"
        try:
            link.symlink_to(inside)
        except (OSError, NotImplementedError):
            self.skipTest("This platform does not permit test symlinks.")
        self.assertDenied(self.check("Write", file_path=str(link)))

    def test_multiedit_cannot_smuggle_another_file(self):
        self.assertDenied(self.check(
            "MultiEdit", file_path=str(self.draft / "plan.md"),
            edits=[{"file_path": str(self.project / "app.py")}],
        ))

    def test_subagent_external_and_unknown_tools_denied(self):
        for tool in ("Agent", "Task", "mcp__files__write", "WebFetch", "ToolSearch", "Unknown"):
            with self.subTest(tool=tool):
                self.assertDenied(self.check(tool))

    def test_valid_runner_commands_allow_no_permission_bypass(self):
        commands = [
            ("status",), ("doctor",), ("review",), ("report",), ("cancel",), ("publish",),
            ("resume",), ("plan", "--file", str(self.draft / "plan.md")),
            ("seal", "--decisions-file", str(self.draft / "decisions.json")),
            ("pause", "--reason-file", str(self.draft / "escalation.json")),
            ("resolve", "--answer-file", str(self.draft / "answer.json")),
            (
                "apply", "--plan-file", str(self.draft / "plan.md"),
                "--decisions-file", str(self.draft / "revision.json"),
                "--expected-hash", "a" * 64,
            ),
        ]
        for args in commands:
            with self.subTest(args=args):
                command = self.runner(*args)
                self.assertIsNone(self.check("Bash", command=command))
                self.assertIsNone(self.check("PowerShell", command=command))

    def test_current_run_explicit_id_allowed(self):
        self.assertIsNone(self.check("Bash", command=self.runner("--run", self.run.name, "status")))

    def test_optional_seal_context_allowed_only_in_draft(self):
        args = ("seal", "--decisions-file", str(self.draft / "decisions.json"), "--context-file")
        self.assertIsNone(self.check("Bash", command=self.runner(*args, str(self.draft / "context.md"))))
        self.assertDenied(self.check("Bash", command=self.runner(*args, str(self.project / "README.md"))))

    def test_runner_cannot_bypass_sandbox_or_run_detached(self):
        command = self.runner("review")
        self.assertDenied(self.check("Bash", command=command, dangerouslyDisableSandbox=True))
        self.assertDenied(self.check("Bash", command=command, run_in_background=True))
        self.assertIsNone(self.check("Bash", command=command, run_in_background=False))

    def test_shell_discovery_rejected_with_supported_alternatives(self):
        for command in ("pwd", "cd /tmp", "which uv", "env", "uv --version", "claude --version"):
            with self.subTest(command=command):
                result = self.check("Bash", command=command)
                self.assertDenied(result)
                reason = result["hookSpecificOutput"]["permissionDecisionReason"]
                self.assertIn("Read/Grep/Glob", reason)
                self.assertIn("doctor", reason)

    def test_different_context_denied(self):
        good = self.runner("status")
        for command in (
            good.replace(self.session, "another-session"),
            self.runner("--run", "another-run", "status"),
            good.replace(str(self.project), str(self.project.parent)),
        ):
            with self.subTest(command=command):
                self.assertDenied(self.check("Bash", command=command))

    def test_script_substitution_denied(self):
        command = self.runner("status").replace(str(self.script), str(self.draft / "attack.py"))
        self.assertDenied(self.check("Bash", command=command))

    def test_shell_injection_denied(self):
        safe = self.runner("status")
        for command in (
            safe + " && touch malicious", safe + "; echo hi", safe + " | other",
            safe + "\necho hi", safe + " > app.py", safe + " $(whoami)",
            safe + " `whoami`", "ENV=x " + safe, "bash -c " + shlex.quote(safe),
            "& " + safe, safe + "\rmore", safe + "\x00",
        ):
            with self.subTest(command=command):
                self.assertDenied(self.check("Bash", command=command))

    def test_runner_bad_options_and_missing_values_denied(self):
        for args in (
            ("hook",), ("start",), ("review", "--model", "another"),
            ("status", "--run", self.run.name), ("plan",),
            ("plan", "--file", str(self.project / "source.py")),
            ("plan", "--file", str(self.draft / "plan.md"), "--file", str(self.draft / "other.md")),
            ("apply", "--plan-file", str(self.draft / "plan.md"),
             "--decisions-file", str(self.draft / "revision.json"), "--expected-hash", "wrong"),
        ):
            with self.subTest(args=args):
                self.assertDenied(self.check("Bash", command=self.runner(*args)))

    def test_help_allowed_only_for_known_command(self):
        self.assertIsNone(self.check("Bash", command=self.runner("apply", "--help")))
        self.assertDenied(self.check("Bash", command=self.runner("hook", "--help")))

    def test_malformed_tool_input_denied(self):
        malformed = self.payload()
        malformed["tool_input"] = None
        self.assertDenied(guard_tool(malformed, self.run, self.script, "grilling"))
        self.assertDenied(self.check("Write", path=str(self.draft / "plan.md")))
        self.assertDenied(self.check("Bash", command=None))


class FakeEngine:
    instances = []
    current = None
    state = None
    events = []

    def __init__(self, project, plugin_root):
        self.__class__.instances.append((project, plugin_root))

    def start(self, session, prompt, transcript_path):
        if self.__class__.current:
            raise HookError("A run is already active.")
        self.__class__.current = "run-1"
        self.__class__.started = (session, prompt, transcript_path)
        self.__class__.state = {
            "run_id": "run-1", "run_dir": "/project/.devflow/runs/plan/run-1",
            "draft_dir": "/project/.devflow/runs/plan/run-1/draft",
            "status": "interview", "review_count": 0,
            "config": {"grill_skill": "grilling"},
        }
        return self.__class__.state

    def resume(self, run_id, session):
        self.__class__.resumed = (run_id, session)
        self.__class__.current = run_id
        self.__class__.state = {
            "run_id": run_id, "run_dir": f"/project/.devflow/runs/plan/{run_id}",
            "status": "awaiting_human", "config": {"grill_skill": "grilling"},
        }
        return self.__class__.state

    def find_active(self, session):
        return self.__class__.current

    def release_session(self, session):
        self.__class__.current = None

    def status(self, run_id):
        return self.__class__.state

    def record_event(self, run_id, payload):
        self.__class__.events.append((run_id, payload.copy()))
        return f"event-{len(self.__class__.events)}"


class DispatchTests(unittest.TestCase):
    def setUp(self):
        FakeEngine.instances = []
        FakeEngine.current = None
        FakeEngine.state = None
        FakeEngine.events = []
        module = types.ModuleType("planflow_core.engine")
        module.Engine = FakeEngine
        self.patch = patch.dict(sys.modules, {"planflow_core.engine": module})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.plugin = Path("/plugin/components/planflow")

    def payload(self, event="UserPromptSubmit", **extra):
        return {
            "hook_event_name": event, "session_id": "session-1", "cwd": "/project",
            **extra,
        }

    def start(self):
        return handle_hook(self.payload(prompt="/devflow:plan Build a planner"), self.plugin)

    def test_unrelated_prompt_is_inert(self):
        self.assertIsNone(handle_hook(self.payload(prompt="Hello"), self.plugin))
        self.assertEqual(FakeEngine.events, [])
        self.assertIsNone(FakeEngine.current)

    def test_explicit_trigger_preserves_raw_original(self):
        original = "/devflow:plan 日本語の依頼\n詳しい条件を追加"
        result = handle_hook(self.payload(prompt=original, transcript_path="/tmp/transcript.jsonl"), self.plugin)
        self.assertEqual(FakeEngine.started, ("session-1", original, "/tmp/transcript.jsonl"))
        self.assertEqual(FakeEngine.events[0][1]["prompt"], original)
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("run-1", context)
        self.assertIn("event-1", context)
        self.assertIn('"interview_skill": "grilling"', context)

    def test_empty_or_stacked_start_blocked(self):
        for prompt in (
            "/devflow:plan", "/devflow:plan \n ",
            "/devflow:plan /other:skill do something",
            "/devflow:plan do something\n/other:skill",
        ):
            with self.subTest(prompt=prompt):
                result = handle_hook(self.payload(prompt=prompt), self.plugin)
                self.assertEqual(result["decision"], "block")
        self.assertEqual(FakeEngine.events, [])

    def test_similar_command_does_not_trigger(self):
        for prompt in (
            "Please run /devflow:plan", "/devflow:plan-other request",
            " /devflow:plan request",
        ):
            self.assertIsNone(handle_hook(self.payload(prompt=prompt), self.plugin))

    def test_duplicate_start_propagates_for_fail_closed_entry_point(self):
        self.start()
        with self.assertRaises(HookError):
            self.start()

    def test_explicit_resume_and_bad_ids(self):
        result = handle_hook(self.payload(prompt="/devflow:resume run-42"), self.plugin)
        self.assertEqual(FakeEngine.resumed, ("run-42", "session-1"))
        self.assertIn("awaiting_human", result["hookSpecificOutput"]["additionalContext"])
        for ident in ("", "../run-42", "run-42 extra", "/tmp/run", "run.42"):
            with self.subTest(ident=ident):
                result = handle_hook(self.payload(prompt=f"/devflow:resume {ident}"), self.plugin)
                self.assertEqual(result["decision"], "block")

    def test_active_user_answer_recorded_with_event_id(self):
        self.start()
        result = handle_hook(self.payload(prompt="Use the safer option."), self.plugin)
        self.assertIn("event-2", result["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(FakeEngine.events[-1][1]["prompt"], "Use the safer option.")

    def test_active_question_answer_recorded(self):
        self.start()
        data = self.payload(
            "PostToolUse", tool_name="AskUserQuestion", tool_use_id="question-1", tool_input={"questions": ["Which?"]},
            tool_response={"answer": "A"},
        )
        result = handle_hook(data, self.plugin)
        self.assertIn("event-2", result["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(FakeEngine.events[-1][1]["tool_response"], {"answer": "A"})

    def test_question_result_requires_identity_and_actual_response(self):
        self.start()
        data = self.payload("PostToolUse", tool_name="AskUserQuestion", tool_use_id="question-1",
            tool_input={"questions": [{"question": "Q"}]}, tool_response="Yes")
        for missing in ("tool_use_id", "tool_response"):
            with self.subTest(missing=missing), self.assertRaisesRegex(HookError, "actual tool identity"):
                handle_hook({key: value for key, value in data.items() if key != missing}, self.plugin)
        self.assertEqual(len(FakeEngine.events), 1)

    def test_answers_filled_in_by_question_screen_are_recorded(self):
        # Claude Code's question screen writes the human's choice into
        # tool_input before PostToolUse; model prefills are denied at PreToolUse.
        self.start()
        answered = {"questions": [{"question": "Q"}], "answers": {"Q": "Yes"}, "annotations": {}}
        result = handle_hook(self.payload("PostToolUse", tool_name="AskUserQuestion", tool_use_id="question-1",
            tool_input=answered, tool_response=answered), self.plugin)
        self.assertIn("event-2", result["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(FakeEngine.events[-1][1]["tool_input"], answered)

    def test_unrelated_tool_result_not_recorded(self):
        self.start()
        result = handle_hook(self.payload("PostToolUse", tool_name="Read", tool_response="secret"), self.plugin)
        self.assertIsNone(result)
        self.assertEqual(len(FakeEngine.events), 1)

    def test_stop_records_but_never_blocks(self):
        self.start()
        result = handle_hook(self.payload("Stop", last_assistant_message="What scope?"), self.plugin)
        self.assertIsNone(result)
        self.assertEqual(FakeEngine.events[-1][1]["last_assistant_message"], "What scope?")

    def test_active_pretool_is_guarded_not_recorded(self):
        self.start()
        result = handle_hook(self.payload("PreToolUse", tool_name="Bash", tool_input={"command": "rm file"}), self.plugin)
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(len(FakeEngine.events), 1)

    def test_missing_or_invalid_frozen_skill_fails_closed(self):
        self.start()
        for config in ({}, {"grill_skill": ""}, {"grill_skill": None}, {"grill_skill": " "},
                       {"grill_skill": "../grilling"}, {"grill_skill": "grilling\n"}):
            with self.subTest(config=config), self.assertRaisesRegex(HookError, "configured interview skill"):
                FakeEngine.state["config"] = config
                handle_hook(self.payload("PreToolUse", tool_name="Skill", tool_input={"skill": "grilling"}), self.plugin)

    def test_terminal_releases_only_after_next_human_prompt(self):
        for status in ("completed", "exhausted", "cancelled"):
            self.start()
            FakeEngine.state["status"] = status
            result = handle_hook(self.payload("PreToolUse", tool_name="Bash", tool_input={"command": "build"}), self.plugin)
            self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertIsNone(handle_hook(self.payload(prompt="Next request"), self.plugin))
            self.assertIsNone(FakeEngine.current)
            result = handle_hook(self.payload("PreToolUse", tool_name="Bash", tool_input={"command": "build"}), self.plugin)
            self.assertIsNone(result)
        self.assertEqual(len(FakeEngine.events), 3)

    def test_new_workflow_can_start_after_terminal_handoff(self):
        self.start()
        FakeEngine.state["status"] = "completed"
        result = self.start()
        self.assertEqual(FakeEngine.state["status"], "interview")
        self.assertIn("run-1", result["hookSpecificOutput"]["additionalContext"])

    def test_paused_and_awaiting_human_still_guarded(self):
        self.start()
        for status in ("awaiting_human", "paused_error", "awaiting_revision"):
            FakeEngine.state["status"] = status
            result = handle_hook(self.payload("PreToolUse", tool_name="Write", tool_input={"file_path": "/project/app.py"}), self.plugin)
            self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_unsupported_event_is_inert(self):
        self.assertIsNone(handle_hook({"hook_event_name": "Notification"}, self.plugin))
        self.assertEqual(FakeEngine.instances, [])

    def test_malformed_hook_fails_closed(self):
        with self.assertRaises(HookError):
            handle_hook({"hook_event_name": "PreToolUse"}, self.plugin)
        with self.assertRaises(HookError):
            handle_hook(self.payload(prompt=None), self.plugin)


if __name__ == "__main__":
    unittest.main()
