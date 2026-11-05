"""Offline end-to-end engine invariants, with real snapshots, hooks and checks.

Only the paid reviewer entry points are mocked.  Disk writes in these tests are
temporary fixture setup, simulated external edits, or explicit crash fixtures.
Runs start the documented way: the ``/devflow:implement`` prompt hook records a
pending start, then the runner's ``implement start`` creates the run.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from devflow_core import router
from devflow_core.common import runs, sessions
from devflow_core.common.errors import DevFlowError
from devflow_core.common.paths import PLUGIN_ROOT
from devflow_core.common.storage import atomic_write_json, digest, json_bytes, read_json
from devflow_core.implement import checks as checks_module
from devflow_core.implement.engine import Engine
from devflow_core.implement.review import validate_review
from devflow_core.workflows import WORKFLOWS
from devflow_core.common.review import REVIEW_LIMIT_REASON
from tests.common.test_common import limit_question

ACTIVITY = {"tool_call_count": 2, "tool_calls": {"command_execution": 2}, "other_events": {}}
EVENT_ID = r"\A[a-f0-9]{32}\Z"


class InjectedStop(Exception):
    """A process stopped at a chosen moment (power loss, a killed command)."""


def in_thread(function):
    """Run ``function`` in another thread (a concurrent process stand-in); return or re-raise."""
    outcome = {}

    def target():
        try:
            outcome["value"] = function()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller
            outcome["error"] = exc

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(60)
    if thread.is_alive():
        raise AssertionError("Concurrent call did not finish; a lock is held across the external process")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


class EngineFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="devflow implement engine ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.project = self.base / "project with spaces"
        self.project.mkdir()
        self.plan = self.base / "independent plan.md"
        self.plan.write_text("# Feature\n\nImplement VALUE with regression coverage.\n", encoding="utf-8")
        self.source = self.project / "module.py"
        self.source.write_text("VALUE = 0\n", encoding="utf-8")
        self.config = {
            "snapshot": {"mode": "files"},
            "checks": [{"id": "verify", "argv": [sys.executable, "-c", "print('validation-ok')"], "timeout_seconds": 10, "required": True}],
        }
        self.write_config()
        self.engine = Engine(self.project)
        self.session = "claude-engine-session"
        self.run_id = None
        self.packets = []
        self.reviewers = []
        self.response_kind = "pass"
        self.alter = None  # Optional in-place change to a review before validation.
        self.questions = 0
        self.addCleanup(patch.stopall)
        self.preflight_patch = patch("devflow_core.implement.engine.preflight",
                                     return_value={"available": True, "offline_probe": True})
        self.preflight = self.preflight_patch.start()
        self.reviewer = patch("devflow_core.implement.engine.run_review", side_effect=self.respond).start()

    def write_config(self):
        atomic_write_json(self.project / "devflow.config.json", {"schema_version": 1, "implement": self.config})

    def hook(self, event, session=None, **fields):
        payload = {"hook_event_name": event, "cwd": str(self.project), "session_id": session or self.session, **fields}
        return router.handle_hook(payload, PLUGIN_ROOT)

    def prompt(self, text, session=None):
        """Submit a real prompt; return the event ID it was recorded as (None if not recorded)."""
        result = self.hook("UserPromptSubmit", session, prompt=text)
        context = (result or {}).get("hookSpecificOutput", {}).get("additionalContext", "")
        if '{"' not in context:
            return None
        fields, _ = json.JSONDecoder().raw_decode(context, context.index('{"'))
        return fields.get("recorded_event_id")

    def ask(self, session=None, **fields):
        """A human AskUserQuestion: the question as Claude sent it (PreToolUse, recorded as an
        observation without answers), then the answered result (PostToolUse); returns the latter's output."""
        asked = {key: value for key, value in fields["tool_input"].items() if key not in sessions.SCREEN_FILLED_QUESTION_FIELDS}
        self.hook("PreToolUse", session, tool_name="AskUserQuestion", tool_use_id=fields["tool_use_id"], tool_input=asked)
        return self.hook("PostToolUse", session, tool_name="AskUserQuestion", **fields)

    def answer(self, text="Yes, I independently verified this version.", session=None):
        self.questions += 1
        result = self.ask(session, tool_use_id=f"question-{self.questions}",
                          tool_input={"questions": [{"question": "Continue with this exact version?"}]},
                          tool_response={"answers": {"Continue with this exact version?": text}})
        return re.search(r"as event ([a-f0-9]{32})", result["hookSpecificOutput"]["additionalContext"]).group(1)

    def start(self, session=None):
        identity = session or self.session
        self.prompt(f'/devflow:implement "{self.plan}"', identity)
        status = router.runner_start(self.project, identity, "implement", str(self.plan))
        self.run_id = status["run_id"]
        self.run = Path(status["run_dir"])
        self.draft = Path(status["draft_dir"])
        return status

    def runs(self):
        root = self.project / ".devflow" / "runs" / "implement"
        return sorted(path.name for path in root.iterdir()) if root.exists() else []

    def state(self):
        return read_json(self.run / "state.json")

    def draft_text(self, name, text):
        path = self.draft / name
        path.write_text(text, encoding="utf-8")
        return path

    def checkpoint(self, text="Implemented VALUE; preserve existing project changes."):
        return self.engine.checkpoint(self.run_id, self.session, self.draft_text("summary.md", text))

    def ready(self, value=1):
        self.source.write_text(f"VALUE = {value}\n", encoding="utf-8")
        result = self.checkpoint()
        self.assertEqual(result["status"], "ready_check")
        return self.engine.check(self.run_id, self.session)

    def resume(self, session=None):
        """The only resume: the user's explicit /devflow:resume prompt, handled in the hook."""
        identity = session or self.session
        self.prompt(f"/devflow:resume {self.run_id}", identity)
        self.session = identity
        return self.engine.status(self.run_id)

    def respond(self, config, packet_path, work_dir, schema_path, instructions_path, validate, probe, *, cwd):
        packet = read_json(packet_path)
        self.packets.append(packet)
        self.reviewers.append(copy.deepcopy(config))
        self.assertNotEqual(Path(work_dir).resolve(), self.project)
        # The reviewer starts in the workspace and reads the code itself.
        self.assertEqual(Path(cwd), Path(packet["workspace"]["path"]))
        self.assertTrue(Path(schema_path).is_file())
        self.assertTrue(Path(instructions_path).is_file())
        self.assertIs(validate, validate_review)
        self.assertEqual(probe, {"available": True, "offline_probe": True})
        review = {"schema_version": 1, "snapshot_id": packet["snapshot_id"], "verdict": self.response_kind,
                  "summary": "Independent offline test review.", "findings": [], "questions": []}
        if self.response_kind in {"changes_required", "human_required"}:
            review["findings"] = [{"id": "F1", "severity": "high", "category": "correctness", "file": "module.py",
                "line_start": 1, "line_end": 1, "evidence": "VALUE needs a regression correction.",
                "recommendation": "Correct VALUE and validate the behavior.", "requires_human": self.response_kind == "human_required"}]
        if self.response_kind == "human_required":
            review["questions"] = ["Which intended VALUE is correct?"]
        if self.alter:
            self.alter(review)
        # The real adapter validates with the workflow's validator before returning.
        return {"review": validate(review), "activity": dict(ACTIVITY)}

    def review(self):
        return self.engine.review(self.run_id, self.session)

    def reflect(self, action="accept"):
        state = self.state()
        last = state["attempts"][-1]
        review = read_json(self.run / last["review_path"])
        value = {"review_number": last["number"], "summary": "Address the independent review with evidence.",
            "decisions": [{"finding_id": item["id"], "action": action, "reason": "Confirmed by reading the implementation and check evidence."} for item in review["findings"]]}
        path = self.draft / "decisions.json"
        atomic_write_json(path, value)
        return self.engine.reflect(self.run_id, self.session, path)

    def at_limit(self, decision, reason="The user chose this at the review limit question."):
        """Answer the pending review limit question on the question screen and resolve it."""
        evidence = self.answer("Continue with two more reviews." if decision == "continue" else "Stop here.")
        return self.engine.resolve(self.run_id, self.session, evidence, decision, reason)

    def fail_until_last(self):
        """Use five attempts on reviewer failures (each resumed), leaving one."""
        self.reviewer.side_effect = DevFlowError("Unsuccessful independent review")
        for _ in range(5):
            self.review()
            self.resume()
        self.reviewer.side_effect = self.respond

    def escalate(self):
        path = self.draft / "questions.json"
        atomic_write_json(path, {"reason": "A product decision is required.", "questions": ["Which VALUE should be implemented?"]})
        return self.engine.escalate(self.run_id, self.session, path)

    def tool(self, name="Write", **tool_input):
        return self.hook("PreToolUse", tool_name=name, tool_input=tool_input)

    def assertNotAccepted(self, text, reason, session=None):
        """Submit a /devflow: command DevFlow does not accept: it adds context naming the reason, never raises."""
        context = self.hook("UserPromptSubmit", session, prompt=text)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("did not accept", context)
        self.assertIn(reason, context)

    def assertFinalReportSaved(self, status):
        path = self.run / status["report_path"]
        self.assertTrue(path.is_file())
        self.assertEqual(read_json(path), status["report"])
        self.assertEqual(digest(path.read_bytes()), self.state()["artifacts"][status["report_path"]])


