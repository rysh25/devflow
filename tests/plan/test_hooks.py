"""Plan workflow hooks through the router: the plan-only guard and human-event dispatch.

Every test uses a real bound plan run and a separately installed plugin root,
so the runner path the guard accepts is that plugin's ``scripts/devflow.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
import shlex
import tempfile
import unittest

from devflow_core import router
from devflow_core.common import sessions
from devflow_core.common.errors import DevFlowError, GuardError
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

    def assertDenied(self, result):
        self.assertIsInstance(result, dict)
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(result["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertTrue(result["hookSpecificOutput"]["permissionDecisionReason"].startswith("DevFlow guard: "))
        return result["hookSpecificOutput"]["permissionDecisionReason"]


class GuardTests(PlanHookFixture):
    def setUp(self):
        super().setUp()
        self.start()

    def check(self, tool="Read", **tool_input):
        return self.hook(self.payload("PreToolUse", tool_name=tool, tool_input=tool_input))

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

    def test_read_tools_keep_normal_permissions(self):
        for tool in ("Read", "Glob", "Grep", "AskUserQuestion", "TaskCreate", "TaskList", "ToolSearch"):
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
        self.assertDenied(self.check("Skill"))

    def test_manual_wrapper_is_not_implicitly_allowed(self):
        reason = self.assertDenied(self.check("Skill", skill="grill-me"))
        self.assertIn("configured interview skill 'grilling'", reason)

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

    def test_installed_plugin_is_read_only(self):
        self.assertIn("read-only", self.assertDenied(self.check("Write", file_path=str(self.script))))

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
        for tool in ("Agent", "Task", "mcp__files__write", "WebFetch", "Unknown"):
            with self.subTest(tool=tool):
                self.assertDenied(self.check(tool))

    def test_valid_runner_commands_allow_no_permission_bypass(self):
        commands = [
            ("status",), ("doctor",), ("review",), ("report",), ("cancel",), ("publish",),
            ("plan", "--file", str(self.draft / "plan.md")),
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
        self.assertIsNone(self.check("Bash", command=self.runner("--run", self.run_id, "status")))

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
        reasons = {}
        for command in ("pwd", "cd /tmp", "which uv", "env", "uv --version", "claude --version"):
            with self.subTest(command=command):
                reason = reasons[command] = self.assertDenied(self.check("Bash", command=command))
                self.assertIn("read-only search commands (grep, rg, find, ls, cat, head, tail, wc)", reason)
                self.assertIn(str(self.script), reason)
        # The supported alternative to probing the environment is the runner's doctor.
        self.assertIn("doctor", reasons["which uv"], "the denial no longer points to the runner's doctor")

    def test_read_only_search_commands_keep_normal_permissions(self):
        for command in (
            "grep -rn REVIEW-10 docs", "rg -n --glob '*.md' 'foo|bar$' .", "find . -name '*.py' -type f",
            "ls -la src", "cat README.md", "head -n 20 'file with spaces.md'", "tail -n 5 log.txt", "wc -l a b",
            "grep -E '^(a|b)[0-9]{2}$' f.txt",
        ):
            with self.subTest(command=command):
                self.assertIsNone(self.check("Bash", command=command))

    def test_read_only_commands_cannot_run_programs_write_or_chain(self):
        for command in (
            "find . -exec rm {} +", "find . -delete", "find . -fprint out.txt", "rg --pre=sh x .", "rg --pre sh x .",
            "grep -rn x . | head", "grep x . > out.txt", "cat a; rm b", "grep \"$HOME\" .", "cat $(echo a)",
            "grep x . && rm y", "grep \"a|b\" .", "cat a\nrm b",
        ):
            with self.subTest(command=command):
                self.assertDenied(self.check("Bash", command=command))
        # PowerShell quotes differently, so it gets the runner only.
        self.assertDenied(self.check("PowerShell", command="ls"))
        self.assertDenied(self.check("Bash", command="grep -rn x .", run_in_background=True))

    def test_runner_path_mismatch_names_the_installed_runner(self):
        stale = self.runner("status").replace(str(self.script), "/old/plugin/0.1.0/scripts/devflow.py")
        self.assertIn(str(self.script), self.assertDenied(self.check("Bash", command=stale)))

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
            ("status", "--run", self.run_id), ("plan",),
            ("plan", "--file", str(self.project / "source.py")),
            ("plan", "--file", str(self.draft / "plan.md"), "--file", str(self.draft / "other.md")),
            ("apply", "--plan-file", str(self.draft / "plan.md"),
             "--decisions-file", str(self.draft / "revision.json"), "--expected-hash", "wrong"),
            # Resume happens only in the hook; recover is for a human's own terminal.
            ("resume",), ("recover", "--expected-review", "1", "--confirm-process-stopped"),
        ):
            with self.subTest(args=args):
                self.assertDenied(self.check("Bash", command=self.runner(*args)))

    def test_help_never_runs_anything_so_any_help_is_allowed(self):
        for action in ("apply", "hook", "arbitrary"):
            with self.subTest(action=action):
                self.assertIsNone(self.check("Bash", command=self.runner(action, "--help")))
        self.assertDenied(self.check("Bash", command=self.runner("hook")))
        self.assertDenied(self.check("Bash", command=self.runner("apply", "--help", "--plan-file", "x")))

    def test_other_workflow_runner_actions_denied(self):
        command = self.runner("status").replace(" plan status", " implement check")
        self.assertDenied(self.check("Bash", command=command))

    def test_malformed_tool_input_denied(self):
        malformed = self.payload("PreToolUse", tool_name="Read")
        malformed["tool_input"] = None
        self.assertDenied(self.hook(malformed))
        self.assertDenied(self.check("Write", path=str(self.draft / "plan.md")))
        self.assertDenied(self.check("Bash", command=None))

    def test_pretool_is_never_recorded(self):
        before = self.state()["event_ids"]
        self.check("Write", file_path=str(self.draft / "plan.md"))
        self.check("Bash", command="rm file")
        self.assertEqual(self.state()["event_ids"], before)


class ConfiguredSkillTests(PlanHookFixture):
    def check_skill(self, skill):
        return self.hook(self.payload("PreToolUse", tool_name="Skill", tool_input={"skill": skill}))

    def test_configured_skill_not_replaced_by_default(self):
        self.start(grill_skill="questions:interview")
        self.assertIsNone(self.check_skill("questions:interview"))
        self.assertDenied(self.check_skill("grilling"))

    def test_namespaced_configured_skill(self):
        self.start(grill_skill="questions:interview")
        self.assertIsNone(self.check_skill("questions:interview"))
        self.assertIsNone(self.check_skill("/questions:interview"))
        self.assertDenied(self.check_skill("questions:other"))


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

    def test_empty_or_stacked_start_blocked(self):
        for prompt in (
            "/devflow:plan", "/devflow:plan \n ",
            "/devflow:plan /other:skill do something",
            "/devflow:plan do something\n/other:skill",
        ):
            with self.subTest(prompt=prompt), self.assertRaises(GuardError):
                self.hook(self.payload(prompt=prompt))
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

    def test_duplicate_start_propagates_for_fail_closed_entry_point(self):
        self.start()
        with self.assertRaises(DevFlowError):
            self.start()
        self.assertEqual([info.run_id for info in self.store.scan()[0]], [self.run_id])

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
            with self.subTest(ident=ident), self.assertRaises(DevFlowError):
                self.hook(self.payload(prompt=f"/devflow:resume {ident}"))
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
            with self.subTest(missing=missing), self.assertRaisesRegex(DevFlowError, "actual tool identity"):
                self.hook({key: value for key, value in data.items() if key != missing})
        self.assertEqual(len(self.events()), 1)

    def test_answers_filled_in_by_question_screen_are_recorded(self):
        # Claude Code's question screen writes the human's choice into
        # tool_input before PostToolUse; model prefills are denied at PreToolUse.
        self.start()
        answered = {"questions": [{"question": "Q"}], "answers": {"Q": "Yes"}, "annotations": {}}
        result = self.hook(self.payload("PostToolUse", tool_name="AskUserQuestion", tool_use_id="question-1",
            tool_input=answered, tool_response=answered))
        event = self.events()[-1]
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

    def test_active_pretool_is_guarded_not_recorded(self):
        self.start()
        result = self.hook(self.payload("PreToolUse", tool_name="Bash", tool_input={"command": "rm file"}))
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(len(self.events()), 1)

    def test_missing_or_invalid_frozen_skill_fails_closed(self):
        self.start()
        for config in ({}, {"grill_skill": ""}, {"grill_skill": None}, {"grill_skill": " "},
                       {"grill_skill": "../grilling"}, {"grill_skill": "grilling\n"}):
            with self.subTest(config=config):
                state = self.state()
                frozen = {key: value for key, value in state["config"].items() if key != "grill_skill"}
                state["config"] = {**frozen, **config}
                atomic_json(self.run / "state.json", state)
                result = self.hook(self.payload("PreToolUse", tool_name="Skill", tool_input={"skill": "grilling"}))
                self.assertIn("configured interview skill", self.assertDenied(result))
        # A frozen configuration that is not even an object makes the run unreadable: the hook fails closed.
        state = self.state()
        state["config"] = "broken"
        atomic_json(self.run / "state.json", state)
        with self.assertRaisesRegex(DevFlowError, "cannot be read"):
            self.hook(self.payload("PreToolUse", tool_name="Skill", tool_input={"skill": "grilling"}))

    def test_terminal_releases_only_after_next_human_prompt(self):
        for status in ("completed", "exhausted", "cancelled"):
            with self.subTest(status=status):
                self.start()
                self.set_status(status)
                result = self.hook(self.payload("PreToolUse", tool_name="Bash", tool_input={"command": "build"}))
                self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
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

    def test_paused_and_awaiting_human_still_guarded(self):
        self.start()
        for status in ("awaiting_human", "paused_error", "awaiting_revision"):
            with self.subTest(status=status):
                self.set_status(status)
                result = self.hook(self.payload("PreToolUse", tool_name="Write",
                                                tool_input={"file_path": str(self.project / "app.py")}))
                self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_unsupported_event_is_inert(self):
        self.assertIsNone(self.hook({"hook_event_name": "Notification"}))
        self.assertIsNone(self.hook(self.payload("Notification")))
        self.assertFalse((self.project / ".devflow").exists())

    def test_malformed_hook_fails_closed(self):
        with self.assertRaises(DevFlowError):
            self.hook({"hook_event_name": "PreToolUse"})
        with self.assertRaises(DevFlowError):
            self.hook(self.payload(prompt=None))
        with self.assertRaises(DevFlowError):
            self.hook(self.payload(prompt="/devflow:plan request", session="../bad session"))


if __name__ == "__main__":
    unittest.main()
