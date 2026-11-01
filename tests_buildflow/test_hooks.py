"""Offline, cross-platform tests of BuildFlow's session-scoped tool boundary."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "devflow" / "components" / "buildflow"
sys.path.insert(0, str(PLUGIN / "scripts"))

from buildflow_core.hooks import HookError, guard_tool, handle, handle_hook  # noqa: E402
from buildflow_core.storage import atomic_write_json, read_json, session_path  # noqa: E402


RUN_ID = "20261101T123456Z-123456abcdef"


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="buildflow hooks ")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.project = self.base / "project with spaces"
        self.project.mkdir()
        (self.project / "devflow.config.json").write_text(
            '{"schema_version":1,"implement":{"snapshot":{"mode":"files"}}}\n', encoding="utf-8",
        )
        self.plugin = self.base / "plugin with spaces" / "components" / "buildflow"
        (self.plugin / "scripts").mkdir(parents=True)
        self.script = self.plugin.parents[1] / "scripts" / "devflow.py"
        self.script.parent.mkdir(parents=True)
        self.script.write_text("# trusted runner\n", encoding="utf-8")
        self.plan = self.project / "feature.md"
        self.plan.write_text("Implement a tested feature.\n", encoding="utf-8")
        self.session_id = "claude-session-123"
        self.session = {
            "session_id": self.session_id, "active_run": RUN_ID, "intent": None,
            "seq": 1, "prompt_seq": 1, "events": [],
        }
        self.state = {
            "run_id": RUN_ID, "project": str(self.project), "session_id": self.session_id,
            "status": "implementing", "plan_source": str(self.plan),
            "config": {"snapshot": {"mode": "files", "include": ["**"], "exclude": []}},
        }
        self.run = self.project / ".devflow" / "runs" / "implement" / RUN_ID
        self.draft = self.run / "draft"

    def payload(self, event="PreToolUse", tool="Read", **tool_input):
        return {
            "hook_event_name": event, "cwd": str(self.project), "session_id": self.session_id,
            "tool_name": tool, "tool_input": tool_input,
        }

    def guard(self, tool="Read", **tool_input):
        return guard_tool(self.payload(tool=tool, **tool_input), self.project, self.session, self.state, self.plugin)

    def runner(self, *arguments):
        arguments = list(arguments)
        selectors = []
        while arguments and arguments[0] in {"--run", "--session", "--project"}:
            selectors += arguments[:2]
            arguments = arguments[2:]
        return " ".join(shlex.quote(value) for value in [
            "uv", "run", "--offline", "--no-project", "--no-config", "--no-python-downloads",
            str(self.script), "--project", str(self.project), "--session", self.session_id,
            *selectors, "implement", *arguments,
        ])

    def assertDenied(self, result):
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(result["hookSpecificOutput"]["hookEventName"], "PreToolUse")

    def bind(self):
        self.draft.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.run / "state.json", self.state)
        atomic_write_json(session_path(self.project, self.session_id), self.session)


class ToolGuardTests(Fixture):
    def test_read_and_task_metadata_do_not_grant_permissions(self):
        for tool in ("Read", "Grep", "Glob", "LS", "TodoWrite", "TaskCreate", "TaskUpdate", "TaskGet", "TaskList", "ToolSearch"):
            with self.subTest(tool=tool):
                self.assertEqual(self.guard(tool), {})

    def test_question_cannot_be_self_answered(self):
        self.assertEqual(self.guard("AskUserQuestion", questions=[]), {})
        self.assertDenied(self.guard("AskUserQuestion", answers={"Approve?": "Yes"}))
        self.assertDenied(self.guard("AskUserQuestion", annotations={"Approve?": {"notes": "Approved"}}))

    def test_read_only_search_commands_allowed_but_cannot_run_programs(self):
        for command in ("grep -rn 'def (main|run)' src", "find . -name '*.py'", "ls", "cat README.md"):
            with self.subTest(command=command):
                self.assertEqual(self.guard("Bash", command=command), {})
        for command in ("find . -exec sh -c x \\;", "rg --pre sh x", "grep x . | sh", "cat a > b"):
            with self.subTest(command=command):
                self.assertDenied(self.guard("Bash", command=command))
        self.assertDenied(self.guard("PowerShell", command="ls"))

    def test_runner_path_mismatch_names_the_installed_runner(self):
        stale = self.runner("status").replace(str(self.script), "/old/plugin/0.1.0/scripts/devflow.py")
        result = self.guard("Bash", command=stale)
        self.assertDenied(result)
        self.assertIn(str(self.script), result["hookSpecificOutput"]["permissionDecisionReason"])

    def test_unknown_external_agent_and_skill_tools_denied(self):
        for tool in ("Agent", "Task", "Skill", "WebSearch", "WebFetch", "mcp__files__write", "Unknown"):
            with self.subTest(tool=tool):
                self.assertDenied(self.guard(tool))

    def test_source_write_allowed_only_in_edit_phases(self):
        for status in ("implementing", "needs_revision"):
            self.state["status"] = status
            for tool in ("Write", "Edit", "MultiEdit"):
                with self.subTest(status=status, tool=tool):
                    self.assertEqual(self.guard(tool, file_path=str(self.project / "src" / "feature.py")), {})
        for status in ("checking", "ready_review", "reviewing", "awaiting_human", "paused_error", "completed", "exhausted", "cancelled", "unknown"):
            self.state["status"] = status
            with self.subTest(status=status):
                self.assertDenied(self.guard("Write", file_path=str(self.project / "src" / "feature.py")))

    def test_draft_remains_writable_for_decisions_and_escalations(self):
        for status in ("implementing", "needs_revision", "awaiting_human", "paused_error", "ready_review"):
            self.state["status"] = status
            self.assertEqual(self.guard("Write", file_path=str(self.draft / "decisions.json")), {})
        self.assertEqual(self.guard("NotebookEdit", notebook_path=str(self.draft / "note.ipynb")), {})

    def test_audit_configuration_instruction_and_plan_paths_are_protected(self):
        for relative in (
            "devflow.config.json", "feature.md", ".git/config", ".gitignore", ".gitattributes",
            ".claude/settings.json", ".codex/config.toml", ".opencode/opencode.json",
            ".agents/skills/bad/SKILL.md", "AGENTS.md", "nested/CLAUDE.md", ".mcp.json",
            f".devflow/runs/implement/{RUN_ID}/state.json", ".devflow/active-implement.json",
            ".devflow/sessions/implement/bad.json", ".planflow/data.json", ".buildflow/data.json",
        ):
            with self.subTest(relative=relative):
                self.assertDenied(self.guard("Write", file_path=str(self.project / relative)))

    def test_protected_names_are_case_insensitive(self):
        self.assertDenied(self.guard("Write", file_path=str(self.project / ".ClAuDe" / "config.json")))
        self.assertDenied(self.guard("Write", file_path=str(self.project / "agents.MD")))

    def test_custom_config_path_and_installed_plugin_are_protected(self):
        self.state["config_path"] = str(self.project / "config" / "workflow.json")
        self.assertDenied(self.guard("Write", file_path=self.state["config_path"]))
        plugin_inside = self.project / "tools" / "buildflow"
        result = guard_tool(self.payload(tool="Write", file_path=str(plugin_inside / "scripts" / "buildflow.py")), self.project, self.session, self.state, plugin_inside)
        self.assertDenied(result)

    def test_outside_parent_paths_and_directories_denied(self):
        for path in (self.base / "outside.py", self.project / ".." / "outside.py", self.project, self.draft, self.run / "draft-other" / "x"):
            with self.subTest(path=path):
                self.assertDenied(self.guard("Write", file_path=str(path)))

    def test_multiedit_cannot_smuggle_protected_path(self):
        self.assertDenied(self.guard("MultiEdit", file_path=str(self.project / "safe.py"), edits=[{"file_path": str(self.plan)}]))
        self.assertDenied(self.guard("MultiEdit", file_path=str(self.project / "safe.py"), edits=["bad"]))
        self.assertDenied(self.guard("NotebookEdit", file_path=str(self.project / "safe.py")))

    def test_symlinked_source_and_draft_denied(self):
        outside = self.base / "outside"
        outside.mkdir()
        source = self.project / "link"
        self.draft.mkdir(parents=True)
        try:
            source.symlink_to(outside, target_is_directory=True)
            (self.draft / "link").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks unavailable on this platform")
        self.assertDenied(self.guard("Write", file_path=str(source / "x.py")))
        self.assertDenied(self.guard("Write", file_path=str(self.draft / "link" / "answer.json")))

    def test_hard_linked_source_denied(self):
        original = self.base / "secret.txt"
        original.write_text("secret", encoding="utf-8")
        alias = self.project / "alias.py"
        try:
            os.link(original, alias)
        except OSError:
            self.skipTest("Hard links unavailable on this platform")
        self.assertDenied(self.guard("Edit", file_path=str(alias)))

    def test_snapshot_scope_is_enforced(self):
        self.state["config"]["snapshot"]["include"] = ["src/**"]
        self.assertEqual(self.guard("Write", file_path=str(self.project / "src" / "x.py")), {})
        self.assertDenied(self.guard("Write", file_path=str(self.project / "docs" / "x.md")))
        self.state["config"]["snapshot"]["exclude"] = ["src/generated/**"]
        self.assertDenied(self.guard("Write", file_path=str(self.project / "src" / "generated" / "x.py")))

    def test_git_ignored_new_file_denied(self):
        self.state["config"]["snapshot"]["mode"] = "git"
        with patch("buildflow_core.hooks.subprocess.run") as execute:
            for code, allowed in ((0, False), (1, True), (128, False)):
                execute.return_value = subprocess.CompletedProcess([], code)
                result = self.guard("Write", file_path=str(self.project / "src" / "x.py"))
                if allowed:
                    self.assertEqual(result, {})
                else:
                    self.assertDenied(result)
            self.assertFalse(execute.call_args.kwargs["shell"])
            self.assertEqual(execute.call_args.kwargs["timeout"], 10)

    def test_git_error_denies_write(self):
        self.state["config"]["snapshot"]["mode"] = "git"
        with patch("buildflow_core.hooks.subprocess.run", side_effect=FileNotFoundError("git missing")):
            self.assertDenied(self.guard("Write", file_path=str(self.project / "src" / "x.py")))


class RunnerGuardTests(Fixture):
    def test_documented_commands_are_permitted_without_permission_grant(self):
        commands = [
            ("doctor",), ("status",), ("check",), ("review",),
            ("checkpoint", "--summary-file", str(self.draft / "summary.md")),
            ("reflect", "--decisions-file", str(self.draft / "decisions.json")),
            ("escalate", "--questions-file", str(self.draft / "questions.json")),
            ("resolve", "--event-id", "evt-" + "a" * 32, "--decision", "continue", "--reason", "User answered"),
            ("cancel", "--reason", "User cancelled"),
        ]
        for args in commands:
            for shell in ("Bash", "PowerShell"):
                with self.subTest(args=args, shell=shell):
                    self.assertEqual(self.guard(shell, command=self.runner(*args)), {})

    def test_explicit_run_is_bound(self):
        self.assertEqual(self.guard("Bash", command=self.runner("--run", RUN_ID, "status")), {})
        self.assertDenied(self.guard("Bash", command=self.runner("--run", "another-run", "status")))

    def test_resume_requires_actual_user_intent(self):
        self.assertDenied(self.guard("Bash", command=self.runner("--run", RUN_ID, "resume")))
        self.session["intent"] = {"action": "resume", "args": RUN_ID}
        self.assertEqual(self.guard("Bash", command=self.runner("--run", RUN_ID, "resume")), {})

    def test_human_only_hook_and_unknown_commands_denied(self):
        for args in (("recover", "--confirm-process-stopped"), ("hook",), ("start", "--plan", str(self.plan)), ("arbitrary",)):
            self.assertDenied(self.guard("Bash", command=self.runner(*args)))

    def test_file_arguments_must_be_current_run_drafts(self):
        self.assertDenied(self.guard("Bash", command=self.runner("checkpoint", "--summary-file", str(self.plan))))
        self.assertDenied(self.guard("Bash", command=self.runner("reflect", "--decisions-file", str(self.run / "state.json"))))

    def test_bad_runner_arguments_denied(self):
        for args in (
            ("review", "--model", "different"), ("check", "--command", "evil"),
            ("reflect",), ("reflect", "--decisions-file", str(self.draft / "x.json"), "--decisions-file", str(self.draft / "y.json")),
            ("resolve", "--event-id", "not-a-real-id", "--decision", "continue", "--reason", "reason"),
            ("resolve", "--event-id", "evt-" + "a" * 32, "--decision", "approve-all", "--reason", "reason"),
            ("status", "--run", RUN_ID), ("cancel", "--reason", ""),
        ):
            with self.subTest(args=args):
                self.assertDenied(self.guard("Bash", command=self.runner(*args)))

    def test_shell_injection_and_wrapper_denied(self):
        safe = self.runner("status")
        for command in (
            safe + "; anything", safe + " && anything", safe + " | anything", safe + " > target",
            safe + " $(anything)", safe + " `anything`", safe + "\nanything", "ENV=x " + safe,
            "bash -c " + shlex.quote(safe), "& " + safe,
        ):
            with self.subTest(command=command):
                self.assertDenied(self.guard("Bash", command=command))

    def test_context_or_script_substitution_denied(self):
        safe = self.runner("status")
        self.assertDenied(self.guard("Bash", command=safe.replace(self.session_id, "another-session")))
        self.assertDenied(self.guard("Bash", command=safe.replace(str(self.project), str(self.base))))
        self.assertDenied(self.guard("Bash", command=safe.replace(str(self.script), str(self.draft / "attack.py"))))
        self.assertDenied(self.guard("Bash", command=safe.replace("--offline ", "")))

    def test_unified_runner_cannot_select_another_flow_or_python_pin(self):
        safe = self.runner("status")
        self.assertEqual(self.guard("Bash", command=safe), {})
        for command in (
            safe.replace(" implement status", " plan status"),
            safe.replace(" implement status", " status"),
            safe.replace("--no-python-downloads", "--python 3.11"),
            safe.replace("--no-python-downloads ", ""),
            safe.replace("--no-python-downloads", "--no-python-downloads --python 3.11"),
        ):
            with self.subTest(command=command):
                self.assertDenied(self.guard("Bash", command=command))

    def test_entire_installed_plugin_is_read_only_not_just_this_component(self):
        root = self.project / "installed" / "devflow"
        component = root / "components" / "buildflow"
        for relative in ("scripts/devflow.py", "components/planflow/config/defaults.json", "skills/plan/SKILL.md"):
            result = guard_tool(self.payload(tool="Write", file_path=str(root / relative)),
                                self.project, self.session, self.state, component)
            self.assertDenied(result)

    def test_background_and_sandbox_bypass_denied(self):
        safe = self.runner("review")
        self.assertDenied(self.guard("Bash", command=safe, run_in_background=True))
        self.assertDenied(self.guard("Bash", command=safe, dangerouslyDisableSandbox=True))

    def test_help_only_for_documented_commands(self):
        self.assertEqual(self.guard("Bash", command=self.runner("status", "--help")), {})
        self.assertDenied(self.guard("Bash", command=self.runner("hook", "--help")))

    def test_pending_start_is_bound_to_original_plan(self):
        self.state = None
        self.session["active_run"] = None
        self.session["intent"] = {"action": "implement", "args": str(self.plan)}
        self.assertEqual(self.guard("Bash", command=self.runner("start", "--plan", str(self.plan))), {})
        self.assertDenied(self.guard("Bash", command=self.runner("start", "--plan", str(self.project / "other.md"))))
        self.assertDenied(self.guard("Write", file_path=str(self.project / "x.py")))
        self.assertDenied(self.guard("Write", file_path=str(self.draft / "x.json")))
        self.assertDenied(self.guard("Bash", command=self.runner("review")))
        self.assertEqual(self.guard("Bash", command=self.runner("doctor")), {})

    def test_pending_resume_is_bound_to_user_selected_run(self):
        self.state = None
        self.session["active_run"] = None
        self.session["intent"] = {"action": "resume", "args": RUN_ID}
        self.assertEqual(self.guard("Bash", command=self.runner("--run", RUN_ID, "resume")), {})
        self.assertDenied(self.guard("Bash", command=self.runner("resume")))
        self.assertDenied(self.guard("Bash", command=self.runner("--run", "different", "resume")))


class SessionCaptureTests(Fixture):
    def submit(self, prompt):
        return handle("UserPromptSubmit", dict(self.payload(event="UserPromptSubmit"), prompt=prompt), self.plugin)

    def test_unrelated_session_is_inert_and_creates_no_directory(self):
        self.assertEqual(self.submit("Please explain this code"), {})
        self.assertEqual(self.submit("/planflow:plan-review a feature"), {})
        self.assertEqual(handle_hook(self.payload(tool="Bash", command="anything"), self.plugin), {})
        self.assertFalse((self.project / ".devflow").exists())

    def test_explicit_start_records_exact_user_text_and_intent(self):
        prompt = '/devflow:implement "feature.md"'
        output = self.submit(prompt)
        stored = read_json(session_path(self.project, self.session_id))
        self.assertEqual(stored["intent"]["args"], "feature.md")
        self.assertEqual(stored["intent"]["action"], "implement")
        self.assertEqual(stored["events"][0]["text"], prompt)
        self.assertEqual(stored["seq"], 1)
        self.assertEqual(stored["prompt_seq"], 1)
        self.assertRegex(stored["events"][0]["event_id"], r"^evt-[a-f0-9]{32}$")
        self.assertIn(stored["events"][0]["event_id"], output["hookSpecificOutput"]["additionalContext"])
        # A resumed conversation may still hold an older plugin path.
        self.assertIn(json.dumps(str(self.script)), output["hookSpecificOutput"]["additionalContext"])
        self.assertEqual((self.project / ".devflow" / ".gitignore").read_text(), "*\n")
        self.assertDenied(handle_hook(self.payload(tool="Write", file_path=str(self.project / "x.py")), self.plugin))

    def test_plan_path_with_spaces_and_native_separators_preserved(self):
        self.submit('/devflow:implement "C:\\projects\\my plan.md"')
        stored = read_json(session_path(self.project, self.session_id))
        self.assertEqual(stored["intent"]["args"], "C:\\projects\\my plan.md")

    def test_claude_file_mention_prefix_is_normalized(self):
        self.submit("/devflow:implement @feature.md")
        stored = read_json(session_path(self.project, self.session_id))
        self.assertEqual(stored["intent"]["args"], "feature.md")

    def test_missing_or_malformed_start_rejected(self):
        for prompt in ("/devflow:implement", "/devflow:implement --plan other", '/devflow:implement "unterminated', "/devflow:implement foo\n/other:skill"):
            with self.subTest(prompt=prompt), self.assertRaises(HookError):
                self.submit(prompt)
        self.assertFalse((self.project / ".devflow").exists())

    def test_resume_intent_records_safe_run_id(self):
        self.submit("/devflow:resume " + RUN_ID)
        stored = read_json(session_path(self.project, self.session_id))
        self.assertEqual(stored["intent"]["action"], "resume")
        self.assertEqual(stored["intent"]["args"], RUN_ID)
        with self.assertRaises(Exception):
            self.submit("/devflow:resume ../other")

    def test_next_real_prompt_can_abandon_pending_start(self):
        self.submit("/devflow:implement feature.md")
        self.assertEqual(self.submit("Cancel that; explain instead"), {})
        stored = read_json(session_path(self.project, self.session_id))
        self.assertIsNone(stored["intent"])
        self.assertEqual(stored["seq"], 2)
        self.assertEqual(handle_hook(self.payload(tool="Bash", command="normal command"), self.plugin), {})

    def test_active_session_other_session_remains_unaffected(self):
        self.bind()
        payload = self.payload(tool="Bash", command="normal command")
        payload["session_id"] = "another-session"
        self.assertEqual(handle_hook(payload, self.plugin), {})
        self.assertFalse(session_path(self.project, "another-session").exists())

    def test_terminal_state_not_released_until_next_real_prompt(self):
        self.state["status"] = "completed"
        self.bind()
        before = self.payload(tool="Write", file_path=str(self.project / "x.py"))
        self.assertDenied(handle_hook(before, self.plugin))
        self.assertEqual(self.submit("Now explain the implementation"), {})
        stored = read_json(session_path(self.project, self.session_id))
        self.assertIsNone(stored["active_run"])
        self.assertIsNone(stored["intent"])
        self.assertEqual(stored["events"][-1]["text"], "Now explain the implementation")
        self.assertEqual(handle_hook(before, self.plugin), {})

    def test_running_flow_cannot_be_replaced_by_new_start(self):
        self.bind()
        output = self.submit("/devflow:implement other.md")
        self.assertEqual(output["decision"], "block")
        self.assertEqual(read_json(session_path(self.project, self.session_id))["active_run"], RUN_ID)

    def test_same_running_flow_can_receive_resume_intent(self):
        self.bind()
        self.submit("/devflow:resume " + RUN_ID)
        self.assertEqual(read_json(session_path(self.project, self.session_id))["intent"]["args"], RUN_ID)

    def test_user_response_retains_original_text(self):
        self.state["status"] = "awaiting_human"
        self.bind()
        original = "既存 API は変更せず、互換レイヤーを追加してください。"
        self.submit(original)
        stored = read_json(session_path(self.project, self.session_id))
        self.assertEqual(stored["events"][-1]["text"], original)
        self.assertEqual(stored["events"][-1]["kind"], "user_prompt")
        self.assertEqual(stored["events"][-1]["seq"], 2)

    def question(self):
        payload = self.payload(event="PostToolUse", tool="AskUserQuestion", questions=[{"question": "Continue?"}])
        payload.update({"tool_use_id": "tool-123", "tool_response": {"answers": {"Continue?": "Yes, keep compatibility"}}})
        return payload

    def test_question_response_is_stored_verbatim_and_deduplicated(self):
        self.bind()
        payload = self.question()
        first = handle_hook(payload, self.plugin)
        second = handle_hook(payload, self.plugin)
        stored = read_json(session_path(self.project, self.session_id))
        self.assertEqual(len(stored["events"]), 1)
        self.assertEqual(stored["events"][0]["tool_response"], payload["tool_response"])
        self.assertEqual(stored["events"][0]["tool_input"], payload["tool_input"])
        self.assertEqual(first, second)
        self.assertEqual(stored["prompt_seq"], 1)
        self.assertEqual(stored["seq"], 2)

    def test_question_replay_with_changed_result_rejected(self):
        self.bind()
        payload = self.question()
        handle_hook(payload, self.plugin)
        payload["tool_response"] = "different answer"
        with self.assertRaises(HookError):
            handle_hook(payload, self.plugin)

    def test_answers_filled_in_by_question_screen_are_recorded(self):
        # Claude Code's question screen writes the human's choice into
        # tool_input before PostToolUse; model prefills are denied at PreToolUse.
        self.bind()
        payload = self.question()
        payload["tool_input"].update({"answers": {"Continue?": "Yes, keep compatibility"}, "annotations": {}})
        handle_hook(payload, self.plugin)
        stored = read_json(session_path(self.project, self.session_id))
        self.assertEqual(stored["seq"], 2)
        self.assertEqual(stored["events"][0]["tool_input"], payload["tool_input"])

    def test_empty_cancelled_or_failed_question_is_not_an_answer(self):
        self.bind()
        for response in (None, "", "   ", [], {}, {"answers": {}},
                         {"cancelled": True, "answers": {"Continue?": "yes"}},
                         {"is_error": True}, {"answers": {"Continue?": ""}}):
            payload = self.question()
            payload["tool_response"] = response
            with self.subTest(response=response), self.assertRaises(HookError):
                handle_hook(payload, self.plugin)
        self.assertEqual(read_json(session_path(self.project, self.session_id))["seq"], 1)

    def test_top_level_question_error_cannot_capture_or_replay_an_answer(self):
        self.bind()
        payload = self.question()
        payload["is_error"] = True
        with self.assertRaises(HookError):
            handle_hook(payload, self.plugin)
        self.assertEqual(read_json(session_path(self.project, self.session_id))["seq"], 1)
        payload.pop("is_error")
        handle_hook(payload, self.plugin)
        payload["is_error"] = True
        with self.assertRaises(HookError):
            handle_hook(payload, self.plugin)
        self.assertEqual(read_json(session_path(self.project, self.session_id))["seq"], 2)

    def test_nonempty_question_text_blocks_preserved(self):
        self.bind()
        payload = self.question()
        payload["tool_response"] = [{"type": "text", "text": "Keep the existing API."}]
        handle_hook(payload, self.plugin)
        stored = read_json(session_path(self.project, self.session_id))
        self.assertEqual(stored["events"][0]["tool_response"], payload["tool_response"])

    def test_oversize_event_not_silently_truncated(self):
        self.bind()
        with patch("buildflow_core.hooks.MAX_EVENT_BYTES", 30), self.assertRaises(HookError):
            self.submit("A message beyond the configured capture budget")
        self.assertEqual(read_json(session_path(self.project, self.session_id))["seq"], 1)

    def test_stored_run_identity_must_match_session(self):
        self.state["session_id"] = "other-session"
        self.bind()
        with self.assertRaises(HookError):
            handle_hook(self.payload(), self.plugin)

    def test_unknown_events_and_nonquestion_results_inert(self):
        self.assertEqual(handle("Stop", self.payload(event="Stop"), self.plugin), {})
        self.assertEqual(handle("PostToolUse", self.payload(event="PostToolUse", tool="Write"), self.plugin), {})

    def test_hook_configuration_uses_exec_form_and_no_stop_loop(self):
        config = json.loads((PLUGIN.parents[1] / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(set(config["hooks"]), {"UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"})
        for event, matchers in config["hooks"].items():
            for matcher in matchers:
                for hook in matcher["hooks"]:
                    self.assertEqual(hook["command"], "uv")
                    self.assertIsInstance(hook["args"], list)
                    if event == "Stop":
                        self.assertNotEqual(hook.get("onFailure"), "block")
                        self.assertEqual(hook["timeout"], 15)
                    else:
                        self.assertEqual(hook["onFailure"], "block")
                        self.assertEqual(hook["timeout"], 30)
                    self.assertEqual(hook["args"][-1], "hook")
                    self.assertIn("${CLAUDE_PLUGIN_ROOT}/scripts/devflow.py", hook["args"])
                    self.assertIn("--no-python-downloads", hook["args"])
                    self.assertNotIn("--python", hook["args"])


if __name__ == "__main__":
    unittest.main()