class EngineFlowTests(EngineFixture):
    def test_start_does_not_preflight_reviewers(self):
        # Reviewer CLIs are probed by doctor and before each review, never at start.
        status = self.start()
        self.assertEqual(status["status"], "implementing")
        self.preflight.assert_not_called()
        self.reviewer.assert_not_called()
        self.assertNotIn("capabilities", self.state())

    def test_start_plan_change_during_baseline_is_rejected_before_run(self):
        from devflow_core.implement import snapshot
        original_capture = snapshot.capture
        original = self.plan.read_bytes()
        def change_after_capture(project, config, copies=None):
            result = original_capture(project, config, copies)
            self.plan.write_bytes(original + b"Changed during baseline capture.\n")
            return result
        self.prompt(f'/devflow:implement "{self.plan}"')
        with patch("devflow_core.implement.snapshot.capture", side_effect=change_after_capture):
            with self.assertRaisesRegex(DevFlowError, "plan changed during startup"):
                router.runner_start(self.project, self.session, "implement", str(self.plan))
        self.assertEqual(self.runs(), [])
        self.reviewer.assert_not_called()

    def test_a_baseline_too_large_to_read_back_creates_no_run(self):
        from devflow_core.common import storage
        self.prompt(f'/devflow:implement "{self.plan}"')
        with patch.object(storage, "JSON_LIMIT", 64):
            with self.assertRaisesRegex(DevFlowError, r"Record baseline.json would be .* over the 64 MiB JSON limit; "
                                                      r"it was not saved\. The snapshot scope has 1 files; narrow "
                                                      r"snapshot.include or snapshot.exclude and start again"):
                router.runner_start(self.project, self.session, "implement", str(self.plan))
        self.assertEqual(self.runs(), [])  # No run, no creation or scratch directory left.
        self.reviewer.assert_not_called()

    def test_start_retains_exact_bom_crlf_plan_bytes_and_review_hash(self):
        raw = b"\xef\xbb\xbf" + "# Feature\r\n\r\n日本語の計画を保持。\r\n".encode("utf-8")
        self.plan.write_bytes(raw)
        self.start()
        self.assertEqual((self.run / "plan.md").read_bytes(), raw)
        self.assertEqual(self.state()["source_digest"], digest(raw))
        self.assertEqual(self.state()["artifacts"]["plan.md"], digest(raw))
        self.ready()
        self.review()
        self.assertEqual(self.packets[-1]["plan"].encode("utf-8"), raw)
        self.assertEqual(self.packets[-1]["plan_sha256"], digest(raw))

    def test_start_rejects_invalid_utf8_empty_bom_and_nul_plans(self):
        self.prompt(f'/devflow:implement "{self.plan}"')
        for raw in (b"\xff\xfe", b"", b"\xef\xbb\xbf \r\n", b"# Feature\x00"):
            with self.subTest(raw=raw):
                self.plan.write_bytes(raw)
                with self.assertRaises(DevFlowError):
                    router.runner_start(self.project, self.session, "implement", str(self.plan))
        self.assertEqual(self.runs(), [])
        self.preflight.assert_not_called()
        self.reviewer.assert_not_called()

    def test_standalone_markdown_plan_is_copied_without_plan_workflow_dependency(self):
        status = self.start()
        self.assertEqual(status["status"], "implementing")
        self.assertEqual((self.run / "plan.md").read_text(encoding="utf-8"), self.plan.read_text(encoding="utf-8"))
        self.assertEqual(status["plan_source"], str(self.plan))
        self.assertEqual(status["review_count"], 0)
        self.assertFalse((self.project / ".devflow" / "runs" / "plan").exists())
        self.preflight.assert_not_called()
        self.reviewer.assert_not_called()
        request = read_json(self.run / "request.json")
        self.assertEqual(request["intent"], {"action": "start", "args": str(self.plan), "workspace": None})
        self.assertIn(str(self.plan), request["event"]["payload"]["prompt"])
        self.assertEqual(request["event"]["event_id"], self.state()["event_ids"][0])

    def test_unified_run_and_session_paths_are_distinct_and_private(self):
        status = self.start()
        self.assertEqual(status["flow"], "implement")
        self.assertEqual(self.run, self.project / ".devflow" / "runs" / "implement" / self.run_id)
        record = sessions.session_path(self.project, self.session)
        self.assertEqual(record.parent, self.project / ".devflow" / "sessions")
        self.assertEqual(read_json(record)["run"], {"flow": "implement", "run_id": self.run_id})
        for legacy in ("build", "plan"):
            self.assertFalse((self.project / ("." + legacy + "flow")).exists())
        self.assertFalse((self.project / ".devflow" / "runs" / "plan").exists())
        baseline = read_json(self.run / "baseline.json")
        self.assertNotIn("devflow.config.json", baseline["files"])
        self.assertFalse(any(".devflow" in name for name in baseline["files"]))

    def test_unrelated_plan_history_cannot_contaminate_implementation_snapshot(self):
        self.start()
        self.ready()
        original = self.state()["candidate"]["snapshot_id"]
        plan_history = self.project / ".devflow" / "runs" / "plan" / "independent-history" / "interview.md"
        plan_history.parent.mkdir(parents=True)
        plan_history.write_text("Private planning context that must not be part of the code review.\n", encoding="utf-8")
        reviewed = self.review()
        self.assertEqual(reviewed["candidate"]["snapshot_id"], original)
        self.assertNotIn("Private planning context", str(self.packets))

    def test_start_requires_real_explicit_intent_and_exact_plan_path(self):
        with self.assertRaisesRegex(DevFlowError, "explicit /devflow:implement"):
            router.runner_start(self.project, self.session, "implement", str(self.plan))
        self.prompt(f'/devflow:implement "{self.plan}"')
        other = self.base / "different.md"
        other.write_text("Do something different.\n", encoding="utf-8")
        with self.assertRaisesRegex(DevFlowError, "does not match"):
            router.runner_start(self.project, self.session, "implement", str(other))
        with self.assertRaisesRegex(DevFlowError, "explicit /devflow:implement"):
            router.runner_start(self.project, "another-session", "implement", str(self.plan))
        self.assertEqual(self.runs(), [])
        # The same file spelled with a redundant "." component is the same request.
        started = router.runner_start(self.project, self.session, "implement",
                                      os.path.join(str(self.base), ".", self.plan.name))
        self.assertEqual(started["plan_source"], str(self.plan))
        self.reviewer.assert_not_called()

    def test_two_independent_passes_for_same_snapshot_complete(self):
        self.start()
        self.assertEqual(self.ready()["status"], "ready_review")
        first = self.review()
        self.assertEqual(first["status"], "ready_review")
        self.assertEqual(first["next_reviewer"], "opencode")
        final = self.review()
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["review_count"], 2)
        self.assertEqual([r["kind"] for r in self.reviewers], ["codex", "opencode"])
        self.assertEqual([r["effort"] for r in self.reviewers], ["xhigh", "high"])
        self.assertEqual(len(set(final["approvals"].values())), 1)
        self.assertTrue(final["report"]["all_reviewers_approved"])
        self.assertFalse(final["report"]["automatic_commit_push_deploy"])
        self.assertEqual(final["report"]["validation"]["status"], "passed")
        self.assertFinalReportSaved(final)
        self.assertIn(b"validation-ok", (self.run / "check-processes" / "001" / "verify.stdout.log").read_bytes())

    def test_packets_include_plan_full_changes_checks_and_review_history(self):
        context = self.project / "unchanged.txt"
        context.write_text("This stable API contract is context.\n", encoding="utf-8")
        self.config["snapshot"]["context_paths"] = ["unchanged.txt"]
        self.write_config()
        self.start()
        self.ready()
        self.review()
        self.review()
        first, second = self.packets
        self.assertEqual(first["kind"], "code_review")
        self.assertEqual(first["plan"], self.plan.read_bytes().decode("utf-8"))
        self.assertIn("Implemented VALUE", first["implementation_summary"])
        self.assertEqual(first["schema_version"], 2)
        changed = next(x for x in first["changes"] if x["path"] == "module.py")
        # Only where the change is: mode, size and hash, never the contents.
        self.assertEqual(changed["change"], "modified")
        self.assertEqual(set(changed["before"]), {"mode", "size", "sha256"})
        self.assertEqual(changed["after"]["size"], len(self.source.read_bytes()))
        self.assertNotIn("VALUE = 1", json.dumps(first["changes"]))
        diff = Path(first["diff_path"]).read_text(encoding="utf-8")
        self.assertEqual(Path(first["diff_path"]), self.run / "snapshots" / "001.diff")
        self.assertFalse((self.run / "reviews" / "01" / "changes.diff").exists())
        self.assertIn("diff --git a/module.py b/module.py\n", diff)
        self.assertIn("+VALUE = 1", diff)
        self.assertEqual(first["context_paths"], ["unchanged.txt"])
        self.assertNotIn("This stable API contract", json.dumps(first))
        self.assertEqual(first["workspace"]["path"], str(self.project.resolve()))
        self.assertEqual(first["plan_path"], str(self.run / "plan.md"))
        self.assertEqual(first["check_results"]["status"], "passed")
        result = first["check_results"]["results"][0]
        self.assertNotIn("stdout", result)
        self.assertTrue(Path(result["stdout_path"]).is_file())
        self.assertEqual(self.state()["attempts"][0]["activity"], ACTIVITY)
        # status shows each round's tool calls, as the final report does.
        rounds = self.engine.status(self.run_id)["reviews"]
        self.assertEqual([(r["number"], r["reviewer_id"], r["status"], r["verdict"], r["tool_calls"]) for r in rounds],
                         [(1, "codex", "succeeded", "pass", ACTIVITY["tool_calls"]),
                          (2, "opencode", "succeeded", "pass", ACTIVITY["tool_calls"])])
        self.assertEqual(first["previous_reviews"], [])
        self.assertEqual(second["previous_reviews"][0]["review"]["verdict"], "pass")
        self.assertEqual(second["snapshot_id"], first["snapshot_id"])

    def test_code_change_invalidates_prior_approval_and_requires_fresh_checks(self):
        self.start()
        self.ready()
        self.review()
        original = self.state()["candidate"]["snapshot_id"]
        self.source.write_text("VALUE = 2\n", encoding="utf-8")
        with self.assertRaisesRegex(DevFlowError, "Code changed"):
            self.review()
        self.assertEqual(len(self.state()["attempts"]), 1)
        refreshed = self.checkpoint()
        self.assertEqual(refreshed["approvals"], {})
        self.assertNotEqual(refreshed["candidate"]["snapshot_id"], original)
        with self.assertRaises(DevFlowError):
            self.review()
        self.engine.check(self.run_id, self.session)
        self.assertEqual(self.review()["status"], "ready_review")
        final = self.review()
        self.assertEqual(final["status"], "completed")
        self.assertEqual([r["kind"] for r in self.reviewers], ["codex", "opencode", "codex"])

    def test_review_is_gated_on_checkpoint_and_successful_checks(self):
        self.config["checks"][0]["argv"] = [sys.executable, "-c", "raise SystemExit(1)"]
        self.write_config()
        self.start()
        with self.assertRaises(DevFlowError):
            self.review()
        self.checkpoint()
        with self.assertRaises(DevFlowError):
            self.review()
        result = self.engine.check(self.run_id, self.session)
        self.assertEqual(result["status"], "implementing")
        self.assertEqual(result["validation"]["status"], "failed")
        with self.assertRaises(DevFlowError):
            self.review()
        self.reviewer.assert_not_called()
        self.assertEqual(len(self.state()["attempts"]), 0)

    def test_attempt_is_durably_reserved_before_dispatch(self):
        self.start()
        self.ready()

        def observe(*args, **kwargs):
            stored = self.state()
            self.assertEqual(stored["status"], "reviewing")
            self.assertEqual(len(stored["attempts"]), 1)
            attempt = stored["attempts"][0]
            self.assertEqual(attempt["status"], "dispatched")
            self.assertEqual(stored["inflight"]["kind"], "review")
            self.assertEqual(stored["inflight"]["number"], 1)
            self.assertEqual(stored["inflight"]["target"], attempt["snapshot_id"])
            packet = self.run / attempt["packet_path"]
            self.assertTrue(packet.is_file())
            self.assertEqual(digest(packet.read_bytes()), attempt["packet_sha256"])
            # No lock is held while the external reviewer runs.
            self.assertFalse((self.run / "state.lock").exists())
            return self.respond(*args, **kwargs)

        self.reviewer.side_effect = observe
        result = self.review()
        self.assertEqual(result["review_count"], 1)
        self.assertIsNone(result["inflight"])

    def test_preflight_failure_before_dispatch_does_not_consume_attempt(self):
        self.start()
        self.ready()
        self.preflight.side_effect = DevFlowError("Requested model CLI capability unavailable")
        with self.assertRaises(DevFlowError):
            self.review()
        self.assertEqual(len(self.state()["attempts"]), 0)
        self.assertEqual(self.state()["status"], "ready_review")
        self.assertIsNone(self.state()["inflight"])
        self.assertFalse((self.run / "reviews").exists())
        self.reviewer.assert_not_called()

    def test_missing_reviewer_cli_fails_preflight_without_consuming_attempt(self):
        missing = str(self.base / "no such reviewer cli")
        self.config["reviewers"] = [
            {"id": "codex", "kind": "codex", "command": [missing], "model": "m", "effort": "high", "timeout_seconds": 10},
            {"id": "opencode", "kind": "opencode", "command": [missing], "model": "m", "effort": "high", "timeout_seconds": 10},
        ]
        self.write_config()
        self.start()
        self.ready()
        self.preflight_patch.stop()  # The real local probe runs before any reservation.
        with self.assertRaisesRegex(DevFlowError, "not found"):
            self.review()
        state = self.state()
        self.assertEqual((state["status"], state["attempts"], state["inflight"]), ("ready_review", [], None))
        self.assertFalse((self.run / "reviews").exists())
        self.assertFalse((self.run / "review-processes").exists())
        self.reviewer.assert_not_called()

    def test_oversized_packet_fails_before_dispatch_without_truncation(self):
        self.config["snapshot"]["max_packet_bytes"] = 1024
        self.write_config()
        self.start()
        self.ready()
        with self.assertRaisesRegex(DevFlowError, "packet exceeds"):
            self.review()
        self.assertEqual(self.state()["status"], "ready_review")
        self.assertEqual(len(self.state()["attempts"]), 0)
        self.reviewer.assert_not_called()

    def test_failed_dispatches_count_and_alternate_without_resume_reset(self):
        self.start()
        self.ready()
        self.reviewer.side_effect = DevFlowError("Offline simulated provider failure")
        for number in range(1, 7):
            result = self.review()
            self.assertEqual(result["review_count"], number)
            # The last failure leaves no attempt to retry: the next review request asks the human.
            self.assertEqual(result["status"], "ready_review" if number == 6 else "paused_error")
            if number != 6:
                resumed = self.resume()
                self.assertEqual(resumed["review_count"], number)
                self.assertEqual(resumed["status"], "ready_review")
        self.assertEqual([call.args[0]["kind"] for call in self.reviewer.call_args_list], ["codex", "opencode"] * 3)
        self.assertEqual((result["remaining_reviews"], result["next_reviewer"]), (0, None))
        self.assertEqual(self.review()["status"], "awaiting_human")
        self.assertEqual(self.reviewer.call_count, 6)
        stopped = self.at_limit("stop")
        self.assertEqual(stopped["status"], "exhausted")
        self.assertFalse(stopped["report"]["all_reviewers_approved"])
        with self.assertRaises(DevFlowError):
            self.review()
        self.assertEqual(self.reviewer.call_count, 6)

    def test_unknown_outcome_requires_human_recovery_and_keeps_attempt(self):
        self.start()
        self.ready()
        self.reviewer.side_effect = KeyboardInterrupt("simulated caller interruption")
        with self.assertRaises(KeyboardInterrupt):
            self.review()
        self.assertEqual(self.state()["status"], "reviewing")
        self.assertIsNotNone(self.state()["inflight"])
        self.assertEqual(len(self.state()["attempts"]), 1)
        self.assertEqual([r["status"] for r in self.engine.status(self.run_id)["reviews"]], ["dispatched"])
        self.assertNotAccepted(f"/devflow:resume {self.run_id}", "may still be running")
        self.assertNotAccepted(f"/devflow:resume {self.run_id}", "may still be running", "new-claude-session")
        with self.assertRaisesRegex(DevFlowError, "confirm"):
            self.engine.recover(self.run_id)
        recovered = self.engine.recover(self.run_id, confirmed=True)
        self.assertEqual(recovered["status"], "paused_error")
        self.assertIsNone(recovered["inflight"])
        self.assertEqual(recovered["review_count"], 1)
        self.assertEqual(recovered["attempts"][0]["status"], "failed")
        self.assertEqual([r["status"] for r in recovered["reviews"]], ["failed"])
        self.reviewer.side_effect = self.respond
        self.resume("new-claude-session")
        self.assertEqual(self.review()["attempts"][-1]["reviewer_id"], "opencode")

    def test_wrong_snapshot_or_malformed_result_never_approves(self):
        self.start()
        self.ready()

        self.alter = lambda review: review.update(snapshot_id="different-snapshot")
        result = self.review()
        self.assertEqual(result["status"], "paused_error")
        self.assertEqual(result["approvals"], {})
        self.assertEqual(result["review_count"], 1)
        self.resume()

        self.alter = lambda review: review.update(unexpected="not allowed")
        result = self.review()
        self.assertEqual(result["status"], "paused_error")
        self.assertEqual(result["review_count"], 2)
        self.assertEqual(result["approvals"], {})

    def test_reviewer_failure_keeps_other_approvals_for_the_same_snapshot(self):
        self.start()
        self.ready()
        self.assertEqual(self.review()["status"], "ready_review")  # codex passes
        snapshot = self.state()["candidate"]["snapshot_id"]
        self.alter = lambda review: review.update(unexpected="not allowed")
        failed = self.review()  # opencode: a reply with an extra field
        self.assertEqual(failed["status"], "paused_error")
        self.assertEqual(failed["approvals"], {"codex": snapshot})
        self.resume()
        failed = self.review()  # codex fails too; its earlier pass still counts
        self.assertEqual(failed["status"], "paused_error")
        self.assertEqual(failed["approvals"], {"codex": snapshot})
        self.resume()
        self.alter = None
        final = self.review()  # opencode passes
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["review_count"], 4)
        self.assertEqual([r["id"] for r in self.reviewers], ["codex", "opencode", "codex", "opencode"])
        self.assertEqual([a["status"] for a in final["attempts"]], ["succeeded", "failed", "failed", "succeeded"])
        self.assertEqual(final["approvals"], {"codex": snapshot, "opencode": snapshot})

    def test_sixth_correction_is_checked_then_stopping_at_the_limit_leaves_it_unreviewed(self):
        self.start()
        self.ready()
        self.response_kind = "changes_required"
        for number in range(1, 7):
            result = self.review()
            self.assertEqual(result["status"], "needs_revision")
            self.assertEqual(result["review_count"], number)
            self.assertEqual(self.reflect()["status"], "implementing")
            result = self.ready(number + 1)
            self.assertEqual(result["status"], "ready_review")
        self.assertEqual(self.source.read_text(encoding="utf-8"), "VALUE = 7\n")
        self.assertEqual(self.review()["status"], "awaiting_human")
        self.assertEqual(self.reviewer.call_count, 6)
        result = self.at_limit("stop")
        self.assertEqual(result["status"], "exhausted")
        self.assertEqual(result["report"]["review_limit"], 6)
        self.assertEqual(result["review_count"], 6)
        self.assertEqual(result["remaining_reviews"], 0)
        self.assertEqual(result["validation"]["status"], "passed")
        self.assertTrue(result["report"]["unreviewed_final_changes"])
        self.assertFalse(result["report"]["all_reviewers_approved"])
        self.assertEqual([r["kind"] for r in self.reviewers], ["codex", "opencode"] * 3)
        self.assertEqual(len(result["reflections"]), 6)
        self.assertFinalReportSaved(result)
        with self.assertRaises(DevFlowError):
            self.review()

    def test_single_pass_at_sixth_attempt_is_not_completion(self):
        self.start()
        self.ready()
        self.fail_until_last()
        result = self.review()
        self.assertEqual(result["status"], "ready_review")
        self.assertEqual(set(result["approvals"]), {"opencode"})
        self.assertEqual(self.review()["status"], "awaiting_human")
        result = self.at_limit("stop")
        self.assertEqual(result["status"], "exhausted")
        self.assertFalse(result["report"]["all_reviewers_approved"])

    def test_the_limit_question_keeps_approvals_and_continue_dispatches_the_seventh_review(self):
        self.start()
        self.ready()
        self.fail_until_last()
        passed = self.review()  # the sixth review: opencode approves
        snapshot = passed["candidate"]["snapshot_id"]
        self.assertEqual(passed["approvals"], {"opencode": snapshot})
        self.preflight.reset_mock()
        asked = self.review()
        # No reviewer starts and no CLI is probed at the limit.
        self.preflight.assert_not_called()
        self.assertEqual(self.reviewer.call_count, 6)
        self.assertEqual(asked["status"], "awaiting_human")
        self.assertEqual(asked["escalation"]["detail"]["kind"], "review_budget")
        self.assertEqual((asked["escalation"]["reason"], asked["escalation"]["questions"]),
                         (REVIEW_LIMIT_REASON, [limit_question(snapshot)]))
        self.assertEqual(read_json(self.run / asked["escalations"][-1])["detail"]["kind"], "review_budget")
        self.assertEqual(asked["approvals"], {"opencode": snapshot})
        # The resume command is never the answer.
        self.resume()
        self.assertEqual(self.engine.status(self.run_id)["human_events"], [])
        evidence = self.answer("Continue with two more reviews.")
        with self.assertRaisesRegex(DevFlowError, "continue/stop/accept"):
            self.engine.resolve(self.run_id, self.session, evidence, "cancel", "Cancel is not this question's answer.")
        continued = self.engine.resolve(self.run_id, self.session, evidence, "continue", "The user chose two more reviews.")
        self.assertEqual((continued["status"], continued["review_limit"], continued["remaining_reviews"],
                          continued["next_reviewer"]), ("ready_review", 8, 2, "codex"))
        self.assertEqual(continued["approvals"], {"opencode": snapshot})
        self.assertEqual(continued["config"]["max_reviews"], 6)
        self.assertEqual(read_json(self.run / continued["resolutions"][-1])["decision"], "continue")
        final = self.review()  # the seventh review: codex approves the same snapshot
        self.assertEqual(self.reviewers[-1]["id"], "codex")
        self.assertEqual(self.reviewer.call_count, 7)
        self.assertEqual((final["status"], final["review_count"]), ("completed", 7))
        self.assertEqual(final["report"]["review_limit"], 8)

    def test_stop_answers_only_the_limit_question(self):
        self.start()
        self.escalate()
        evidence = self.answer("Implement VALUE = 1.")
        with self.assertRaisesRegex(DevFlowError, "continue/cancel"):
            self.engine.resolve(self.run_id, self.session, evidence, "stop", "Stop is only for the review limit.")
        self.assertEqual(self.engine.resolve(self.run_id, self.session, evidence, "continue", "Use 1.")["status"],
                         "implementing")

    def test_a_code_change_during_the_last_review_is_checked_before_the_question(self):
        self.config["max_reviews"] = 2
        self.write_config()
        self.start()
        self.ready()
        self.assertEqual(self.review()["status"], "ready_review")  # codex approves

        def edit_during_review(*args, **kwargs):
            self.source.write_text("VALUE = 2\n", encoding="utf-8")
            return self.respond(*args, **kwargs)

        self.reviewer.side_effect = edit_during_review
        failed = self.review()
        self.assertEqual((failed["status"], failed["review_count"]), ("ready_review", 2))
        self.assertIn("changed since checkpoint", failed["last_error"])
        self.reviewer.side_effect = self.respond
        with self.assertRaisesRegex(DevFlowError, "changed since checkpoint"):
            self.review()
        self.assertIsNone(self.state()["escalation"])
        self.assertEqual(self.reviewer.call_count, 2)
        self.assertEqual(self.checkpoint()["approvals"], {})
        self.assertEqual(self.engine.check(self.run_id, self.session)["status"], "ready_review")
        asked = self.review()
        self.assertEqual(asked["status"], "awaiting_human")
        self.assertIn("2 of 2 reviews used. Reviewers approving the current version: none.", asked["escalation"]["questions"][0])

    def test_manual_validation_at_the_limit_leads_to_the_question(self):
        self.config.update(checks=[], max_reviews=2)
        self.write_config()
        self.start()
        self.response_kind = "changes_required"
        for value in (1, 2):
            self.assertEqual(self.ready(value)["status"], "awaiting_human")
            evidence = self.answer(f"I validated VALUE = {value}.")
            self.engine.resolve(self.run_id, self.session, evidence, "continue", "Manually validated.")
            self.assertEqual(self.review()["status"], "needs_revision")
            self.reflect()
        self.assertEqual(self.ready(3)["status"], "awaiting_human")
        evidence = self.answer("I validated VALUE = 3.")
        validated = self.engine.resolve(self.run_id, self.session, evidence, "continue", "Manually validated.")
        self.assertEqual((validated["status"], validated["review_count"]), ("ready_review", 2))
        self.assertEqual(self.review()["escalation"]["detail"]["kind"], "review_budget")

    def test_reflection_requires_one_explicit_disposition_per_finding(self):
        self.start()
        self.ready()
        self.response_kind = "changes_required"
        self.review()
        path = self.draft / "invalid-decisions.json"
        atomic_write_json(path, {"review_number": 1, "summary": "Missing evidence", "decisions": []})
        with self.assertRaisesRegex(DevFlowError, "Missing finding"):
            self.engine.reflect(self.run_id, self.session, path)
        self.assertEqual(self.state()["status"], "needs_revision")
        self.assertEqual(self.state()["reflections"], [])
        result = self.reflect("reject")
        self.assertEqual(result["status"], "implementing")
        self.assertEqual(result["approvals"], {})


