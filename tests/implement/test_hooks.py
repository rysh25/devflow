"""Offline, cross-platform tests of the implement workflow's session-scoped tool boundary.

Guard rules are exercised directly through the shared guard with a synthetic
run state; session capture runs real hooks through the router against real
runs started the documented way (prompt hook, then the runner's start).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from devflow_core import router
from devflow_core.common import guard, sessions
from devflow_core.common.errors import DevFlowError, GuardError
from devflow_core.common.runs import TERMINAL, RunStore
from devflow_core.common.storage import atomic_write_json, read_json
from devflow_core.implement import WORKFLOW
from devflow_core.implement.engine import Engine
from devflow_core.implement.workflow import EDIT_STATUSES
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
        # A fake installed plugin: the guard only needs its runner path.
        self.plugin = self.base / "plugin with spaces"
        self.script = self.plugin / "scripts" / "devflow.py"
        self.script.parent.mkdir(parents=True)
        self.script.write_text("# trusted runner\n", encoding="utf-8")
        self.plan = self.project / "feature.md"
        self.plan.write_text("Implement a tested feature.\n", encoding="utf-8")
        self.session_id = "claude-session-123"
        # Synthetic bound state for direct guard decisions (``pending`` set means a pending start).
        self.pending = None
        self.state = {
            "run_id": RUN_ID, "project": str(self.project), "owner_session": self.session_id,
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

    def guard(self, tool="Read", plugin=None, **tool_input):
        payload = self.payload(tool=tool, **tool_input)
        plugin = plugin or self.plugin
        if self.pending is not None:
            return guard.pending_decision(payload, self.project, plugin, self.pending, WORKFLOW, WORKFLOWS)
        return guard.bound_decision(payload, self.project, plugin, self.state, self.run, WORKFLOW, WORKFLOWS)

    def pend(self, plan=None):
        """Make the synthetic session wait for an implement start of ``plan``."""
        self.state = None
        self.pending = {"flow": "implement", "run_id": RUN_ID,
                        "args": WORKFLOW.normalize_args(str(plan or self.plan), self.project),
                        "event": {"event_id": EVENT, "recorded_at": "2026-11-01T12:34:56+00:00",
                                  "kind": "prompt", "payload": {}},
                        "created_at": "2026-11-01T12:34:56+00:00"}

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

    def assertDenied(self, result):
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny", result)
        self.assertEqual(result["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertTrue(result["hookSpecificOutput"]["permissionDecisionReason"].startswith("DevFlow guard: "))

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
        """Fixture-only state change through the run store's own transaction."""
        with self.store.lock(self.run_id):
            state = self.store.load(self.run_id)
            state["status"] = status
            state["inflight"] = ({"kind": "check", "number": 1, "reviewer_id": None, "target": "fixture",
                                  "started_at": "2026-11-01T12:34:56+00:00"}
                                 if status in WORKFLOW.inflight_statuses else None)
            self.store.save(state)

    def escalate(self):
        path = self.draft / "questions.json"
        atomic_write_json(path, {"reason": "A product decision is required.", "questions": ["Continue?"]})
        return Engine(self.project).escalate(self.run_id, self.session_id, path)


