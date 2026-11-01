"""Offline integration: one plugin, independent workflows, hooks and engines.

Reviewer API dispatch is always mocked. Project checks run only synthetic Python
programs inside fresh temporary project directories; no external model is used.
"""
from __future__ import annotations

import ast
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "devflow"
BUILD_PLUGIN = PLUGIN / "components" / "buildflow"
PLAN_PLUGIN = PLUGIN / "components" / "planflow"
for plugin in (BUILD_PLUGIN, PLAN_PLUGIN):
    sys.path.insert(0, str(plugin / "scripts"))

from buildflow_core import cli as build_cli  # noqa: E402
from buildflow_core.config import validate_config, load_config as load_build_config  # noqa: E402
from buildflow_core.engine import Engine as BuildEngine  # noqa: E402
from buildflow_core.hooks import handle_hook as build_hook  # noqa: E402
from buildflow_core.storage import BuildFlowError, read_json, session_path  # noqa: E402
from planflow_core.engine import Engine as PlanEngine  # noqa: E402
from planflow_core.config import load_config as load_plan_config  # noqa: E402
from planflow_core.hooks import handle_hook as plan_hook  # noqa: E402


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def review_pass(provider, packet_path, *_):
    packet = json.loads(Path(packet_path).read_text(encoding="utf-8"))
    return {"schema_version": 1, "snapshot_id": packet["snapshot_id"], "verdict": "pass",
            "summary": "Synthetic review: no unresolved findings.", "findings": [], "questions": []}