class HumanEvidenceTests(EngineFixture):
    def test_missing_automated_checks_require_real_manual_evidence(self):
        self.config["checks"] = []
        self.write_config()
        self.start()
        original_event = self.state()["event_ids"][0]
        result = self.ready()
        self.assertEqual(result["status"], "awaiting_human")
        self.assertEqual(result["validation"]["status"], "not_configured")
        self.assertEqual(result["human_events"], [])
        with self.assertRaises(DevFlowError):
            self.review()
        with self.assertRaisesRegex(DevFlowError, "AFTER"):
            self.engine.resolve(self.run_id, self.session, original_event, "continue", "Old start event is not evidence.")
        evidence = self.answer("I ran the documented manual cases on this exact version; VALUE is correct.")
        self.assertEqual([item["event_id"] for item in self.engine.status(self.run_id)["human_events"]], [evidence])
        result = self.engine.resolve(self.run_id, self.session, evidence, "continue", "The user manually validated this exact snapshot.")
        self.assertEqual(result["status"], "ready_review")
        self.assertEqual(result["validation"]["status"], "human_attested")
        self.assertEqual(result["validation"]["kind"], "manual")
        self.review()
        self.assertEqual(self.packets[-1]["validation"]["kind"], "manual")
        self.assertEqual(self.packets[-1]["human_resolutions"][-1]["human_event"]["event_id"], evidence)
        self.assertEqual(self.review()["status"], "completed")

    def test_binary_changes_require_specific_human_attestation(self):
        self.start()
        (self.project / "asset.bin").write_bytes(b"\x00binary\xffcontent")
        result = self.checkpoint()
        self.assertEqual(result["status"], "awaiting_human")
        self.assertEqual(result["escalation"]["detail"]["kind"], "binary_changes")
        self.assertEqual(result["escalation"]["detail"]["snapshot_id"], result["candidate"]["snapshot_id"])
        with self.assertRaises(DevFlowError):
            self.engine.check(self.run_id, self.session)
        evidence = self.answer("I inspected asset.bin with its native viewer and approve these bytes.")
        result = self.engine.resolve(self.run_id, self.session, evidence, "continue", "The actual binary was inspected by the user.")
        self.assertEqual(result["status"], "ready_check")
        self.assertEqual(result["binary_attestation"]["snapshot_id"], result["candidate"]["snapshot_id"])
        self.engine.check(self.run_id, self.session)
        self.review()
        self.assertEqual([x["path"] for x in self.packets[-1]["changes"] if x["binary"]], ["asset.bin"])
        self.assertIn("Binary files", Path(self.packets[-1]["diff_path"]).read_text(encoding="utf-8"))
        self.assertIsNotNone(self.packets[-1]["binary_attestation"])

    def test_changed_snapshot_cannot_reuse_manual_answer(self):
        self.config["checks"] = []
        self.write_config()
        self.start()
        self.ready()
        evidence = self.answer()
        self.source.write_text("VALUE = 999\n", encoding="utf-8")
        with self.assertRaisesRegex(DevFlowError, "Code changed"):
            self.engine.resolve(self.run_id, self.session, evidence, "continue", "Approved earlier version only.")
        self.assertEqual(self.state()["status"], "awaiting_human")
        self.assertEqual(self.state()["resolutions"], [])
        self.source.write_text("VALUE = 1\n", encoding="utf-8")
        self.assertEqual(self.engine.resolve(self.run_id, self.session, evidence, "continue", "The restored exact version was manually validated.")["status"], "ready_review")

    def test_only_post_escalation_human_events_can_resolve(self):
        self.start()
        old_event = self.prompt("Prefer a small implementation.")
        self.assertRegex(old_event, EVENT_ID)
        self.escalate()
        for event in (old_event, "evt-" + "f" * 32, "f" * 32):
            with self.subTest(event=event), self.assertRaises(DevFlowError):
                self.engine.resolve(self.run_id, self.session, event, "continue", "Not a new actual answer.")
        command = self.prompt(f"/devflow:resume {self.run_id}")
        self.assertRegex(command, EVENT_ID)
        with self.assertRaisesRegex(DevFlowError, "resume command are not answers"):
            self.engine.resolve(self.run_id, self.session, command, "continue", "A command is not approval.")
        ordinary = self.prompt("Use VALUE = 1; continue with the existing scope.")
        with self.assertRaisesRegex(DevFlowError, "AskUserQuestion"):
            self.engine.resolve(self.run_id, self.session, ordinary, "continue", "An ordinary prompt is not approval evidence.")
        self.assertEqual(self.engine.status(self.run_id)["human_events"], [])
        event = self.answer("Use VALUE = 1; continue with the existing scope.")
        result = self.engine.resolve(self.run_id, self.session, event, "continue", "The user selected VALUE = 1.")
        self.assertEqual(result["status"], "implementing")
        self.assertIsNone(result["escalation"])
        saved = read_json(self.run / result["resolutions"][0])
        self.assertEqual(saved["human_event"]["event_id"], event)
        self.assertEqual(saved["human_event"]["kind"], "question")
        self.assertIn("Use VALUE = 1; continue with the existing scope.", saved["human_event"]["answer_text"])

    def test_string_answer_text_is_the_exact_human_text(self):
        self.start()
        self.escalate()
        self.ask(tool_use_id="string-answer", tool_input={"questions": [{"question": "Which VALUE?"}]},
                 tool_response="Use VALUE = 3, please.")
        self.assertEqual([item["answer_text"] for item in self.engine.status(self.run_id)["human_events"]],
                         ["Use VALUE = 3, please."])

    def test_resume_rebinds_owner_and_establishes_new_human_boundary(self):
        self.start()
        self.escalate()
        old_event = self.answer("Old-session answer before handoff.")
        self.assertEqual([item["event_id"] for item in self.engine.status(self.run_id)["human_events"]], [old_event])
        old_session = self.session
        result = self.resume("new-human-session")
        self.assertEqual(result["status"], "awaiting_human")
        self.assertEqual(result["owner_session"], self.session)
        self.assertEqual(result["escalation"]["session_id"], self.session)
        self.assertEqual(result["human_events"], [])
        stored = self.state()
        self.assertEqual(result["escalation"]["boundary"], len(stored["event_ids"]))
        # The old session keeps only a stale pointer; its audit trail stays in the run.
        stale = router.status(self.project, old_session, None)
        self.assertEqual((stale["status"], stale["owner_matches"]), ("stale", False))
        self.assertIn(old_event, stored["event_ids"])
        self.assertTrue((self.run / "events" / (old_event + ".json")).is_file())
        with self.assertRaises(DevFlowError):
            self.engine.resolve(self.run_id, old_session, old_event, "continue", "Old owner cannot resolve.")
        with self.assertRaises(DevFlowError):
            self.engine.resolve(self.run_id, self.session, old_event, "continue", "Old event cannot transfer.")
        resume_event = stored["event_ids"][-1]
        self.assertEqual(read_json(self.run / "events" / (resume_event + ".json"))["payload"]["prompt"],
                         f"/devflow:resume {self.run_id}")
        with self.assertRaises(DevFlowError):
            self.engine.resolve(self.run_id, self.session, resume_event, "continue", "Resume is not an answer.")
        # A stale session's question is never recorded: neither the question
        # (PreToolUse) nor its answer reaches the run (no event, no hook context).
        events_before = read_json(self.run / "state.json")["event_ids"]
        self.assertIsNone(self.ask(old_session, tool_use_id="stale-1",
                                   tool_input={"questions": [{"question": "Continue with this exact version?"}]},
                                   tool_response={"answers": {"Continue with this exact version?": "Yes"}}))
        self.assertEqual(read_json(self.run / "state.json")["event_ids"], events_before)
        event = self.answer("New owner confirms VALUE = 1 after seeing the question.")
        self.assertEqual([item["event_id"] for item in self.engine.status(self.run_id)["human_events"]], [event])
        result = self.engine.resolve(self.run_id, self.session, event, "continue", "New-session explicit answer.")
        self.assertEqual(result["status"], "implementing")
        self.assertEqual(result["review_count"], 0)

    def test_human_required_review_pauses_before_reflection(self):
        self.start()
        self.ready()
        self.response_kind = "human_required"
        result = self.review()
        self.assertEqual(result["status"], "awaiting_human")
        self.assertEqual(result["review_count"], 1)
        self.assertEqual(result["escalation"]["questions"], ["Which intended VALUE is correct?"])
        with self.assertRaises(DevFlowError):
            self.reflect()
        event = self.answer("VALUE = 2 is required; adjust accordingly.")
        result = self.engine.resolve(self.run_id, self.session, event, "continue", "The user clarified the intended value.")
        self.assertEqual(result["status"], "needs_revision")
        self.assertEqual(self.reflect()["status"], "implementing")
        self.assertEqual(self.ready(2)["status"], "ready_review")
        self.response_kind = "pass"
        self.review()
        packet = self.packets[-1]
        self.assertEqual(len(packet["reflections"]), 1)
        self.assertEqual(len(packet["human_resolutions"]), 1)

    def test_critical_finding_escalates_even_without_model_human_flag(self):
        self.start()
        self.ready()
        self.response_kind = "changes_required"

        self.alter = lambda review: review["findings"][0].update(severity="critical")
        result = self.review()
        self.assertEqual(result["status"], "awaiting_human")
        self.assertEqual(result["escalation"]["detail"]["kind"], "review_decision")

    def test_cancel_is_terminal_without_faking_validation_or_review(self):
        self.start()
        self.escalate()
        event = self.answer("Cancel this implementation.")
        result = self.engine.resolve(self.run_id, self.session, event, "cancel", "The user declined the change.")
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["review_count"], 0)
        self.assertIsNone(result["validation"])
        self.assertFalse(result["report"]["all_reviewers_approved"])
        self.assertFinalReportSaved(result)

    def test_the_final_report_lists_only_questions_the_model_prefilled(self):
        self.start()
        self.answer()  # An ordinary question: recorded at PreToolUse without answers.
        prefilled = {"questions": [{"question": "Ship it?"}], "answers": {"Ship it?": "Yes"}}
        self.hook("PreToolUse", None, tool_name="AskUserQuestion", tool_use_id="prefilled-1", tool_input=prefilled)
        observed = [json.loads((self.run / "events" / f"{event_id}.json").read_text(encoding="utf-8"))
                    for event_id in self.state()["event_ids"]]
        events = {event["payload"].get("tool_use_id"): event["event_id"] for event in observed
                  if event["kind"] == "observation"}
        # A subagent's prefilled question is not the interview's.
        self.hook("PreToolUse", None, tool_name="AskUserQuestion", tool_use_id="subagent-1", agent_id="agent-1",
                  tool_input=prefilled)
        self.assertEqual(self.engine.status(self.run_id)["prefilled_questions"], 1)
        result = self.engine.cancel(self.run_id, self.session, "Stop here.")
        self.assertEqual(result["report"]["prefilled_question_events"], [events["prefilled-1"]])
        self.assertNotIn(events["question-1"], result["report"]["prefilled_question_events"])

    def test_cancel_is_saved_even_if_an_event_file_is_damaged(self):
        # Review finding: the final report lists prefilled questions; a damaged
        # event file is reported there instead of blocking the cancellation.
        self.start()
        event_id = self.state()["event_ids"][0]
        (self.run / "events" / f"{event_id}.json").write_text("{}", encoding="utf-8")
        result = self.engine.cancel(self.run_id, self.session, "Stop here.")
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.state()["status"], "cancelled")
        self.assertIsNone(result["report"]["prefilled_question_events"])
        self.assertIn("Invalid human event record", result["report"]["prefilled_question_events_error"])
        self.assertFinalReportSaved(result)
        # Status is display only: it reports the unreadable events instead of failing.
        self.assertEqual(result["human_events"], [])
        self.assertIsNone(result["prefilled_questions"])
        self.assertIn("Invalid human event record", result["events_error"])
        self.assertIsNone(self.engine.status(self.run_id)["prefilled_questions"])