class ToolGuardTests(Fixture):
    def test_read_and_task_metadata_do_not_grant_permissions(self):
        for tool in ("Read", "Grep", "Glob", "LS", "TodoWrite", "TaskCreate", "TaskUpdate", "TaskGet", "TaskList", "ToolSearch"):
            with self.subTest(tool=tool):
                self.assertIsNone(self.guard(tool))

    def test_question_cannot_be_self_answered(self):
        self.assertIsNone(self.guard("AskUserQuestion", questions=[]))
        self.assertDenied(self.guard("AskUserQuestion", answers={"Approve?": "Yes"}))
        self.assertDenied(self.guard("AskUserQuestion", annotations={"Approve?": {"notes": "Approved"}}))

    def test_read_only_search_commands_allowed_but_cannot_run_programs(self):
        for command in ("grep -rn 'def (main|run)' src", "find . -name '*.py'", "ls", "cat README.md"):
            with self.subTest(command=command):
                self.assertIsNone(self.guard("Bash", command=command))
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
                    self.assertIsNone(self.guard(tool, file_path=str(self.project / "src" / "feature.py")))
        for status in ("ready_check", "checking", "ready_review", "reviewing", "awaiting_human", "paused_error",
                       "completed", "exhausted", "cancelled", "unknown"):
            self.state["status"] = status
            with self.subTest(status=status):
                self.assertDenied(self.guard("Write", file_path=str(self.project / "src" / "feature.py")))

    def test_draft_remains_writable_for_decisions_and_escalations(self):
        for status in sorted(WORKFLOW.statuses | {"unknown"}):
            self.state["status"] = status
            with self.subTest(status=status):
                self.assertIsNone(self.guard("Write", file_path=str(self.draft / "decisions.json")))
        self.assertIsNone(self.guard("NotebookEdit", notebook_path=str(self.draft / "note.ipynb")))

    def test_audit_configuration_instruction_and_plan_paths_are_protected(self):
        for relative in (
            "devflow.config.json", "feature.md", ".git/config", ".gitignore", ".gitattributes",
            ".claude/settings.json", ".codex/config.toml", ".opencode/opencode.json", "opencode.json",
            ".agents/skills/bad/SKILL.md", "AGENTS.md", "nested/CLAUDE.md", ".mcp.json",
            f".devflow/runs/implement/{RUN_ID}/state.json", ".devflow/dispatch.lock",
            ".devflow/sessions/bad.json", ".devflow/runs/plan/other/draft/x.md",
        ):
            with self.subTest(relative=relative):
                self.assertDenied(self.guard("Write", file_path=str(self.project / relative)))

    def test_protected_names_are_case_insensitive(self):
        self.assertDenied(self.guard("Write", file_path=str(self.project / ".ClAuDe" / "config.json")))
        self.assertDenied(self.guard("Write", file_path=str(self.project / "agents.MD")))

    def test_installed_plugin_inside_project_is_protected(self):
        plugin_inside = self.project / "tools" / "devflow-plugin"
        result = self.guard("Write", plugin=plugin_inside, file_path=str(plugin_inside / "scripts" / "devflow.py"))
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
        self.assertIsNone(self.guard("Write", file_path=str(self.project / "src" / "x.py")))
        self.assertDenied(self.guard("Write", file_path=str(self.project / "docs" / "x.md")))
        self.state["config"]["snapshot"]["exclude"] = ["src/generated/**"]
        self.assertDenied(self.guard("Write", file_path=str(self.project / "src" / "generated" / "x.py")))

    def test_git_ignored_new_file_denied(self):
        self.state["config"]["snapshot"]["mode"] = "git"
        with patch("devflow_core.implement.workflow.subprocess.run") as execute:
            for code, allowed in ((0, False), (1, True), (128, False)):
                execute.return_value = subprocess.CompletedProcess([], code)
                result = self.guard("Write", file_path=str(self.project / "src" / "x.py"))
                if allowed:
                    self.assertIsNone(result)
                else:
                    self.assertDenied(result)
            self.assertFalse(execute.call_args.kwargs["shell"])
            self.assertEqual(execute.call_args.kwargs["timeout"], 10)

    def test_git_error_denies_write(self):
        self.state["config"]["snapshot"]["mode"] = "git"
        with patch("devflow_core.implement.workflow.subprocess.run", side_effect=FileNotFoundError("git missing")):
            self.assertDenied(self.guard("Write", file_path=str(self.project / "src" / "x.py")))