class BundleContractTests(unittest.TestCase):
    def test_marketplace_and_plugin_names_match_exact_sources(self):
        marketplace = read_json(ROOT / ".claude-plugin" / "marketplace.json")
        self.assertEqual(marketplace["name"], "devflow-marketplace")
        entries = marketplace["plugins"]
        self.assertEqual({entry["name"] for entry in entries}, {"devflow"})
        self.assertEqual(len(entries), 1)
        for entry in entries:
            path = ROOT / entry["source"]
            self.assertTrue(path.resolve().is_relative_to(ROOT.resolve()))
            manifest = read_json(path / ".claude-plugin" / "plugin.json")
            self.assertEqual(entry["name"], manifest["name"])

    def test_one_plugin_has_no_sibling_installation_dependencies(self):
        meta = read_json(ROOT / "plugins" / "devflow" / ".claude-plugin" / "plugin.json")
        self.assertFalse(meta.get("dependencies"))
        for plugin in (BUILD_PLUGIN, PLAN_PLUGIN):
            self.assertTrue((plugin / "scripts").is_dir())
            self.assertFalse((plugin / ".claude-plugin" / "plugin.json").exists())

    def test_plugins_do_not_import_each_other_or_build_time_shared_package(self):
        for plugin, forbidden in ((BUILD_PLUGIN, "planflow_core"), (PLAN_PLUGIN, "buildflow_core")):
            for source in (plugin / "scripts").rglob("*.py"):
                tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        names = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        names = [node.module or ""]
                    else:
                        continue
                    for name in names:
                        with self.subTest(source=source, module=name):
                            self.assertNotEqual(name.split(".")[0], forbidden)
                            self.assertFalse(name.startswith("shared."))

    def test_shared_source_is_identically_vendored_into_each_plugin(self):
        for source in (ROOT / "shared" / "flow_common").glob("*.py"):
            for plugin in (BUILD_PLUGIN, PLAN_PLUGIN):
                target = plugin / "scripts" / (plugin.name + "_core") / "_vendor" / "flow_common" / source.name
                with self.subTest(target=target):
                    self.assertEqual(target.read_bytes(), source.read_bytes())

    def test_standalone_import_has_no_sibling_plugin_dependency(self):
        for plugin, own, other in ((BUILD_PLUGIN, "buildflow_core", "planflow_core"), (PLAN_PLUGIN, "planflow_core", "buildflow_core")):
            code = (
                "import sys; sys.path.insert(0, " + repr(str(plugin / "scripts")) + "); "
                "import " + own + ".engine, " + own + ".hooks; "
                "assert not any(x == " + repr(other) + " or x.startswith(" + repr(other + ".") + ") for x in sys.modules)"
            )
            execution = subprocess.run([sys.executable, "-I", "-c", code], cwd=ROOT,
                                       capture_output=True, text=True, timeout=20, shell=False)
            self.assertEqual(execution.returncode, 0, execution.stderr)

    def test_root_commands_are_manual_and_use_unified_runner(self):
        self.assertTrue((BUILD_PLUGIN / "references" / "workflow.md").is_file())
        for name in ("plan", "implement", "resume", "status", "doctor"):
            source = (PLUGIN / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
            frontmatter = source.split("---", 2)[1]
            self.assertIn("name: " + name, frontmatter)
            self.assertIn("disable-model-invocation: true", frontmatter)
            self.assertIn('${CLAUDE_PLUGIN_ROOT}/scripts/devflow.py', source)
            self.assertNotIn("dangerously-skip-permissions", source)
            self.assertNotIn("--python 3.11", source)

    def test_configuration_rejects_advisory_only_validation(self):
        value = read_json(BUILD_PLUGIN / "config" / "defaults.json")
        value["checks"] = [{"id": "advisory", "argv": [sys.executable, "-c", "raise SystemExit(1)"], "required": False}]
        with self.assertRaises(BuildFlowError):
            validate_config(value)

    def test_check_validation_agrees_before_start(self):
        base = read_json(BUILD_PLUGIN / "config" / "defaults.json")
        for check in (
            {"id": "UnitTests", "argv": [sys.executable]},
            {"id": "unit", "argv": [sys.executable], "cwd": "../outside"},
            {"id": "unit", "argv": [sys.executable], "cwd": "/outside"},
            {"id": "unit", "argv": [sys.executable], "cwd": "C:\\outside"},
        ):
            value = copy.deepcopy(base)
            value["checks"] = [check]
            with self.subTest(check=check), self.assertRaises(BuildFlowError):
                validate_config(value)

    def test_every_project_example_is_valid_for_both_independent_workflows(self):
        examples = sorted((ROOT / "examples").glob("devflow.*.json"))
        self.assertGreaterEqual(len(examples), 4)
        with tempfile.TemporaryDirectory(prefix="devflow examples ") as directory:
            project = Path(directory).resolve()
            for path in examples:
                with self.subTest(example=path.name):
                    write_json(project / "devflow.config.json", read_json(path))
                    plan = load_plan_config(project, PLAN_PLUGIN)
                    implement = load_build_config(project, BUILD_PLUGIN)
                    self.assertEqual(plan["max_reviews"], 6)
                    self.assertEqual(implement["max_reviews"], 6)
                    self.assertNotIn("checks", plan)
                    self.assertNotIn("grill_skill", implement)
            self.assertFalse((project / ".devflow").exists())


class FlowFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="devflow integration ")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.project = self.base / "project with spaces"
        self.project.mkdir()
        self.config = {"snapshot": {"mode": "files"}, "checks": [{
            "id": "unit", "argv": [sys.executable, "-c", "print('synthetic test passed')"],
            "timeout_seconds": 10, "required": True,
        }]}
        write_json(self.project / "devflow.config.json", {"schema_version": 1, "implement": self.config})
        self.plan = self.project / "feature.md"
        self.plan.write_text("# Feature\nImplement a pure function with a regression test. Keep the API compatible.\n", encoding="utf-8")
        self.source = self.project / "feature.py"
        self.source.write_text("VALUE = 1\n", encoding="utf-8")
        self.session = "integration-session-1"
        self.transcript = self.base / "claude-transcript.jsonl"
        self.engine = BuildEngine(self.project, BUILD_PLUGIN)
        self.plan_engine = PlanEngine(self.project, PLAN_PLUGIN)
        self.run_id = None
        self.draft = None
        self.preflight = patch("buildflow_core.adapters.preflight", return_value={"ready": True, "kind": "synthetic"})
        self.preflight.start()
        self.addCleanup(self.preflight.stop)

    def event(self, name="UserPromptSubmit", session=None, **extra):
        return {"hook_event_name": name, "cwd": str(self.project), "session_id": session or self.session,
                "transcript_path": str(self.transcript), **extra}

    def submit(self, prompt, session=None):
        return build_hook(self.event(session=session, prompt=prompt), BUILD_PLUGIN)

    def invoke(self, action, *options, session=None, run=True, expect=0):
        session = session or self.session
        arguments = ["--project", str(self.project), "--session", session]
        if run:
            self.assertIsNotNone(self.run_id)
            arguments += ["--run", self.run_id]
        context = arguments.copy()
        arguments += [action, *map(str, options)]
        command = shlex.join([
            "uv", "run", "--offline", "--no-project", "--no-config", "--no-python-downloads",
            str(PLUGIN / "scripts" / "devflow.py"), *context, "implement", action, *map(str, options),
        ])
        decision = build_hook(self.event("PreToolUse", session=session, tool_name="Bash", tool_input={"command": command}), BUILD_PLUGIN)
        self.assertFalse((decision.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny", decision)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = build_cli.main(arguments)
        self.assertEqual(result, expect, stderr.getvalue())
        return json.loads(stdout.getvalue()) if result == 0 else stderr.getvalue()

    def start(self, plan=None):
        self.plan = plan or self.plan
        self.submit("/devflow:implement " + str(self.plan))
        self.invoke("doctor", run=False)
        state = self.invoke("start", "--plan", self.plan, run=False)
        self.run_id = state["run_id"]
        self.draft = Path(state["draft_dir"])
        return state

    def edit(self, path, text, session=None):
        result = build_hook(self.event("PreToolUse", session=session, tool_name="Write", tool_input={"file_path": str(path)}), BUILD_PLUGIN)
        self.assertEqual(result, {})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def checkpoint(self, session=None):
        summary = self.draft / "implementation.md"
        self.edit(summary, "Implemented the selected plan and added regression coverage.\n", session=session)
        return self.invoke("checkpoint", "--summary-file", summary, session=session)

    def ready_review(self):
        self.start()
        self.edit(self.source, "VALUE = 2\n")
        self.checkpoint()
        return self.invoke("check")

    def plan_append(self, role, content):
        with self.transcript.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps({"type": role, "message": {"role": role, "content": content}}, ensure_ascii=False) + "\n")

    def plan_start(self):
        request = "/devflow:plan Make a tested pure function; do not implement."
        event = self.event(prompt=request)
        self.assertEqual(build_hook(event, BUILD_PLUGIN), {})
        plan_hook(event, PLAN_PLUGIN)
        self.plan_append("user", request)
        run_id = self.plan_engine.session_run(self.session)
        return run_id

    def plan_complete(self):
        run_id = self.plan_start()
        self.plan_append("assistant", "grill-me: Must the old API remain compatible?")
        answer = "Yes. Keep the API compatible and add a regression test."
        plan_hook(self.event(prompt=answer), PLAN_PLUGIN)
        self.plan_append("user", answer)
        draft = Path(self.plan_engine.status(run_id)["draft_dir"])
        decisions = draft / "decisions.json"
        write_json(decisions, {"summary": "Human confirmed API compatibility.", "decisions": ["Preserve the API"],
                               "constraints": ["Add a regression test"], "open_questions": []})
        self.plan_engine.seal(run_id, decisions)
        plan = draft / "plan.md"
        plan.write_text("# Feature\nImplement a pure function. Preserve its API. Add a regression test.\n", encoding="utf-8")
        self.plan_engine.plan(run_id, plan)
        reviewed = {"schema_version": 1, "verdict": "pass", "summary": "Synthetic planning review passed.", "findings": [], "questions": []}
        with patch("planflow_core.adapters.run_review", return_value=reviewed):
            self.plan_engine.review(run_id)
            state = self.plan_engine.review(run_id)
        self.assertEqual(state["status"], "completed")
        return run_id, Path(state["current_plan"])


class HookCliEngineTests(FlowFixture):
    def test_documented_command_loop_reaches_two_reviewer_completion(self):
        state = self.ready_review()
        self.assertEqual(state["status"], "ready_review")
        with patch("buildflow_core.adapters.run_review", side_effect=review_pass) as dispatch:
            first = self.invoke("review")
            second = self.invoke("review")
        self.assertEqual(first["status"], "ready_review")
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["review_count"], 2)
        self.assertTrue(second["report"]["both_reviewers_approved"])
        report = second["report"]
        self.assertEqual(report["workspace_snapshot_id_at_report"], second["candidate"]["snapshot_id"])
        self.assertTrue(report["workspace_matches_checkpoint_at_report"])
        self.assertTrue(report["final_workspace_verified"])
        self.assertTrue(report["final_snapshot_reviewed"])
        self.assertFalse(report["unreviewed_final_changes"])
        self.assertIsNone(report["workspace_capture_error"])
        self.assertEqual([call.args[0]["id"] for call in dispatch.call_args_list], ["codex", "opencode"])
        self.assertEqual(first["candidate"]["snapshot_id"], second["candidate"]["snapshot_id"])
        blocked = build_hook(self.event("PreToolUse", tool_name="Write", tool_input={"file_path": str(self.source)}), BUILD_PLUGIN)
        self.assertEqual(blocked["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_status_command_is_usable_in_its_documented_form(self):
        self.start()
        state = self.invoke("status")
        self.assertEqual(state["run_id"], self.run_id)
        self.assertEqual(state["review_count"], 0)

    def test_documented_reflect_command_revalidates_changed_code_before_next_provider(self):
        first_snapshot = self.ready_review()["candidate"]["snapshot_id"]
        packets = []
        providers = []

        def review_with_one_correction(provider, packet_path, *args):
            packets.append(read_json(Path(packet_path)))
            providers.append(provider["id"])
            review = review_pass(provider, packet_path, *args)
            if len(providers) == 1:
                review.update(verdict="changes_required", findings=[{
                    "id": "F1", "severity": "high", "category": "correctness", "file": "feature.py",
                    "line_start": 1, "line_end": 1, "evidence": "The synthetic value needs correction.",
                    "recommendation": "Correct the value and rerun the configured regression check.", "requires_human": False,
                }])
            return review

        with patch("buildflow_core.adapters.run_review", side_effect=review_with_one_correction):
            required = self.invoke("review")
            self.assertEqual(required["status"], "needs_revision")
            decisions = self.draft / "decisions.json"
            self.edit(decisions, json.dumps({"review_number": 1, "summary": "Accept the evidence-based correction.", "decisions": [
                {"finding_id": "F1", "action": "accept", "reason": "The proposed correction matches the frozen plan."},
            ]}))
            reflected = self.invoke("reflect", "--decisions-file", decisions)
            self.assertEqual(reflected["status"], "implementing")
            self.assertEqual(reflected["approvals"], {})
            self.assertIsNone(reflected["validation"])
            self.edit(self.source, "VALUE = 3\n")
            self.checkpoint()
            self.invoke("check")
            second = self.invoke("review")
            completed = self.invoke("review")
        self.assertEqual(providers, ["codex", "opencode", "codex"])
        self.assertNotEqual(first_snapshot, second["candidate"]["snapshot_id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["review_count"], 3)
        self.assertTrue(completed["report"]["both_reviewers_approved"])
        self.assertEqual(len(packets[1]["previous_reviews"]), 1)
        self.assertEqual(packets[1]["reflections"][0]["decisions"][0]["finding_id"], "F1")
        self.assertEqual(packets[1]["validation"]["snapshot_id"], packets[1]["snapshot_id"])

    def test_check_mutating_source_never_becomes_a_pass(self):
        self.config["checks"][0]["argv"] = [sys.executable, "-c", "from pathlib import Path; Path('feature.py').write_text('VALUE = 9\\n')"]
        write_json(self.project / "devflow.config.json", {"schema_version": 1, "implement": self.config})
        self.start()
        self.checkpoint()
        with patch("buildflow_core.adapters.run_review") as dispatch:
            result = self.invoke("check")
        self.assertEqual(result["status"], "paused_error")
        self.assertEqual(result["approvals"], {})
        self.assertEqual(result["review_count"], 0)
        self.assertIsNone(result["validation"])
        self.assertIn("changed since checkpoint", result["last_error"])
        dispatch.assert_not_called()

    def test_real_human_event_resolves_but_model_invented_event_does_not(self):
        self.start()
        questions = self.draft / "questions.json"
        self.edit(questions, json.dumps({"reason": "Existing API conflicts with requested change.", "questions": ["Keep compatibility?"]}))
        waiting = self.invoke("escalate", "--questions-file", questions)
        self.assertEqual(waiting["status"], "awaiting_human")
        self.invoke("resolve", "--event-id", "evt-" + "b" * 32, "--decision", "continue", "--reason", "Claimed approval", expect=2)
        actual = "Keep the existing API and continue with a compatible implementation."
        build_hook(self.event("PostToolUse", tool_name="AskUserQuestion", tool_use_id="human-resolution-1",
                             tool_input={"questions": [{"question": "Keep compatibility?"}]},
                             tool_response={"answers": {"Keep compatibility?": actual}}), BUILD_PLUGIN)
        after_answer = self.invoke("status")
        event = after_answer["human_events"][-1]
        resolved = self.invoke("resolve", "--event-id", event["event_id"], "--decision", "continue", "--reason", "Preserve the existing API")
        self.assertEqual(resolved["status"], "implementing")
        saved = read_json(Path(resolved["run_dir"]) / resolved["resolutions"][-1])
        self.assertEqual(json.loads(saved["human_event"]["text"])["answers"]["Keep compatibility?"], actual)

    def test_resume_command_cannot_be_reused_as_human_answer(self):
        self.start()
        questions = self.draft / "questions.json"
        self.edit(questions, json.dumps({"reason": "Need a decision.", "questions": ["May the API change?"]}))
        self.invoke("escalate", "--questions-file", questions)
        self.submit("/devflow:resume " + self.run_id)
        self.invoke("resume")
        record = read_json(session_path(self.project, self.session))
        event_id = record["events"][-1]["event_id"]
        self.invoke("resolve", "--event-id", event_id, "--decision", "continue", "--reason", "Resume is not an approval", expect=2)
        self.assertEqual(self.engine.status(self.run_id)["status"], "awaiting_human")

    def test_new_session_resume_clears_old_guard_without_resetting_count(self):
        self.ready_review()
        with patch("buildflow_core.adapters.run_review", side_effect=review_pass):
            self.invoke("review")
        new_session = "integration-session-2"
        self.submit("/devflow:resume " + self.run_id, session=new_session)
        resumed = self.invoke("resume", session=new_session)
        self.assertEqual(resumed["review_count"], 1)
        self.assertEqual(resumed["next_reviewer"], "opencode")
        old = read_json(session_path(self.project, self.session))
        self.assertIsNone(old["active_run"])
        self.assertIsNone(old["intent"])
        self.assertEqual(build_hook(self.event("PreToolUse", tool_name="Bash", tool_input={"command": "normal user work"}), BUILD_PLUGIN), {})
        with patch("buildflow_core.adapters.run_review", side_effect=review_pass):
            result = self.invoke("review", session=new_session)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["review_count"], 2)

    def test_cancel_before_checkpoint_reports_actual_unreviewed_workspace(self):
        self.start()
        self.edit(self.source, "VALUE = 12\n")
        self.submit("Cancel this BuildFlow run; keep my current files without declaring them reviewed.")
        cancelled = self.invoke("cancel", "--reason", "Human requested cancellation")
        report = cancelled["report"]
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(report["review_count"], 0)
        self.assertIsNone(report["snapshot_id"])
        self.assertTrue(report["workspace_snapshot_id_at_report"])
        self.assertFalse(report["workspace_matches_checkpoint_at_report"])
        self.assertFalse(report["final_workspace_verified"])
        self.assertFalse(report["final_snapshot_reviewed"])
        self.assertFalse(report["both_reviewers_approved"])
        self.assertTrue(report["unreviewed_final_changes"])
        self.assertEqual(self.source.read_text(encoding="utf-8"), "VALUE = 12\n")

    def test_terminal_report_is_frozen_when_next_real_prompt_releases_guard(self):
        self.ready_review()
        with patch("buildflow_core.adapters.run_review", side_effect=review_pass):
            self.invoke("review")
            completed = self.invoke("review")
        captured_report = copy.deepcopy(completed["report"])
        self.submit("The workflow is finished. I will now make an unrelated local change.")
        session = read_json(session_path(self.project, self.session))
        self.assertIsNone(session["active_run"])
        self.edit(self.source, "VALUE = 29\n")
        recorded = self.invoke("status")
        self.assertEqual(recorded["report"], captured_report)
        self.assertIn("Later edits are not covered", recorded["report"]["note"])

    def test_terminal_report_cannot_be_overwritten_or_forged(self):
        self.start()
        cancelled = self.invoke("cancel", "--reason", "No implementation requested")
        report_path = Path(cancelled["run_dir"]) / cancelled["report_path"]
        decision = build_hook(self.event("PreToolUse", tool_name="Write", tool_input={"file_path": str(report_path)}), BUILD_PLUGIN)
        self.assertEqual(decision["hookSpecificOutput"]["permissionDecision"], "deny")
        # External tampering is not a simulated model tool operation. A recorded
        # report must still fail its integrity check when read by the engine.
        forged = dict(cancelled["report"], both_reviewers_approved=True)
        write_json(report_path, forged)
        message = self.invoke("status", expect=2)
        self.assertIn("terminal report was modified", message)


class WorkflowIndependenceTests(FlowFixture):
    def test_neither_inactive_plugin_creates_runtime_state(self):
        payload = self.event("PreToolUse", tool_name="Bash", tool_input={"command": "normal project work"})
        self.assertEqual(build_hook(payload, BUILD_PLUGIN), {})
        self.assertIsNone(plan_hook(payload, PLAN_PLUGIN))
        self.assertFalse((self.project / ".devflow").exists())

    def test_planflow_start_does_not_activate_buildflow(self):
        self.plan_start()
        self.assertFalse((self.project / ".devflow" / "runs" / "implement").exists())
        payload = self.event("PreToolUse", tool_name="Bash", tool_input={"command": "something PlanFlow will inspect"})
        self.assertEqual(build_hook(payload, BUILD_PLUGIN), {})

    def test_buildflow_start_does_not_activate_planflow(self):
        self.start()
        payload = self.event("PreToolUse", tool_name="Write", tool_input={"file_path": str(self.source)})
        self.assertIsNone(plan_hook(payload, PLAN_PLUGIN))
        self.assertFalse((self.project / ".devflow" / "runs" / "plan").exists())

    def test_completed_planflow_next_prompt_starts_independent_buildflow(self):
        plan_run, plan_file = self.plan_complete()
        prompt = self.event(prompt="/devflow:implement " + str(plan_file))
        # The root dispatcher releases the terminal plan guard before passing
        # an explicitly requested implementation start to its own component.
        self.assertIsNone(plan_hook(prompt, PLAN_PLUGIN))
        self.assertIsNone(self.plan_engine.find_active(self.session))
        build_hook(prompt, BUILD_PLUGIN)
        self.invoke("doctor", run=False)
        state = self.invoke("start", "--plan", plan_file, run=False)
        self.run_id = state["run_id"]
        self.draft = Path(state["draft_dir"])
        self.assertEqual(state["status"], "implementing")
        self.assertEqual(state["plan_source"], str(plan_file))
        self.assertEqual(self.plan_engine.status(plan_run)["status"], "completed")
        payload = self.event("PreToolUse", tool_name="Write", tool_input={"file_path": str(self.source)})
        self.assertIsNone(plan_hook(payload, PLAN_PLUGIN))
        self.assertEqual(build_hook(payload, BUILD_PLUGIN), {})

    def test_completed_planflow_transition_also_works_with_reverse_hook_order(self):
        _, plan_file = self.plan_complete()
        prompt = self.event(prompt="/devflow:implement " + str(plan_file))
        build_hook(prompt, BUILD_PLUGIN)
        self.assertIsNone(plan_hook(prompt, PLAN_PLUGIN))
        state = self.invoke("start", "--plan", plan_file, run=False)
        self.assertEqual(state["status"], "implementing")

if __name__ == "__main__":
    unittest.main()