class RecoveryAndIntegrityTests(EngineFixture):
    def test_mutating_check_failure_can_resume_with_fresh_process_logs(self):
        marker = self.base / "check-ran-once"
        script = ("from pathlib import Path\n"
            f"marker = Path({str(marker)!r})\n"
            "if not marker.exists():\n"
            "    Path('module.py').write_text('VALUE = 2\\n', encoding='utf-8')\n"
            "    marker.write_text('done', encoding='utf-8')\n"
            "print('validated current files')\n")
        self.config["checks"][0]["argv"] = [sys.executable, "-c", script]
        self.write_config()
        self.start()
        first = self.ready()
        self.assertEqual(first["status"], "paused_error")
        self.assertIsNone(first["inflight"])
        self.assertIn("Code changed", first["last_error"])
        self.assertEqual(first["review_count"], 0)
        self.assertEqual(self.resume()["status"], "implementing")
        self.checkpoint("Inspected the one-time generated correction before retrying checks.")
        second = self.engine.check(self.run_id, self.session)
        self.assertEqual(second["status"], "ready_review", second.get("last_error"))
        logs = list((self.run / "check-processes").glob("*/verify.stdout.log"))
        self.assertEqual(len(logs), 2, "Every launched check must retain a distinct diagnostic directory.")

    def test_check_budget_requires_human_authorization_not_automatic_retry(self):
        self.config["checks"][0]["argv"] = [sys.executable, "-c", "from pathlib import Path; raise SystemExit(0 if 'VALUE = 1' in Path('module.py').read_text() else 1)"]
        self.write_config()
        self.start()
        for number in range(1, 4):
            self.checkpoint()
            result = self.engine.check(self.run_id, self.session)
            self.assertEqual(result["check_attempts"], number)
        self.assertEqual(result["status"], "awaiting_human")
        self.assertEqual(result["escalation"]["detail"]["kind"], "check_budget")
        self.reviewer.assert_not_called()
        event = self.answer("I inspected the test failure; correct VALUE and allow another bounded check cycle.")
        resolved = self.engine.resolve(self.run_id, self.session, event, "continue", "The user authorized further check attempts.")
        self.assertEqual(resolved["check_attempts"], 0)
        self.assertEqual(resolved["review_count"], 0)
        self.assertEqual(self.ready()["status"], "ready_review")

    def test_resume_cannot_replace_a_different_active_project_run(self):
        first = self.start()
        first_id = first["run_id"]
        self.engine.cancel(first_id, self.session, "Finished this attempt; retain audit records.")
        second = self.start()
        self.assertNotEqual(second["run_id"], first_id)
        other_session = "separate-session"
        self.assertNotAccepted(f"/devflow:resume {first_id}", "unfinished", other_session)
        self.assertFalse(sessions.session_path(self.project, other_session).exists())
        self.assertEqual(read_json(sessions.session_path(self.project, self.session))["run"],
                         {"flow": "implement", "run_id": second["run_id"]})
        self.assertEqual(self.engine.status(first_id)["owner_session"], self.session)
        self.assertEqual(self.state()["status"], "implementing")
        self.assertEqual(self.state()["owner_session"], self.session)

    def test_unbound_live_run_is_not_orphaned_and_blocks_another_start(self):
        self.start()
        original_id = self.run_id
        # Simulate losing the session's pointer to its run (for example a
        # session record moved away by a human); the run itself remains.
        sessions.session_path(self.project, self.session).unlink()
        identity = "another-start-session"
        self.assertNotAccepted(f'/devflow:implement "{self.plan}"', "unfinished", identity)
        with self.assertRaises(DevFlowError):
            router.runner_start(self.project, identity, "implement", str(self.plan))
        self.assertEqual(self.runs(), [original_id])
        self.assertEqual(self.state()["status"], "implementing")
        self.assertEqual(self.resume()["owner_session"], self.session)

    def test_terminal_report_is_immutable_across_explicit_resume(self):
        self.start()
        self.ready()
        self.review()
        completed = self.review()
        self.assertFinalReportSaved(completed)
        original_path = self.run / completed["report_path"]
        original_bytes = original_path.read_bytes()
        resumed = self.resume("terminal-report-session")
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(resumed["review_count"], 2)
        self.assertFinalReportSaved(resumed)
        self.assertNotEqual(resumed["report_path"], completed["report_path"])
        self.assertEqual(original_path.read_bytes(), original_bytes)
        self.assertEqual(self.reviewer.call_count, 2)

    def test_terminal_report_tampering_is_detected_when_the_report_is_read(self):
        self.start()
        self.ready()
        self.review()
        completed = self.review()
        path = self.run / completed["report_path"]
        path.write_bytes(path.read_bytes() + b"tampered")
        # Resume only checks its listed preconditions; reading the report verifies it.
        with self.assertRaisesRegex(DevFlowError, "terminal report was modified"):
            self.engine.status(self.run_id)
        self.assertEqual(self.reviewer.call_count, 2)

    def test_only_optional_checks_cannot_satisfy_validation_gate(self):
        self.config["checks"][0]["required"] = False
        self.write_config()
        self.prompt(f'/devflow:implement "{self.plan}"')
        with self.assertRaises(DevFlowError):
            router.runner_start(self.project, self.session, "implement", str(self.plan))
        self.assertEqual(self.runs(), [])
        self.reviewer.assert_not_called()

    def test_immutable_artifact_tampering_is_detected_before_further_work(self):
        self.start()
        for relative in ("plan.md", "baseline.json", "config.json", "request.json"):
            path = self.run / relative
            original = path.read_bytes()
            try:
                path.write_bytes(original + b"\nTAMPERED\n")
                with self.subTest(artifact=relative), self.assertRaisesRegex(DevFlowError, "Immutable artifact"):
                    self.checkpoint()
            finally:
                path.write_bytes(original)
        self.assertEqual(self.state()["checkpoint_count"], 0)
        self.reviewer.assert_not_called()

    def test_original_plan_change_or_deletion_cannot_be_silently_adopted(self):
        self.start()
        original = self.plan.read_bytes()
        self.plan.write_text("A different request.\n", encoding="utf-8")
        with self.assertRaisesRegex(DevFlowError, "Original plan"):
            self.checkpoint()
        self.plan.unlink()
        with self.assertRaisesRegex(DevFlowError, "Original plan"):
            self.checkpoint()
        self.plan.write_bytes(original)
        self.assertEqual(self.checkpoint()["status"], "ready_check")

    def test_a_review_limit_edit_outside_whole_rounds_is_corrupt(self):
        self.start()
        for limit in (7, 5, 100, "8"):
            with self.subTest(limit=limit):
                stored = self.state()
                stored["review_limit"] = limit
                atomic_write_json(self.run / "state.json", stored)
                with self.assertRaisesRegex(DevFlowError, "never reset"):
                    self.engine.status(self.run_id)
                stored["review_limit"] = 6
                atomic_write_json(self.run / "state.json", stored)

    def test_state_configuration_drift_cannot_expand_budget(self):
        self.start()
        stored = self.state()
        stored["config"]["max_reviews"] = 100
        atomic_write_json(self.run / "state.json", stored)
        with self.assertRaises(DevFlowError):
            self.checkpoint()
        self.reviewer.assert_not_called()

    def test_more_attempts_than_the_review_limit_is_corrupt_not_reset(self):
        self.start()
        stored = self.state()
        stored["attempts"] = [{} for _ in range(7)]
        atomic_write_json(self.run / "state.json", stored)
        with self.assertRaisesRegex(DevFlowError, "never reset"):
            self.engine.status(self.run_id)
        self.assertEqual(len(read_json(self.run / "state.json")["attempts"]), 7)

    def stopped_commit(self):
        """Stop every save just before ``state.json`` is replaced (its records are already written)."""
        original = runs.atomic_json

        def stop(path, value):
            if Path(path).name == "state.json" and Path(path).parent == self.run:
                raise InjectedStop("stopped before the commit")
            return original(path, value)
        return patch.object(runs, "atomic_json", side_effect=stop)

    def test_a_checkpoint_stopped_before_the_commit_is_simply_run_again(self):
        self.start()
        self.source.write_text("VALUE = 1\n", encoding="utf-8")
        with self.stopped_commit(), self.assertRaises(InjectedStop):
            self.checkpoint()
        stopped = self.state()
        self.assertEqual((stopped["status"], stopped["checkpoint_count"]), ("implementing", 0))
        leftovers = sorted(path.name for path in (self.run / "snapshots").iterdir())
        self.assertTrue(leftovers)
        self.assertFalse(any(name.startswith("snapshots/") for name in stopped["artifacts"]))
        # The command is run again; the leftovers are replaced and committed once.
        result = self.checkpoint("Implemented VALUE after the stopped save.")
        self.assertEqual((result["status"], result["checkpoint_count"]), ("ready_check", 1))
        self.assertEqual(sorted(path.name for path in (self.run / "snapshots").iterdir()), leftovers)
        self.assertIn("after the stopped save", (self.run / result["candidate"]["summary"]).read_text(encoding="utf-8"))
        self.engine.store.verify_artifacts(self.state())

    def test_a_changed_committed_record_stops_the_run_and_is_never_rewritten(self):
        self.start()
        plan = self.run / "plan.md"
        plan.write_bytes(b"A different plan.\n")
        with self.assertRaisesRegex(DevFlowError, "Immutable artifact changed or is missing: plan.md"):
            self.checkpoint()
        self.assertEqual(plan.read_bytes(), b"A different plan.\n")
        self.assertEqual(self.state()["checkpoint_count"], 0)

    def test_an_answer_whose_save_stopped_is_never_an_answer(self):
        self.start()
        self.ready()
        self.escalate()
        question = {"questions": [{"question": "Which VALUE should be implemented?"}]}
        self.hook("PreToolUse", tool_name="AskUserQuestion", tool_use_id="stopped", tool_input=question)
        with self.stopped_commit(), self.assertRaises(InjectedStop):
            self.hook("PostToolUse", tool_name="AskUserQuestion", tool_use_id="stopped", tool_input=question,
                      tool_response={"answers": {"Which VALUE should be implemented?": "1"}})
        self.assertEqual(self.engine.status(self.run_id)["human_events"], [])

    def test_a_check_whose_last_save_stopped_waits_for_human_recovery(self):
        self.start()
        self.source.write_text("VALUE = 1\n", encoding="utf-8")
        self.assertEqual(self.checkpoint()["status"], "ready_check")
        original = runs.atomic_json
        saves = []

        def stop_the_last(path, value):
            if Path(path).name == "state.json" and Path(path).parent == self.run:
                saves.append(value["status"])
                if value["status"] != "checking":
                    raise InjectedStop("stopped before the commit")
            return original(path, value)
        with patch.object(runs, "atomic_json", side_effect=stop_the_last), self.assertRaises(InjectedStop):
            self.engine.check(self.run_id, self.session)
        stopped = self.state()
        self.assertEqual((stopped["status"], stopped["inflight"]["kind"]), ("checking", "check"))
        logs = list((self.run / "check-processes").glob("*/verify.stdout.log"))
        self.assertTrue(logs)
        with self.assertRaisesRegex(DevFlowError, "Cannot perform this action in checking"):
            self.engine.check(self.run_id, self.session)
        recovered = self.engine.recover(self.run_id, confirmed=True)
        self.assertEqual((recovered["status"], recovered["inflight"]), ("paused_error", None))
        self.assertIsNone(recovered["validation"])
        self.assertTrue(all(path.exists() for path in logs))

    def test_a_review_whose_last_save_stopped_waits_for_human_recovery(self):
        self.start()
        self.ready()
        original = runs.atomic_json

        def stop_the_last(path, value):
            if Path(path).name == "state.json" and Path(path).parent == self.run and value["status"] != "reviewing":
                raise InjectedStop("stopped before the commit")
            return original(path, value)
        with patch.object(runs, "atomic_json", side_effect=stop_the_last), self.assertRaises(InjectedStop):
            self.review()
        stopped = self.state()
        self.assertEqual((stopped["status"], stopped["inflight"]["kind"]), ("reviewing", "review"))
        with self.assertRaisesRegex(DevFlowError, "Cannot perform this action in reviewing"):
            self.review()
        recovered = self.engine.recover(self.run_id, confirmed=True)
        self.assertEqual((recovered["review_count"], recovered["attempts"][0]["status"]), (1, "failed"))

    def test_stale_lock_requires_human_recovery_and_is_not_auto_deleted(self):
        self.start()
        lock = self.run / "state.lock"
        content = json_bytes({"token": "old-token", "pid": 999999999, "host": "unreachable-host", "created_at": "2000-01-01T00:00:00+00:00"})
        lock.write_bytes(content)
        impatient = Engine(self.project, lock_wait=0.2)
        with self.assertRaisesRegex(DevFlowError, "locked"):
            impatient.checkpoint(self.run_id, self.session, self.draft_text("summary.md", "Blocked by a stale lock."))
        self.assertEqual(lock.read_bytes(), content)
        self.assertEqual(self.state()["checkpoint_count"], 0)

    def test_source_edits_are_never_stopped_but_invalidate_stale_results(self):
        # Decision D1: DevFlow does not stop any tool. Changed code is detected
        # by the snapshot hash instead, before and after every check and review.
        self.start()
        for path in (self.source, self.plan, self.run / "state.json", self.draft / "notes.md"):
            self.assertIsNone(self.tool(file_path=str(path)))
        self.source.write_text("VALUE = 1\n", encoding="utf-8")
        self.checkpoint()
        self.assertIsNone(self.tool(file_path=str(self.source)))
        self.source.write_text("VALUE = 1\n# edited after the checkpoint\n", encoding="utf-8")
        with self.assertRaisesRegex(DevFlowError, "changed since checkpoint"):
            self.engine.check(self.run_id, self.session)
        self.source.write_text("VALUE = 1\n", encoding="utf-8")
        self.assertEqual(self.engine.check(self.run_id, self.session)["status"], "ready_review")

        def edit_during_review(*args, **kwargs):
            self.assertIsNone(self.tool(file_path=str(self.source)))
            self.source.write_text("VALUE = 2\n", encoding="utf-8")
            return self.respond(*args, **kwargs)

        self.reviewer.side_effect = edit_during_review
        result = self.review()
        self.assertEqual(result["status"], "paused_error")
        self.assertIn("changed since checkpoint", result["last_error"])
        self.assertEqual(result["review_count"], 1)
        self.assertEqual(result["attempts"][-1]["status"], "failed")
        # The reviewer ran to the end: what it did stays on the failed round.
        self.assertEqual([(r["status"], r["tool_calls"]) for r in result["reviews"]],
                         [("failed", ACTIVITY["tool_calls"])])

    def run_checks_with(self, during):
        original = checks_module.run_checks

        def wrapper(*args, **kwargs):
            during()
            return original(*args, **kwargs)

        return patch.object(checks_module, "run_checks", side_effect=wrapper)

    def test_event_appended_from_another_thread_during_review_survives(self):
        self.start()
        self.ready()
        recorded = []

        def dispatch(*args, **kwargs):
            recorded.append(in_thread(lambda: self.prompt("A note typed while the reviewer runs.")))
            recorded.append(in_thread(lambda: self.engine.store.append_event(
                self.run_id, {"session_id": self.session, "hook_event_name": "UserPromptSubmit",
                              "prompt": "A second note."}, "prompt")))
            self.assertEqual(self.state()["status"], "reviewing")
            return self.respond(*args, **kwargs)

        self.reviewer.side_effect = dispatch
        result = self.review()
        self.assertEqual((result["status"], result["review_count"]), ("ready_review", 1))
        self.assertEqual(len(recorded), 2)
        for event_id in recorded:
            self.assertRegex(event_id, EVENT_ID)
            self.assertIn(event_id, self.state()["event_ids"])

    def test_event_appended_from_another_thread_during_check_survives(self):
        self.start()
        self.source.write_text("VALUE = 1\n", encoding="utf-8")
        self.checkpoint()
        recorded = []

        def during():
            self.assertEqual(self.state()["status"], "checking")
            self.assertEqual(self.state()["inflight"]["kind"], "check")
            self.assertFalse((self.run / "state.lock").exists())
            recorded.append(in_thread(lambda: self.prompt("A note typed while checks run.")))

        with self.run_checks_with(during):
            result = self.engine.check(self.run_id, self.session)
        self.assertEqual(result["status"], "ready_review")
        self.assertIsNone(result["inflight"])
        self.assertRegex(recorded[0], EVENT_ID)
        self.assertIn(recorded[0], self.state()["event_ids"])

    def test_concurrent_reviews_do_not_both_run(self):
        self.start()
        self.ready()
        refusals = []

        def dispatch(*args, **kwargs):
            for action in (lambda other: other.review(self.run_id, self.session),
                           lambda other: other.check(self.run_id, self.session)):
                with self.assertRaisesRegex(DevFlowError, "reviewing") as caught:
                    in_thread(lambda: action(Engine(self.project)))
                refusals.append(str(caught.exception))
            return self.respond(*args, **kwargs)

        self.reviewer.side_effect = dispatch
        result = self.review()
        self.assertEqual(len(refusals), 2)
        self.assertEqual(self.reviewer.call_count, 1)
        self.assertEqual(result["review_count"], 1)
        self.assertEqual(sorted(path.name for path in (self.run / "reviews").iterdir()), ["01"])

    def test_concurrent_checks_do_not_both_run(self):
        self.start()
        self.source.write_text("VALUE = 1\n", encoding="utf-8")
        self.checkpoint()
        refusals = []

        def during():
            with self.assertRaisesRegex(DevFlowError, "checking") as caught:
                in_thread(lambda: Engine(self.project).check(self.run_id, self.session))
            refusals.append(str(caught.exception))

        with self.run_checks_with(during):
            result = self.engine.check(self.run_id, self.session)
        self.assertEqual(len(refusals), 1)
        self.assertEqual(result["status"], "ready_review")
        self.assertEqual(result["check_attempts"], 1)
        self.assertEqual(sorted(path.name for path in (self.run / "check-processes").iterdir()), ["001"])

    def test_recover_at_the_review_limit_leads_to_the_question(self):
        self.start()
        self.ready()
        self.fail_until_last()
        self.reviewer.side_effect = KeyboardInterrupt("simulated caller interruption")
        with self.assertRaises(KeyboardInterrupt):
            self.review()
        self.assertEqual((self.state()["status"], len(self.state()["attempts"])), ("reviewing", 6))
        recovered = self.engine.recover(self.run_id, confirmed=True)
        self.assertEqual(recovered["status"], "ready_review")
        self.assertIsNone(recovered["inflight"])
        self.assertEqual(recovered["review_count"], 6)
        self.assertEqual(recovered["attempts"][-1]["status"], "failed")
        self.assertEqual(self.review()["status"], "awaiting_human")
        stopped = self.at_limit("stop")
        self.assertFalse(stopped["report"]["all_reviewers_approved"])
        self.assertFinalReportSaved(stopped)

    def test_recover_during_check_gives_paused_error_and_resumes_to_implementing(self):
        self.start()
        self.source.write_text("VALUE = 1\n", encoding="utf-8")
        self.checkpoint()

        def interrupted():
            raise KeyboardInterrupt("simulated caller interruption")

        with self.run_checks_with(interrupted), self.assertRaises(KeyboardInterrupt):
            self.engine.check(self.run_id, self.session)
        self.assertEqual(self.state()["status"], "checking")
        self.assertNotAccepted(f"/devflow:resume {self.run_id}", "may still be running")
        with self.assertRaisesRegex(DevFlowError, "child processes"):
            self.engine.cancel(self.run_id, self.session, "Cannot cancel while a check may run.")
        recovered = self.engine.recover(self.run_id, confirmed=True)
        self.assertEqual(recovered["status"], "paused_error")
        self.assertEqual(recovered["resume_status"], "implementing")
        self.assertIsNone(recovered["inflight"])
        self.assertEqual(recovered["check_attempts"], 1)
        self.assertEqual(recovered["review_count"], 0)
        self.assertEqual(self.resume()["status"], "implementing")

    def assertLate(self, kind, number, *, error):
        late = sorted((self.run / "late").glob(f"{kind}-{number:03}-*.json"))
        self.assertEqual(len(late), 1, late)
        value = read_json(late[0])
        self.assertEqual(value["reservation"]["kind"], kind)
        self.assertEqual(value["reservation"]["number"], number)
        if error:
            self.assertIsNone(value["result"])
            self.assertTrue(value["error"])
        else:
            self.assertIsNotNone(value["result"])
            self.assertIsNone(value["error"])
        return value

    def test_stale_review_result_after_recover_or_cancel_goes_to_late(self):
        self.start()
        self.ready()
        frozen = {}

        def recover_meanwhile(*args, **kwargs):
            # A human wrongly recovers while the reviewer is still running.
            in_thread(lambda: Engine(self.project).recover(self.run_id, confirmed=True))
            frozen["state"] = (self.run / "state.json").read_bytes()
            return self.respond(*args, **kwargs)

        self.reviewer.side_effect = recover_meanwhile
        with self.assertRaisesRegex(DevFlowError, "stale review"):
            self.review()
        self.assertEqual((self.run / "state.json").read_bytes(), frozen["state"])
        self.assertEqual(self.state()["status"], "paused_error")
        self.assertFalse((self.run / "reviews" / "01" / "review.json").exists())
        self.assertEqual(self.assertLate("review", 1, error=False)["result"]["review"]["verdict"], "pass")

        self.resume()

        def recover_and_cancel_meanwhile(*args, **kwargs):
            in_thread(lambda: Engine(self.project).recover(self.run_id, confirmed=True))
            in_thread(lambda: Engine(self.project).cancel(self.run_id, self.session, "Stop this run."))
            frozen["state"] = (self.run / "state.json").read_bytes()
            raise DevFlowError("Reviewer failed after the run was cancelled")

        self.reviewer.side_effect = recover_and_cancel_meanwhile
        with self.assertRaisesRegex(DevFlowError, "stale review"):
            self.review()
        self.assertEqual((self.run / "state.json").read_bytes(), frozen["state"])
        self.assertEqual(self.state()["status"], "cancelled")
        self.assertIn("Reviewer failed", self.assertLate("review", 2, error=True)["error"])

    def test_stale_check_result_after_recover_or_cancel_goes_to_late(self):
        self.start()
        self.source.write_text("VALUE = 1\n", encoding="utf-8")
        self.checkpoint()
        frozen = {}

        def recover_and_cancel_meanwhile():
            in_thread(lambda: Engine(self.project).recover(self.run_id, confirmed=True))
            in_thread(lambda: Engine(self.project).cancel(self.run_id, self.session, "Stop this run."))
            frozen["state"] = (self.run / "state.json").read_bytes()

        with self.run_checks_with(recover_and_cancel_meanwhile):
            with self.assertRaisesRegex(DevFlowError, "stale check"):
                self.engine.check(self.run_id, self.session)
        self.assertEqual((self.run / "state.json").read_bytes(), frozen["state"])
        self.assertEqual(self.state()["status"], "cancelled")
        self.assertEqual(self.state()["check_history"], [])
        self.assertEqual(self.assertLate("check", 1, error=False)["result"]["status"], "passed")

    def test_stale_failed_check_after_recover_goes_to_late(self):
        self.start()
        self.source.write_text("VALUE = 1\n", encoding="utf-8")
        self.checkpoint()
        frozen = {}

        def recover_then_fail():
            in_thread(lambda: Engine(self.project).recover(self.run_id, confirmed=True))
            frozen["state"] = (self.run / "state.json").read_bytes()
            raise DevFlowError("Check process could not be launched")

        with self.run_checks_with(recover_then_fail):
            with self.assertRaisesRegex(DevFlowError, "stale check"):
                self.engine.check(self.run_id, self.session)
        self.assertEqual((self.run / "state.json").read_bytes(), frozen["state"])
        self.assertEqual(self.state()["status"], "paused_error")
        self.assertIn("could not be launched", self.assertLate("check", 1, error=True)["error"])