class RunnerGuardTests(Fixture):
    def test_documented_commands_are_permitted_without_permission_grant(self):
        commands = [
            ("doctor",), ("status",), ("check",), ("review",),
            ("checkpoint", "--summary-file", str(self.draft / "summary.md")),
            ("reflect", "--decisions-file", str(self.draft / "decisions.json")),
            ("escalate", "--questions-file", str(self.draft / "questions.json")),
            ("resolve", "--event-id", EVENT, "--decision", "continue", "--reason", "User answered"),
            ("cancel", "--reason", "User cancelled"),
        ]
        for args in commands:
            for shell in ("Bash", "PowerShell"):
                with self.subTest(args=args, shell=shell):
                    self.assertIsNone(self.guard(shell, command=self.runner(*args)))

    def test_explicit_run_is_bound(self):
        self.assertIsNone(self.guard("Bash", command=self.runner("--run", RUN_ID, "status")))
        self.assertDenied(self.guard("Bash", command=self.runner("--run", "another-run", "status")))

    def test_runner_has_no_resume_action(self):
        # Resume happens only in the hook for the user's /devflow:resume prompt.
        self.assertDenied(self.guard("Bash", command=self.runner("--run", RUN_ID, "resume")))
        self.assertDenied(self.guard("Bash", command=self.runner("resume")))
        self.assertDenied(self.guard("Bash", command=self.runner("resume", flow=None)))

    def test_human_only_hook_and_unknown_commands_denied(self):
        for args in (("recover", "--confirm-process-stopped"), ("hook",), ("start", "--plan", str(self.plan)), ("arbitrary",)):
            with self.subTest(args=args):
                self.assertDenied(self.guard("Bash", command=self.runner(*args)))
        self.assertDenied(self.guard("Bash", command=self.runner("hook", flow=None)))

    def test_file_arguments_must_be_current_run_drafts(self):
        self.assertDenied(self.guard("Bash", command=self.runner("checkpoint", "--summary-file", str(self.plan))))
        self.assertDenied(self.guard("Bash", command=self.runner("reflect", "--decisions-file", str(self.run / "state.json"))))

    def test_bad_runner_arguments_denied(self):
        for args in (
            ("review", "--model", "different"), ("check", "--command", "evil"),
            ("reflect",), ("reflect", "--decisions-file", str(self.draft / "x.json"), "--decisions-file", str(self.draft / "y.json")),
            ("resolve", "--event-id", "not-a-real-id", "--decision", "continue", "--reason", "reason"),
            ("resolve", "--event-id", "evt-" + EVENT, "--decision", "continue", "--reason", "reason"),
            ("resolve", "--event-id", EVENT, "--decision", "approve-all", "--reason", "reason"),
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

    def test_unified_runner_cannot_act_on_another_flow_or_python_pin(self):
        safe = self.runner("status")
        self.assertIsNone(self.guard("Bash", command=safe))
        for command in (
            self.runner("review", flow="plan"),
            self.runner("cancel", flow="plan"),
            self.runner("plan", "--file", str(self.draft / "plan.md"), flow="plan"),
            safe.replace("--no-python-downloads", "--python 3.11"),
            safe.replace("--no-python-downloads ", ""),
            safe.replace("--no-python-downloads", "--no-python-downloads --python 3.11"),
        ):
            with self.subTest(command=command):
                self.assertDenied(self.guard("Bash", command=command))
        # Read-only diagnostics never act on a run, so they remain available.
        for command in (self.runner("status", flow=None), self.runner("doctor", flow=None),
                        self.runner("status", flow="plan")):
            with self.subTest(command=command):
                self.assertIsNone(self.guard("Bash", command=command))

    def test_entire_installed_plugin_is_read_only_not_just_this_component(self):
        root = self.project / "installed" / "devflow"
        for relative in ("scripts/devflow.py", "scripts/devflow_core/plan/defaults.json",
                         "scripts/devflow_core/implement/workflow.py", "skills/plan/SKILL.md"):
            with self.subTest(relative=relative):
                self.assertDenied(self.guard("Write", plugin=root, file_path=str(root / relative)))

    def test_background_and_sandbox_bypass_denied(self):
        safe = self.runner("review")
        self.assertDenied(self.guard("Bash", command=safe, run_in_background=True))
        self.assertDenied(self.guard("Bash", command=safe, dangerouslyDisableSandbox=True))

    def test_help_never_runs_anything_so_any_help_is_allowed(self):
        for action in ("check", "review", "hook", "arbitrary"):
            with self.subTest(action=action):
                self.assertIsNone(self.guard("Bash", command=self.runner(action, "--help")))
        self.assertDenied(self.guard("Bash", command=self.runner("hook")))
        self.assertDenied(self.guard("Bash", command=self.runner("arbitrary")))

    def test_help_for_status_and_doctor_actions_is_permitted(self):
        # --help never executes anything, for any documented action, bound or pending.
        for pending in (False, True):
            if pending:
                self.pend()
            for action in ("status", "doctor"):
                with self.subTest(pending=pending, action=action):
                    self.assertIsNone(self.guard("Bash", command=self.runner(action, "--help")))

    def test_pending_start_is_bound_to_original_plan(self):
        self.pend()
        self.assertIsNone(self.guard("Bash", command=self.runner("start", "--plan", str(self.plan))))
        self.assertDenied(self.guard("Bash", command=self.runner("start", "--plan", str(self.project / "other.md"))))
        self.assertDenied(self.guard("Write", file_path=str(self.project / "x.py")))
        self.assertDenied(self.guard("Write", file_path=str(self.draft / "x.json")))
        self.assertDenied(self.guard("Bash", command=self.runner("review")))
        self.assertIsNone(self.guard("Bash", command=self.runner("doctor")))

    def test_pending_start_permits_only_read_only_work_diagnostics_help_and_the_exact_start(self):
        self.pend()
        allowed = [("Read", {"file_path": str(self.plan)}), ("Grep", {"pattern": "x"}),
                   ("Bash", {"command": "ls"}),
                   ("Bash", {"command": self.runner("status", flow=None)}),
                   ("Bash", {"command": self.runner("doctor", flow=None)}),
                   ("Bash", {"command": self.runner("status")}),
                   ("Bash", {"command": self.runner("start", "--help")}),
                   # "./feature.md", "feature.md" and the absolute path name the same plan.
                   ("Bash", {"command": self.runner("start", "--plan", "feature.md")}),
                   ("Bash", {"command": self.runner("start", "--plan", "./feature.md")}),
                   ("PowerShell", {"command": self.runner("start", "--plan", str(self.plan))})]
        for tool, data in allowed:
            with self.subTest(tool=tool, data=data):
                self.assertIsNone(self.guard(tool, **data))
        denied = [("AskUserQuestion", {"questions": []}), ("Skill", {"skill": "anything"}),
                  ("Write", {"file_path": str(self.plan)}), ("Edit", {"file_path": str(self.project / "x.py")}),
                  ("MultiEdit", {"file_path": str(self.project / "x.py")}),
                  ("NotebookEdit", {"notebook_path": str(self.draft / "x.ipynb")}),
                  ("Bash", {"command": "rm -rf src"}), ("Bash", {"command": self.runner("check")}),
                  ("Bash", {"command": self.runner("--run", RUN_ID, "start", "--plan", str(self.plan))}),
                  ("Bash", {"command": self.runner("start", "--plan", "other.md")}),
                  ("Bash", {"command": self.runner("cancel", "--reason", "x", flow="plan")}),
                  ("WebFetch", {"url": "https://example.invalid"})]
        for tool, data in denied:
            with self.subTest(tool=tool, data=data):
                self.assertDenied(self.guard(tool, **data))

    def test_pending_resume_is_bound_to_user_selected_run(self):
        # The user's /devflow:resume names the run; the hook moves exactly that run.
        status = self.bind()
        other = "claude-session-456"
        with self.assertRaises(DevFlowError):
            self.submit("/devflow:resume " + RUN_ID, other)
        self.assertIsNone(self.record(other))
        self.assertIsNotNone(self.submit("/devflow:resume " + status["run_id"], other))
        self.assertEqual(self.record(other)["run"], {"flow": "implement", "run_id": status["run_id"]})
        self.assertEqual(self.stored()["owner_session"], other)
        self.assertDenied(self.tool("Bash", other, command=self.runner("resume").replace(self.session_id, other)))


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
        self.assertDenied(self.hook(self.payload(tool="Write", file_path=str(self.project / "x.py"))))
        self.assertDenied(self.hook(self.payload(tool="AskUserQuestion", questions=[])))
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

    def test_missing_or_malformed_start_rejected(self):
        for prompt in ("/devflow:implement", "/devflow:implement --plan other", '/devflow:implement "unterminated', "/devflow:implement foo\n/other:skill"):
            with self.subTest(prompt=prompt), self.assertRaises(GuardError):
                self.submit(prompt)
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
            with self.subTest(argument=argument), self.assertRaises(DevFlowError):
                self.submit("/devflow:resume " + argument, "claude-session-789")
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

    def test_draft_writable_after_start_in_every_status_and_denied_before(self):
        self.submit("/devflow:implement feature.md")
        pending = self.record()["pending_start"]
        draft = self.project / ".devflow" / "runs" / "implement" / pending["run_id"] / "draft" / "decisions.json"
        source = self.project / "src" / "feature.py"
        self.assertDenied(self.tool("Write", file_path=str(draft)))
        self.assertDenied(self.tool("Write", file_path=str(source)))
        self.bind(submit=False)
        self.assertEqual(self.draft, draft.parent)
        for status in sorted(WORKFLOW.statuses):
            self.set_status(status)
            with self.subTest(status=status):
                self.assertIsNone(self.tool("Write", file_path=str(draft)))
                result = self.tool("Write", file_path=str(source))
                if status in EDIT_STATUSES:
                    self.assertIsNone(result)
                else:
                    self.assertDenied(result)
        self.assertTrue(TERMINAL <= WORKFLOW.statuses)

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
        self.assertDenied(self.hook(before))
        events = self.stored()["event_ids"]
        self.assertIsNone(self.submit("Now explain the implementation"))
        self.assertIsNone(self.record())
        # The prompt that releases a finished run is not recorded into it.
        self.assertEqual(self.stored()["event_ids"], events)
        self.assertIsNone(self.hook(before))

    def test_running_flow_cannot_be_replaced_by_new_start(self):
        self.bind()
        (self.project / "other.md").write_text("Other plan.\n", encoding="utf-8")
        with self.assertRaisesRegex(DevFlowError, "Finish or cancel"):
            self.submit("/devflow:implement other.md")
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
        with self.assertRaisesRegex(DevFlowError, "different content"):
            self.hook(payload)
        self.assertEqual(len(self.stored()["event_ids"]), 2)

    def test_answers_filled_in_by_question_screen_are_recorded(self):
        # Claude Code's question screen writes the human's choice into
        # tool_input before PostToolUse; model prefills are denied at PreToolUse.
        self.bind()
        self.escalate()
        payload = self.question()
        payload["tool_input"].update({"answers": {"Continue?": "Yes, keep compatibility"}, "annotations": {}})
        self.hook(payload)
        self.assertEqual(len(self.stored()["event_ids"]), 2)
        self.assertEqual(self.events()[-1]["payload"]["tool_input"], payload["tool_input"])
        self.assertEqual([item["event_id"] for item in self.candidates()], [self.stored()["event_ids"][-1]])

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
                self.hook(payload)
                self.assertEqual(self.events()[-1]["payload"]["tool_response"], response)
                self.assertEqual(self.candidates(), [])
        self.assertEqual(len(self.stored()["event_ids"]), 1 + len(responses))
        for event_id in self.stored()["event_ids"][1:]:
            with self.assertRaises(DevFlowError):
                Engine(self.project).resolve(self.run_id, self.session_id, event_id, "continue", "Not an answer.")
        self.hook(dict(self.question("tool-text"), tool_response="Yes, keep compatibility"))
        self.assertEqual([item["answer_text"] for item in self.candidates()], ["Yes, keep compatibility"])

    def test_top_level_question_error_cannot_capture_or_replay_an_answer(self):
        self.bind()
        self.escalate()
        failed = dict(self.question("failed-call"), is_error=True)
        self.hook(failed)
        self.assertEqual(self.candidates(), [])
        # A failed call cannot be replayed as a successful answer.
        succeeded = dict(failed)
        succeeded.pop("is_error")
        with self.assertRaisesRegex(DevFlowError, "different content"):
            self.hook(succeeded)
        self.assertEqual(self.candidates(), [])
        # Nor can a successful answer be replayed as a failure.
        answered = self.question("answered-call")
        self.hook(answered)
        with self.assertRaisesRegex(DevFlowError, "different content"):
            self.hook(dict(answered, is_error=True))
        self.assertEqual(len(self.candidates()), 1)
        self.assertEqual(len(self.stored()["event_ids"]), 3)

    def test_nonempty_question_text_blocks_preserved(self):
        self.bind()
        payload = self.question()
        payload["tool_response"] = [{"type": "text", "text": "Keep the existing API."}]
        self.hook(payload)
        self.assertEqual(self.events()[-1]["payload"]["tool_response"], payload["tool_response"])

    def test_oversize_event_not_silently_truncated(self):
        self.bind()
        with patch("devflow_core.common.runs.MAX_EVENT_BYTES", 30), self.assertRaisesRegex(DevFlowError, "exceeds"):
            self.submit("A message beyond the configured capture budget")
        self.assertEqual(len(self.stored()["event_ids"]), 1)

    def test_stored_run_identity_must_match_session(self):
        self.bind()
        # Another session explicitly resumes the run; this session's pointer goes stale.
        self.submit("/devflow:resume " + self.run_id, "claude-session-456")
        self.assertEqual(self.stored()["owner_session"], "claude-session-456")
        self.assertDenied(self.hook(self.payload()))
        with self.assertRaisesRegex(DevFlowError, "another session"):
            self.submit("Continue the work")
        with self.assertRaises(DevFlowError):
            self.hook(self.question())
        with self.assertRaises(DevFlowError):
            self.hook(self.payload(event="Stop"))
        self.assertEqual(len(self.stored()["event_ids"]), 2)

    def test_unknown_events_and_nonquestion_results_inert(self):
        self.assertIsNone(self.hook(self.payload(event="Stop")))
        self.assertIsNone(self.hook(self.payload(event="PostToolUse", tool="Write")))
        self.assertIsNone(self.hook(self.payload(event="SessionStart")))
        self.bind()
        self.assertIsNone(self.hook(self.payload(event="PostToolUse", tool="Write")))
        self.assertEqual(len(self.stored()["event_ids"]), 1)
        self.assertIsNone(self.hook(self.payload(event="Stop")))
        self.assertEqual(self.events()[-1]["kind"], "stop")

    def test_hook_configuration_uses_exec_form_and_no_stop_loop(self):
        config = json.loads((PACKAGE.parents[2] / "hooks" / "hooks.json").read_text(encoding="utf-8"))
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