@unittest.skipUnless(shutil.which("git"), "Native git is unavailable")
class DirtyGitProjectTests(EngineFixture):
    def git(self, *args):
        environment = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        environment.pop("GIT_DIR", None)
        environment.pop("GIT_WORK_TREE", None)
        return subprocess.run([shutil.which("git"), *args], cwd=self.project, env=environment,
                              capture_output=True, check=True, text=True, timeout=15).stdout

    def initialize_git(self):
        self.config["snapshot"]["mode"] = "git"
        self.write_config()
        self.git("init", "--quiet")
        self.git("add", "module.py", "devflow.config.json")
        self.git("-c", "user.name=DevFlow offline tests", "-c", "user.email=tests@example.invalid", "commit", "--quiet", "-m", "Fixture baseline")
        self.source.write_text("VALUE = 41  # Existing user edit\n", encoding="utf-8")
        (self.project / "user-notes.txt").write_text("Do not discard my work.\n", encoding="utf-8")

    def test_existing_dirty_tree_is_retained_and_requires_acknowledgment(self):
        self.initialize_git()
        previous_head = self.git("rev-parse", "HEAD").strip()
        original = self.source.read_bytes()
        result = self.start()
        self.assertEqual(result["status"], "awaiting_human")
        self.assertEqual(result["escalation"]["detail"]["kind"], "dirty_worktree")
        self.assertEqual(result["escalation"]["boundary"], 1)
        self.assertEqual(result["escalation"]["session_id"], self.session)
        baseline = read_json(self.run / "baseline.json")
        self.assertTrue(baseline["git"]["dirty"])
        # The edit made before the run is kept once, as a copy; the record holds no contents.
        copy = baseline["files"]["module.py"]["source"]["copy"]
        self.assertEqual(copy, "baseline/" + digest(original))
        self.assertEqual((self.run / copy).read_bytes(), original)
        self.assertNotIn(original.decode("utf-8").strip(), (self.run / "baseline.json").read_text(encoding="utf-8"))
        self.assertEqual(self.source.read_bytes(), original)
        event = self.answer("Preserve the existing edits and use them as the implementation baseline.")
        result = self.engine.resolve(self.run_id, self.session, event, "continue", "The user acknowledged the dirty baseline.")
        self.assertEqual(result["status"], "implementing")
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual((self.project / "user-notes.txt").read_text(encoding="utf-8"), "Do not discard my work.\n")
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), previous_head)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")

    def clean_git(self, **files):
        """A clean committed repository holding module.py and ``files``."""
        self.config["snapshot"]["mode"] = "git"
        self.write_config()
        for name, text in files.items():
            (self.project / name).write_text(text, encoding="utf-8")
        self.git("init", "--quiet")
        self.git("add", "--all")
        self.git("-c", "user.name=DevFlow offline tests", "-c", "user.email=tests@example.invalid", "commit", "--quiet", "-m", "Fixture baseline")

    def test_no_record_holds_the_contents_of_an_unchanged_file(self):
        sentinel = "UNCHANGED-FILE-CONTENTS-5d2c41"
        self.clean_git(**{"stable.py": f"# {sentinel}\n"})
        self.start()
        self.ready()
        self.review()
        self.assertEqual(sorted(path.name for path in (self.run / "reviews" / "01").iterdir()), ["packet.json", "review.json"])
        self.assertFalse((self.run / "baseline").exists())  # Git holds every start-time byte.
        for path in self.run.rglob("*"):
            if path.is_file():
                with self.subTest(path=path.relative_to(self.run)):
                    self.assertNotIn(sentinel.encode(), path.read_bytes())

    def test_a_large_diff_is_written_once(self):
        self.clean_git()
        self.start()
        self.source.write_text("".join(f"VALUE_{index} = {index}\n" for index in range(300_000)), encoding="utf-8")
        result = self.checkpoint()
        diff = self.run / result["candidate"]["diff"]
        self.assertEqual(result["candidate"]["diff"], "snapshots/001.diff")
        self.assertGreater(diff.stat().st_size, 3_000_000)
        large = [path.relative_to(self.run).as_posix() for path in self.run.rglob("*")
                 if path.is_file() and path.stat().st_size > 1_000_000]
        self.assertEqual(large, ["snapshots/001.diff"])

    def test_explicit_allow_dirty_keeps_same_baseline_without_question(self):
        self.config["allow_dirty"] = True
        self.initialize_git()
        original = self.source.read_bytes()
        result = self.start()
        self.assertEqual(result["status"], "implementing")
        self.assertEqual(result["escalations"], [])
        self.assertIsNone(result["escalation"])
        self.assertEqual(self.source.read_bytes(), original)


class SummaryAndAcceptanceTests(EngineFixture):
    """0.7.0: the review summary, accepting at the review limit, and the terminal report."""

    def at_the_limit(self):
        self.config["max_reviews"] = 2
        self.write_config()
        self.start()
        self.ready()
        self.review()  # codex approves checkpoint 1
        self.response_kind = "changes_required"
        self.review()  # opencode finds F1 in checkpoint 1
        self.reflect()
        self.ready(2)
        asked = self.review()
        self.assertEqual(asked["escalation"]["detail"]["kind"], "review_budget")
        return asked

    def accept(self):
        evidence = self.answer("Accept and finish.")
        return self.engine.resolve(self.run_id, self.session, evidence, "accept", "The user accepted this version.")

    def with_output(self, lines, error=None):
        """The reviewer CLI printed ``lines``; ``error`` fails the attempt after that."""
        def reviewer(config, packet_path, work_dir, *args, **kwargs):
            Path(work_dir).mkdir(parents=True, exist_ok=True)
            (Path(work_dir) / "invocation.json").write_text(json.dumps({"cli_version": "codex-cli 9.9.9"}), encoding="utf-8")
            (Path(work_dir) / "stdout.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
            if error is not None:
                raise error
            return self.respond(config, packet_path, work_dir, *args, **kwargs)
        self.reviewer.side_effect = reviewer

    def test_accepting_ends_as_accepted_with_the_verified_checkpoint(self):
        asked = self.at_the_limit()
        self.assertEqual(asked["review_summary"]["status"], "awaiting_human")
        self.assertIn("- Status: awaiting_human (review limit question)", asked["review_summary_markdown"])
        self.assertEqual(asked["review_summary"]["approval"]["current_version"]["label"], "checkpoint 2")
        accepted = self.accept()
        self.assertEqual((accepted["status"], accepted["review_limit"]), ("accepted", 2))
        self.assertFinalReportSaved(accepted)
        report = accepted["report"]
        candidate = accepted["candidate"]["snapshot_id"]
        resolution = read_json(self.run / accepted["resolutions"][-1])
        self.assertEqual((resolution["decision"], resolution["snapshot_id"]), ("accept", candidate))
        self.assertEqual(report["human_acceptance"], {
            "event_ids": [resolution["human_event"]["event_id"]], "answer": resolution["human_event"]["answer_text"],
            "version": candidate, "accepted_at": resolution["created_at"], "record": accepted["resolutions"][-1]})
        self.assertEqual(report["warning"], "Accepted by the human at the review limit without approval from every reviewer.")
        self.assertFalse(report["all_reviewers_approved"])
        self.assertFalse(report["final_snapshot_reviewed"])
        summary = report["review_summary"]
        self.assertEqual(summary["approval"]["latest_version_reviewed"], report["final_snapshot_reviewed"])
        self.assertEqual(summary["reviews"][1]["fix"]["file_changes"], [{"path": "module.py", "change": "modified"}])
        self.assertEqual(summary["reviews"][1]["findings"][0]["location"], "module.py:1-1")
        # After the end, status rebuilds the same summary from the report's capture.
        self.assertEqual(accepted["review_summary"], summary)
        self.assertIn("- Status: accepted — accepted by the human", accepted["review_summary_markdown"])
        self.assertNotIn("review_summary_markdown", report)

    def test_accepting_needs_the_checkpoint_in_the_workspace_and_its_validation(self):
        self.at_the_limit()
        self.source.write_text("VALUE = 99\n", encoding="utf-8")
        with self.assertRaisesRegex(DevFlowError, "Cannot accept: Code changed since checkpoint.*Restore the workspace"):
            self.accept()
        self.assertEqual(self.state()["status"], "awaiting_human")
        self.source.write_text("VALUE = 2\n", encoding="utf-8")
        with self.engine._lock(self.run_id):
            state = self.engine._state(self.run_id)
            state["validation"] = None
            with self.assertRaisesRegex(DevFlowError, "no passing checks or human validation"):
                self.engine._accept_current(state)
        self.assertEqual(self.accept()["status"], "accepted")

    def test_cancel_never_changes_an_ended_run(self):
        self.at_the_limit()
        accepted = self.accept()
        cancelled = self.engine.cancel(self.run_id, self.session, "Too late.")
        self.assertEqual((cancelled["status"], cancelled["state_revision"], cancelled["report_path"]),
                         ("accepted", accepted["state_revision"], accepted["report_path"]))

    def test_a_completed_report_summarizes_the_review_saved_with_it(self):
        self.start()
        self.ready()
        self.review()
        completed = self.review()
        self.assertEqual(completed["status"], "completed")
        summary = completed["report"]["review_summary"]
        self.assertEqual([entry["verdict"] for entry in summary["reviews"]], ["pass", "pass"])
        self.assertTrue(summary["approval"]["all_reviewers_approved"])
        self.assertIsNone(completed["report"]["warning"])

    def test_the_terminal_summary_follows_the_reports_workspace_capture(self):
        self.start()
        self.ready()
        self.review()
        self.source.write_text("VALUE = 5\n", encoding="utf-8")  # changed after the review, never reviewed
        cancelled = self.engine.cancel(self.run_id, self.session, "Stopping.")
        report = cancelled["report"]
        self.assertFalse(report["final_snapshot_reviewed"])
        current = report["review_summary"]["approval"]["current_version"]
        self.assertEqual(current, {"id": report["workspace_snapshot_id_at_report"],
                                   "label": "workspace at report (not a checkpoint)"})
        self.assertFalse(report["review_summary"]["approval"]["latest_version_reviewed"])
        # Status never captures the workspace again.
        with patch("devflow_core.implement.engine.snapshot.capture", side_effect=AssertionError("captured")):
            self.assertEqual(self.engine.status(self.run_id)["review_summary"], report["review_summary"])

    def test_a_failed_capture_is_unknown_in_the_summary(self):
        self.start()
        self.ready()
        with patch("devflow_core.implement.engine.snapshot.capture", side_effect=DevFlowError("unreadable")):
            cancelled = self.engine.cancel(self.run_id, self.session, "Stopping.")
        summary = cancelled["report"]["review_summary"]
        self.assertEqual(summary["approval"]["current_version"], {"id": None, "label": "unknown (workspace capture failed)"})
        self.assertFalse(summary["approval"]["latest_version_reviewed"])
        self.assertEqual(cancelled["review_summary"], summary)

    def test_an_oversized_report_still_ends_the_run_and_status_shows_the_summary(self):
        from devflow_core.common import summary as summary_module
        from devflow_core.implement import workflow as workflow_module
        real = workflow_module.check_json_size
        def refuse_reports(name, data):
            if name.startswith("reports/"):
                raise DevFlowError("over the limit")
            return real(name, data)
        for ending in ("accept", "stop", "cancel"):
            with self.subTest(ending=ending):
                self.setUp()
                self.at_the_limit()
                with patch.object(workflow_module, "check_json_size", side_effect=refuse_reports):
                    if ending == "accept":
                        ended = self.accept()
                    elif ending == "stop":
                        ended = self.at_limit("stop")
                    else:
                        ended = self.engine.cancel(self.run_id, self.session, "Stopping.")
                self.assertEqual(ended["status"], {"accept": "accepted", "stop": "exhausted", "cancel": "cancelled"}[ending])
                report = ended["report"]
                self.assertEqual((report["review_summary"], report["review_summary_error"]),
                                 (None, summary_module.TOO_LARGE))
                if ending == "accept":
                    self.assertEqual(report["human_acceptance"]["version"], ended["candidate"]["snapshot_id"])
                self.assertIsNotNone(ended["review_summary"])
                self.assertIn("DevFlow review summary", ended["review_summary_markdown"])

    def test_a_summary_that_cannot_be_built_never_blocks_the_end(self):
        self.start()
        self.ready()
        with patch("devflow_core.implement.engine.summary_source", side_effect=DevFlowError("damaged record")):
            cancelled = self.engine.cancel(self.run_id, self.session, "Stopping.")
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIsNone(cancelled["report"]["review_summary"])
        self.assertIn("damaged record", cancelled["report"]["review_summary_error"])

    def test_attempts_keep_their_checkpoint_and_tokens_whatever_the_outcome(self):
        self.start()
        self.ready()
        usage = {"type": "turn.completed", "usage": {"input_tokens": 11}}
        self.with_output([usage])
        self.review()
        first = self.state()["attempts"][0]
        self.assertEqual((first["snapshot_path"], first["tokens"]), ("snapshots/001.json", {"input_tokens": 11}))
        step = {"type": "step_finish", "part": {"tokens": {"input": 4}}}
        self.with_output([step], error=DevFlowError("invalid answer"))
        self.review()
        second = self.state()["attempts"][1]
        self.assertEqual((second["status"], second["tokens"]), ("failed", {"input": 4}))
        summary = self.engine.cancel(self.run_id, self.session, "Stopping.")["report"]["review_summary"]
        self.assertEqual([entry["cli_version"] for entry in summary["reviews"]], ["codex-cli 9.9.9", "codex-cli 9.9.9"])
        self.assertEqual(summary["reviews"][1]["error"], "invalid answer")

    def test_a_recovered_attempt_keeps_its_tokens(self):
        self.start()
        self.ready()
        self.with_output([{"type": "turn.completed", "usage": {"input_tokens": 3}}], error=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.review()
        recovered = self.engine.recover(self.run_id, confirmed=True)
        attempt = recovered["attempts"][0]
        self.assertEqual((attempt["status"], attempt["tokens"]), ("failed", {"input_tokens": 3}))

    def test_cancelling_with_deeply_nested_process_records_still_ends_the_run(self):
        self.start()
        self.ready()
        deep = "[" * 100000 + "]" * 100000
        def reviewer(config, packet_path, work_dir, *args, **kwargs):
            Path(work_dir).mkdir(parents=True, exist_ok=True)
            (Path(work_dir) / "invocation.json").write_text(deep, encoding="utf-8")
            (Path(work_dir) / "stdout.jsonl").write_text(deep + "\n", encoding="utf-8")
            return self.respond(config, packet_path, work_dir, *args, **kwargs)
        self.reviewer.side_effect = reviewer
        reviewed = self.review()
        self.assertIsNone(reviewed["attempts"][0]["tokens"])
        cancelled = self.engine.cancel(self.run_id, self.session, "Stopping.")
        self.assertEqual(cancelled["status"], "cancelled")
        entry = cancelled["report"]["review_summary"]["reviews"][0]
        self.assertEqual((entry["cli_version"], entry["cli_version_source"]),
                         (None, "review-processes/01/invocation.json"))

    def test_cancelling_with_metadata_a_record_cannot_hold_still_ends_the_run(self):
        self.start()
        self.ready()
        def reviewer(config, packet_path, work_dir, *args, **kwargs):
            Path(work_dir).mkdir(parents=True, exist_ok=True)
            (Path(work_dir) / "invocation.json").write_text(json.dumps({"cli_version": "codex \ud800"}), encoding="utf-8")
            (Path(work_dir) / "stdout.jsonl").write_text(json.dumps(
                {"type": "turn.completed", "usage": {"\ud800": 1}}) + "\n", encoding="utf-8")
            return self.respond(config, packet_path, work_dir, *args, **kwargs)
        self.reviewer.side_effect = reviewer
        self.assertIsNone(self.review()["attempts"][0]["tokens"])
        cancelled = self.engine.cancel(self.run_id, self.session, "Stopping.")
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIsNone(cancelled["report"]["review_summary"]["reviews"][0]["cli_version"])


if __name__ == "__main__":
    unittest.main()
