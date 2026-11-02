"""Offline state-machine integration: real files/hooks, mocked reviewer calls."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from devflow_core import cli, router
from devflow_core.common import paths, sessions
from devflow_core.common import runs as runs_module
from devflow_core.common.errors import DevFlowError, ReviewerError
from devflow_core.common.storage import file_lock, read_json
from devflow_core.plan import engine as engine_module
from devflow_core.plan.engine import Engine
from devflow_core.plan.review import validate_review
from devflow_core.workflows import WORKFLOWS

PLUGIN_ROOT = paths.PLUGIN_ROOT
RESOURCES = WORKFLOWS["plan"].resource_dir
RUN_REVIEW = "devflow_core.plan.engine.run_review"
PREFLIGHT = "devflow_core.plan.engine.preflight"
# What a successful local CLI probe returns; reviews never call a real CLI here.
PROBE = {"kind": "fixture", "executable": "/fixture/reviewer", "cli_version": "fixture 1.0.0",
         "protocol_version": None, "requested_model": "fixture", "requested_effort": "high",
         "effective_model_attested": False}
LEGACY = "plan" + "flow"  # A pre-DevFlow name, assembled so no tracked file spells it.


class InjectedCrash(RuntimeError):
    """A deterministic crash boundary, not an actual killed process."""


def review_value(verdict="pass", *, human=False, severity="medium"):
    findings = [] if verdict == "pass" else [{
        "id": "F1", "severity": severity, "category": "requirements",
        "location": "Plan / validation", "evidence": "A validation case is missing.",
        "recommendation": "Add the agreed validation case.", "requires_human": human,
    }]
    return {
        "schema_version": 1, "verdict": verdict,
        "summary": "No unresolved findings." if verdict == "pass" else "A decision or change is needed.",
        "findings": findings,
        "questions": [{"id": "Q1", "question": "Allow the new scope?", "why_human": "Changes an agreed constraint.",
                       "options": ["Keep scope", "Expand scope"]}] if human else [],
    }


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="devflow plan integration ")
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve() / "project with spaces"
        self.project.mkdir()
        # Keep all run records within this test's own disposable project.
        (self.project / ".git").mkdir()
        self.transcript = self.project / "claude-transcript.jsonl"
        self.session = "claude-test-session-1"
        self.engine = Engine(self.project)
        self.run_id = None
        # The reviewer CLI check happens before every reservation; it is a
        # local process probe, so tests replace it with a successful result.
        preflight = patch(PREFLIGHT, return_value=PROBE)
        self.preflight = preflight.start()
        self.addCleanup(preflight.stop)

    # -- hooks and runner -----------------------------------------------------------------
    def event(self, name="UserPromptSubmit", session=None, **extra):
        return {"hook_event_name": name, "cwd": str(self.project), "session_id": session or self.session,
                "transcript_path": str(self.transcript), **extra}

    def question(self, response, tool_use_id="question-1", session=None, **extra):
        return self.event("PostToolUse", session=session, tool_name="AskUserQuestion", tool_use_id=tool_use_id,
                          tool_input={"questions": [{"question": "Scope?"}]}, tool_response=response, **extra)

    def hook(self, payload):
        return router.handle_hook(payload, PLUGIN_ROOT)

    def record(self, payload):
        """Send a human event through the router and return the event ID it reports."""
        context = self.hook(payload)["hookSpecificOutput"]["additionalContext"]
        return re.search(r'(?:as event |"recorded_event_id": ")([a-f0-9]{32})', context).group(1)

    def resume(self, session):
        """``/devflow:resume RUN`` from ``session``; the hook performs the whole move."""
        result = self.hook(self.event(prompt=f"/devflow:resume {self.run_id}", session=session))
        self.assertIn("additionalContext", result["hookSpecificOutput"])
        return self.engine.status(self.run_id)

    def bound_run(self, session=None):
        record = sessions.read_record(self.project, session or self.session, WORKFLOWS)
        return record["run"]["run_id"] if record and record["run"] else None

    def runner(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--project", str(self.project), *args])
        return code, json.loads(out.getvalue())

    def resolve(self, answer_file, session=None):
        return self.engine.resolve(self.run_id, answer_file, session or self.session)

    # -- workflow steps -------------------------------------------------------------------
    def append(self, role, content):
        record = {"type": role, "message": {"role": role, "content": content}}
        with self.transcript.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    def start_interview(self, answer=True, request=None):
        self.raw_request = request or "/devflow:plan 実装せず、移行計画を作る。\nKeep exact wording."
        result = self.hook(self.event(prompt=self.raw_request))
        self.assertIn("additionalContext", result["hookSpecificOutput"])
        self.run_id = router.status(self.project, self.session, None)["run"]["run_id"]
        self.assertEqual(self.bound_run(), self.run_id)
        self.run = Path(self.engine.status(self.run_id)["run_dir"])
        self.draft_dir = self.run / "draft"
        self.append("user", self.raw_request)
        self.append("assistant", "grill-me question: downtime limit?")
        if answer:
            self.answer = "停止は 5 分まで。\n  Preserve this spacing."
            self.hook(self.event(prompt=self.answer))
            self.append("user", self.answer)
        return self.engine.status(self.run_id)

    def draft(self, name, value):
        path = self.draft_dir / name
        path.write_text(json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value, encoding="utf-8")
        return path

    def seal(self):
        decisions = self.draft("decisions.json", {
            "summary": "Human confirmed the downtime limit.", "decisions": ["Plan only."],
            "constraints": ["Downtime at most five minutes."], "open_questions": [],
        })
        return self.engine.seal(self.run_id, decisions)

    def ready(self, request=None):
        self.start_interview(request=request)
        self.seal()
        return self.engine.plan(self.run_id, self.draft("plan.md", "# Initial plan\n\nDowntime <= 5 minutes.\n"))

    def completed(self, request=None):
        self.ready(request=request)
        self.submit(review_value())
        return self.submit(review_value())

    def submit(self, value):
        # Like the real run_review, return only what the workflow's validator accepts.
        def reviewer(provider, packet, work_dir, schema, instructions, validate, probe):
            return validate(value)
        with patch(RUN_REVIEW, side_effect=reviewer) as call:
            result = self.engine.review(self.run_id)
        self.assertEqual(call.call_count, 1)
        return result

    def reflect(self, state, *, action="accept", change=True, decisions=None, expected_hash=None):
        old_plan = Path(state["current_plan"]).read_text(encoding="utf-8")
        new_plan = old_plan + f"\nValidation added after review {state['review_count']}.\n" if change else old_plan
        plan = self.draft("revised.md", new_plan)
        reflection = self.draft("reflection.json", {
            "review_number": state["review_count"], "summary": "Reviewed each finding against the agreed constraints.",
            "decisions": decisions if decisions is not None else [{
                "finding_id": "F1", "action": action, "reason": "Concrete evidence and disposition for this finding.",
            }],
        })
        return self.engine.apply(self.run_id, plan, reflection, expected_hash or state["plan_hash"])

    def test_two_independent_passes_same_hash_complete(self):
        initial = self.ready()
        with patch(RUN_REVIEW, side_effect=[review_value(), review_value()]) as call:
            first = self.engine.review(self.run_id)
            second = self.engine.review(self.run_id)
        self.assertEqual(first["status"], "ready")
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["review_count"], 2)
        self.assertEqual(second["plan_hash"], initial["plan_hash"])
        self.assertTrue(second["final_plan_reviewed"])
        self.assertIsNone(second["inflight"])
        self.assertEqual([c.args[0]["id"] for c in call.call_args_list], ["codex", "opencode"])
        self.assertTrue(self.engine.report(self.run_id)["converged"])
        self.assertTrue((self.run / "reviews" / "01" / "reflection.json").is_file())

    def test_packet_contains_raw_request_full_interview_and_plan(self):
        self.append("user", "PREVIOUS SESSION SECRET")
        initial = self.ready()
        captured = {}
        def reviewer(provider, packet, work_dir, schema, instructions, validate, probe):
            captured["packet"] = packet.read_text(encoding="utf-8")
            captured["work_dir"] = work_dir
            return review_value()
        with patch(RUN_REVIEW, side_effect=reviewer):
            self.engine.review(self.run_id)
        packet = captured["packet"]
        payload = json.loads(packet)
        self.assertEqual(payload["original_request"]["raw_prompt"], self.raw_request)
        self.assertEqual(payload["interview_verbatim"]["messages"][-1]["content"], self.answer)
        self.assertEqual(payload["plan_sha256"], initial["plan_hash"])
        self.assertNotIn("PREVIOUS SESSION SECRET", packet)
        # The reviewer works in this run's own review directory.
        self.assertEqual(captured["work_dir"], self.run / "reviews" / "01")

    def test_packet_is_review_data_without_reviewer_instructions(self):
        self.ready()
        captured = {}
        def reviewer(provider, packet, work_dir, schema, instructions, validate, probe):
            captured.update(packet=packet.read_text(encoding="utf-8"), schema=schema,
                            instructions=instructions, validate=validate, probe=probe)
            return review_value()
        with patch(RUN_REVIEW, side_effect=reviewer):
            self.engine.review(self.run_id)
        packet = captured["packet"]
        # The packet is review data only; common.reviewers adds the instructions once.
        self.assertIsInstance(json.loads(packet), dict)
        self.assertEqual(captured["instructions"], RESOURCES / "reviewer.md")
        self.assertEqual(captured["schema"], RESOURCES / "review.schema.json")
        instructions = (RESOURCES / "reviewer.md").read_text(encoding="utf-8")
        lines = [line.strip() for line in instructions.splitlines() if len(line.strip()) >= 30]
        self.assertTrue(lines)
        for line in lines:
            self.assertNotIn(line, packet)
        self.assertIs(captured["validate"], validate_review)
        self.assertEqual(captured["probe"], PROBE)  # The pre-reservation probe is reused, not repeated.
        self.preflight.assert_called_once()

    def test_alternation_six_changed_last_revision_stops_unreviewed(self):
        self.ready()
        seen = []
        def reviewer(provider, *args):
            seen.append(provider["id"])
            return review_value("changes_required")
        with patch(RUN_REVIEW, side_effect=reviewer) as call:
            for index in range(1, 7):
                reviewed = self.engine.review(self.run_id)
                self.assertEqual(reviewed["review_count"], index)
                self.assertEqual(reviewed["status"], "awaiting_revision")
                result = self.reflect(reviewed)
                self.assertEqual(result["status"], "exhausted" if index == 6 else "ready")
            with self.assertRaises(DevFlowError):
                self.engine.review(self.run_id)
            self.assertEqual(call.call_count, 6)
        self.assertEqual(seen, ["codex", "opencode"] * 3)
        self.assertFalse(result["final_plan_reviewed"])
        self.assertEqual(result["plan_version"], 6)
        resumed = self.resume("new-human-session")
        self.assertEqual(resumed["status"], "exhausted")
        self.assertEqual(resumed["review_count"], 6)
        self.assertFalse(self.engine.report(self.run_id)["converged"])

    def test_pass_before_change_does_not_approve_new_hash(self):
        self.ready()
        old = self.submit(review_value())
        changed = self.reflect(self.submit(review_value("changes_required")))
        self.assertNotEqual(changed["plan_hash"], old["plan_hash"])
        third = self.submit(review_value())
        self.assertEqual(third["status"], "ready")
        fourth = self.submit(review_value())
        self.assertEqual(fourth["status"], "completed")
        self.assertEqual(fourth["review_count"], 4)

    def test_failed_attempt_consumed_resume_explicit_and_alternating(self):
        self.ready()
        with patch(RUN_REVIEW, side_effect=RuntimeError("mock provider unavailable")):
            failed = self.engine.review(self.run_id)
        self.assertEqual(failed["status"], "paused_error")
        self.assertEqual(failed["review_count"], 1)
        self.assertEqual(failed["next_reviewer"], "opencode")
        self.assertIsNone(failed["inflight"])
        with patch(RUN_REVIEW) as no_call:
            with self.assertRaises(DevFlowError):
                self.engine.review(self.run_id)
            no_call.assert_not_called()
        resumed = self.resume("second-session")
        self.assertEqual(resumed["review_count"], 1)
        self.assertEqual(resumed["owner_session"], "second-session")
        with self.assertRaises(DevFlowError):
            self.engine.require_owner(self.run_id, self.session)
        with patch(RUN_REVIEW, return_value=review_value()) as call:
            next_state = self.engine.review(self.run_id)
        self.assertEqual(call.call_args.args[0]["id"], "opencode")
        self.assertEqual(next_state["review_count"], 2)
        self.assertEqual(next_state["status"], "ready")

    def test_reviewer_failure_keeps_other_passes_for_the_same_plan(self):
        initial = self.ready()
        outcomes = [review_value(), ReviewerError("malformed opencode reply"),
                    ReviewerError("malformed codex reply"), review_value()]
        with patch(RUN_REVIEW, side_effect=outcomes) as call:
            self.assertEqual(self.engine.review(self.run_id)["status"], "ready")
            self.assertEqual(self.engine.review(self.run_id)["status"], "paused_error")
            self.resume(self.session)
            self.assertEqual(self.engine.review(self.run_id)["status"], "paused_error")
            self.resume(self.session)
            final = self.engine.review(self.run_id)
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["review_count"], 4)
        self.assertEqual(final["plan_hash"], initial["plan_hash"])
        self.assertEqual([c.args[0]["id"] for c in call.call_args_list], ["codex", "opencode", "codex", "opencode"])
        for number in ("02", "03"):
            self.assertTrue((self.run / "reviews" / number / "error.json").is_file(), number)

    def test_ownership_moving_during_preflight_stops_the_old_owner_from_reviewing(self):
        self.ready()
        caller = Engine(self.project, session=self.session)

        def move_ownership(provider):
            self.resume("session-that-took-over")
            return PROBE

        with patch(PREFLIGHT, side_effect=move_ownership), patch(RUN_REVIEW) as call:
            with self.assertRaisesRegex(DevFlowError, "does not own"):
                caller.review(self.run_id)
        call.assert_not_called()
        after = self.engine.status(self.run_id)
        self.assertEqual((after["status"], after["review_count"], after["owner_session"]),
                         ("ready", 0, "session-that-took-over"))

    def test_reviewer_preflight_failure_does_not_consume_attempt(self):
        self.ready()
        before = read_json(self.run / "state.json")
        with patch(PREFLIGHT, side_effect=ReviewerError("codex CLI not found")) as probe, patch(RUN_REVIEW) as call:
            with self.assertRaisesRegex(ReviewerError, "not found"):
                self.engine.review(self.run_id)
        probe.assert_called_once()
        call.assert_not_called()
        self.assertEqual(read_json(self.run / "state.json"), before)
        self.assertFalse((self.run / "reviews").exists())
        after = self.engine.status(self.run_id)
        self.assertEqual((after["status"], after["review_count"], after["next_reviewer"]), ("ready", 0, "codex"))
        self.assertIsNone(after["inflight"])
        with patch(RUN_REVIEW, return_value=review_value()) as call:
            reviewed = self.engine.review(self.run_id)
        self.assertEqual(call.call_args.args[0]["id"], "codex")
        self.assertEqual(reviewed["review_count"], 1)

    def test_invalid_semantic_review_is_a_consumed_failure(self):
        self.ready()
        result = self.submit({"unexpected": "not a review"})
        self.assertEqual(result["status"], "paused_error")
        self.assertEqual(result["review_count"], 1)
        self.assertTrue((self.run / "reviews" / "01" / "error.json").exists())

    def test_sixth_failed_attempt_is_exhausted_not_retryable(self):
        self.ready()
        with patch(RUN_REVIEW, return_value=review_value("changes_required")):
            for _ in range(5):
                self.reflect(self.engine.review(self.run_id))
        with patch(RUN_REVIEW, side_effect=RuntimeError("sixth call failed")):
            final = self.engine.review(self.run_id)
        self.assertEqual(final["status"], "exhausted")
        self.assertEqual(final["review_count"], 6)
        self.assertFalse(final["final_plan_reviewed"])
        resumed = self.resume("recovery-session")
        self.assertEqual(resumed["review_count"], 6)
        self.assertEqual(resumed["status"], "exhausted")
        with patch(RUN_REVIEW) as call:
            with self.assertRaises(DevFlowError):
                self.engine.review(self.run_id)
            call.assert_not_called()

    def test_config_is_frozen_for_run_even_if_project_default_changes(self):
        self.ready()
        (self.project / "devflow.config.json").write_text('{"max_reviews":2}', encoding="utf-8")
        self.assertEqual(self.engine.status(self.run_id)["config"]["max_reviews"], 6)
        with patch(RUN_REVIEW, return_value=review_value("changes_required")):
            for _ in range(2):
                self.reflect(self.engine.review(self.run_id))
        self.assertEqual(self.engine.status(self.run_id)["status"], "ready")
        self.assertEqual(read_json(self.run / "config.json")["max_reviews"], 6)

    def test_interruption_is_not_refunded_and_requires_operator_recovery(self):
        self.ready()
        with patch(RUN_REVIEW, side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.engine.review(self.run_id)
        interrupted = self.engine.status(self.run_id)
        self.assertEqual(interrupted["status"], "reviewing")
        self.assertEqual(interrupted["inflight"]["number"], 1)
        with self.assertRaisesRegex(DevFlowError, "recover"):
            self.resume("new-session")
        self.assertEqual(self.engine.status(self.run_id)["owner_session"], self.session)
        with self.assertRaises(DevFlowError):
            self.engine.recover(self.run_id, confirmed=True, expected_review=2)
        recovered = self.engine.recover(self.run_id, confirmed=True, expected_review=1)
        self.assertEqual(recovered["status"], "paused_error")
        self.assertIsNone(recovered["inflight"])
        resumed = self.resume("new-session")
        self.assertEqual(resumed["review_count"], 1)
        self.assertEqual(resumed["next_reviewer"], "opencode")
        self.assertEqual(resumed["status"], "ready")

    def test_recover_clears_inflight_and_exhausts_a_used_budget(self):
        (self.project / "devflow.config.json").write_text('{"max_reviews":2}', encoding="utf-8")
        self.ready()
        self.reflect(self.submit(review_value("changes_required")))
        with patch(RUN_REVIEW, side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.engine.review(self.run_id)
        interrupted = self.engine.status(self.run_id)
        self.assertEqual((interrupted["status"], interrupted["review_count"]), ("reviewing", 2))
        self.assertEqual(interrupted["inflight"]["number"], 2)
        recovered = self.engine.recover(self.run_id, confirmed=True, expected_review=2)
        self.assertEqual(recovered["status"], "exhausted")
        self.assertIsNone(recovered["inflight"])
        self.assertEqual(recovered["review_count"], 2)  # The interrupted attempt stays consumed.
        self.assertIsNone(recovered["next_reviewer"])
        self.assertTrue((self.run / "reviews" / "02" / "recovery.json").is_file())
        with patch(RUN_REVIEW) as call:
            with self.assertRaises(DevFlowError):
                self.engine.review(self.run_id)
            call.assert_not_called()
        self.assertFalse(self.engine.report(self.run_id)["converged"])

    def test_stale_hash_and_missing_dispositions_do_not_commit(self):
        self.ready()
        state = self.submit(review_value("changes_required"))
        with self.assertRaises(DevFlowError):
            self.reflect(state, expected_hash="0" * 64)
        with self.assertRaises(DevFlowError):
            self.reflect(state, decisions=[])
        after = self.engine.status(self.run_id)
        self.assertEqual(after["status"], "awaiting_revision")
        self.assertEqual(after["plan_hash"], state["plan_hash"])
        self.assertFalse((self.run / "reviews" / "01" / "reflection.json").exists())

    def test_accepted_change_requires_changed_plan(self):
        self.ready()
        state = self.submit(review_value("changes_required"))
        with self.assertRaises(DevFlowError):
            self.reflect(state, change=False)

    def test_human_required_needs_exact_new_recorded_answer(self):
        self.ready()
        before_event_ids = read_json(self.run / "state.json")["event_ids"]
        state = self.submit(review_value("human_required", human=True))
        self.assertEqual(state["status"], "awaiting_human")
        self.assertEqual(state["human_events"], [])
        self.assertEqual(state["escalation"]["detail"], {"review_number": 1})
        resumed = self.resume("new-session")
        self.session = "new-session"
        self.assertEqual(resumed["status"], "awaiting_human")
        self.assertEqual(resumed["review_count"], 1)
        with self.assertRaises(DevFlowError):
            self.reflect(resumed)
        stale_answer = self.draft("resolution.json", {"answer": self.answer, "event_ids": [before_event_ids[-1]]})
        with self.assertRaises(DevFlowError):
            self.resolve(stale_answer)
        answer = "Expand scope only for this one agreed case."
        event_id = self.record(self.question(answer, tool_use_id="scope-answer"))
        actual = self.engine.status(self.run_id)["human_events"][-1]
        self.assertEqual(actual, {"event_id": event_id, "kind": "question", "answer_text": answer})
        paraphrase = self.draft("resolution.json", {"answer": "The human probably approved.", "event_ids": [event_id]})
        with self.assertRaises(DevFlowError):
            self.resolve(paraphrase)
        exact = self.draft("resolution.json", {"answer": actual["answer_text"], "event_ids": [actual["event_id"]]})
        resolved = self.resolve(exact)
        self.assertEqual(resolved["status"], "awaiting_revision")
        self.assertIsNone(resolved["escalation"])
        self.assertEqual(resolved["human_events"], [])
        self.assertEqual(self.reflect(resolved)["status"], "ready")

    def test_resume_into_new_session_invalidates_prior_answers_and_resume_prompt(self):
        self.ready()
        self.submit(review_value("human_required", human=True))
        early = self.record(self.question("Keep scope", tool_use_id="before-move"))
        self.assertEqual([e["event_id"] for e in self.engine.status(self.run_id)["human_events"]], [early])
        resumed = self.resume("new-session")
        event_ids = read_json(self.run / "state.json")["event_ids"]
        resume_event = event_ids[-1]
        self.assertEqual(read_json(self.run / "events" / (resume_event + ".json"))["payload"]["prompt"],
                         f"/devflow:resume {self.run_id}")
        self.assertEqual(resumed["escalation"]["boundary"], len(event_ids))
        self.assertEqual(resumed["escalation"]["session_id"], "new-session")
        self.assertEqual(resumed["human_events"], [])
        self.session = "new-session"
        for event_id, text in ((early, "Keep scope"), (resume_event, f"/devflow:resume {self.run_id}")):
            with self.subTest(event_id=event_id):
                draft = self.draft("answer.json", {"answer": text, "event_ids": [event_id]})
                with self.assertRaisesRegex(DevFlowError, "after the pause"):
                    self.resolve(draft)
        self.assertEqual(self.engine.status(self.run_id)["status"], "awaiting_human")
        current = self.record(self.question("Expand scope", tool_use_id="after-move"))
        self.assertEqual(self.engine.status(self.run_id)["human_events"],
                         [{"event_id": current, "kind": "question", "answer_text": "Expand scope"}])
        exact = self.draft("answer.json", {"answer": "Expand scope", "event_ids": [current]})
        self.assertEqual(self.resolve(exact)["status"], "awaiting_revision")

    def test_human_events_are_empty_without_a_pending_escalation(self):
        self.ready()
        self.record(self.question("Yes", tool_use_id="no-escalation"))
        state = self.engine.status(self.run_id)
        self.assertIsNone(state["escalation"])
        self.assertEqual(state["human_events"], [])
        self.assertEqual(router.status(self.project, None, self.run_id)["human_events"], [])

    def test_resolve_requires_the_owning_session(self):
        self.ready()
        self.submit(review_value("human_required", human=True))
        event_id = self.record(self.question("Expand scope"))
        exact = self.draft("answer.json", {"answer": "Expand scope", "event_ids": [event_id]})
        for session in ("another-session", None):
            with self.subTest(session=session), self.assertRaisesRegex(DevFlowError, "owns the run"):
                self.engine.resolve(self.run_id, exact, session)
        code, output = self.runner("--session", "another-session", "--run", self.run_id,
                                   "plan", "resolve", "--answer-file", str(exact))
        self.assertEqual(code, 2)
        self.assertEqual(output["error"], "DevFlowError")
        self.assertIn("does not own", output["message"])
        self.assertEqual(self.engine.status(self.run_id)["status"], "awaiting_human")
        code, output = self.runner("--session", self.session, "plan", "resolve", "--answer-file", str(exact))
        self.assertEqual(code, 0, output)
        self.assertEqual(output["status"], "awaiting_revision")

    def test_critical_rejection_requires_human_even_without_reviewer_flag(self):
        self.ready()
        state = self.submit(review_value("changes_required", severity="critical"))
        with self.assertRaises(DevFlowError):
            self.reflect(state, action="reject", change=False)
        reason = self.draft("pause.json", {"reason": "Critical advice conflicts with explicit policy.", "questions": ["Reject this finding?"]})
        paused = self.engine.pause(self.run_id, reason)
        self.assertEqual(paused["review_count"], 1)
        event_id = self.record(self.question("Reject; the existing policy is binding.",
                                             tool_use_id="critical-rejection-answer"))
        event = self.engine.status(self.run_id)["human_events"][-1]
        resolution = self.draft("answer.json", {"answer": event["answer_text"], "event_ids": [event_id]})
        resolved = self.resolve(resolution)
        self.assertEqual(self.reflect(resolved, action="reject", change=False)["status"], "ready")

    def test_empty_or_cancelled_structured_answers_cannot_resolve_escalation(self):
        self.ready()
        self.submit(review_value("human_required", human=True))
        invalid_responses = [
            None, "", "   ", [], {}, {"answers": {}}, {"answers": {"Q": ""}},
            {"answers": {"Q": []}}, {"answers": {"Q": [""]}},
            {"answers": {"Q": "yes"}, "is_error": True},
            {"answers": {"Q": "yes"}, "error": "failed"},
            {"answers": {"Q": "yes"}, "cancelled": True},
            {"answers": {"Q": "yes"}, "canceled": True},
            {"answers": {"Q": "yes"}, "aborted": True},
            [{"type": "text", "text": "  "}],
        ]
        for index, response in enumerate(invalid_responses):
            with self.subTest(response=response):
                event_id = self.record(self.question(response, tool_use_id=f"invalid-{index}"))
                self.assertNotIn(event_id, [e["event_id"] for e in self.engine.status(self.run_id)["human_events"]])
                forged = self.draft("resolution.json", {"answer": "Pretend yes", "event_ids": [event_id]})
                with self.assertRaises(DevFlowError):
                    self.resolve(forged)
        # A failed or cancelled question is recorded, but never as a candidate.
        failed = self.record(self.question({"answers": {"Q": "yes"}}, tool_use_id="failed-call", is_error=True))
        self.assertEqual(read_json(self.run / "events" / (failed + ".json"))["kind"], "question")
        self.assertEqual(self.engine.status(self.run_id)["human_events"], [])
        self.assertEqual(self.engine.status(self.run_id)["status"], "awaiting_human")
        self.assertEqual(self.engine.status(self.run_id)["review_count"], 1)

    def test_structured_actual_answer_can_resolve_using_reported_answer_text(self):
        self.ready()
        self.submit(review_value("human_required", human=True))
        response = {"questions": [{"question": "Q"}], "answers": {"Q": ["Accept one case", "Keep budget unchanged"]}}
        event_id = self.record(self.question(response, tool_use_id="actual-human-answer"))
        captured = self.engine.status(self.run_id)["human_events"][-1]
        self.assertEqual(captured["event_id"], event_id)
        self.assertEqual(json.loads(captured["answer_text"]), response)
        exact = self.draft("resolution.json", {"answer": captured["answer_text"], "event_ids": [event_id]})
        self.assertEqual(self.resolve(exact)["status"], "awaiting_revision")

    def test_immutable_artifact_tamper_blocks_dispatch_and_report(self):
        self.ready()
        (self.run / "request.json").write_text('{"request":"tampered"}', encoding="utf-8")
        with patch(RUN_REVIEW) as call:
            with self.assertRaisesRegex(DevFlowError, "Immutable artifact"):
                self.engine.review(self.run_id)
            call.assert_not_called()
        with self.assertRaisesRegex(DevFlowError, "Immutable artifact"):
            self.engine.report(self.run_id)
        self.assertEqual(self.engine.status(self.run_id)["review_count"], 0)

    def test_mutating_frozen_config_in_state_blocks_review(self):
        self.ready()
        state_path = self.run / "state.json"
        state = read_json(state_path)
        state["config"]["reviewers"] = state["config"]["reviewers"][:1]
        state_path.write_text(json.dumps(state), encoding="utf-8")
        with patch(RUN_REVIEW) as reviewer:
            with self.assertRaisesRegex(DevFlowError, "configuration changed"):
                self.engine.review(self.run_id)
        reviewer.assert_not_called()

    def test_ordinary_prompt_cannot_resolve_escalation(self):
        self.ready()
        self.submit(review_value("human_required", human=True))
        ordinary = self.record(self.event(prompt="Yes, continue."))
        self.assertEqual(self.engine.status(self.run_id)["human_events"], [])
        answer = self.draft("answer.json", {"answer": "Yes, continue.", "event_ids": [ordinary]})
        with self.assertRaisesRegex(DevFlowError, "AskUserQuestion"):
            self.resolve(answer)
        self.assertEqual(self.engine.status(self.run_id)["status"], "awaiting_human")

    def test_answer_filled_in_by_question_screen_can_resolve_escalation(self):
        # Claude Code's question screen writes the human's choice into
        # tool_input before PostToolUse; model prefills are denied at PreToolUse.
        self.ready()
        self.submit(review_value("human_required", human=True))
        answered = {"questions": [{"question": "Scope?"}], "answers": {"Scope?": "Yes"}, "annotations": {}}
        event_id = self.record(self.event(
            "PostToolUse", tool_name="AskUserQuestion", tool_use_id="screen-answer",
            tool_input=answered, tool_response=answered))
        captured = self.engine.status(self.run_id)["human_events"][-1]
        self.assertEqual(captured["event_id"], event_id)
        exact = self.draft("answer.json", {"answer": captured["answer_text"], "event_ids": [event_id]})
        self.assertEqual(self.resolve(exact)["status"], "awaiting_revision")

    def test_replayed_question_cannot_cross_a_later_escalation_boundary(self):
        self.ready()
        payload = self.event("PostToolUse", tool_name="AskUserQuestion", tool_use_id="unique-question",
            tool_input={"questions": [{"question": "Confirm original scope?"}]},
            tool_response={"answers": {"Confirm original scope?": "Yes"}})
        original_id = self.record(payload)
        self.submit(review_value("human_required", human=True))
        before = (self.run / "state.json").read_bytes()
        replayed_id = self.record(payload)
        self.assertEqual(replayed_id, original_id)
        self.assertEqual((self.run / "state.json").read_bytes(), before)
        self.assertEqual(self.engine.status(self.run_id)["human_events"], [])
        answer = self.draft("answer.json", {"answer": "Yes", "event_ids": [replayed_id]})
        with self.assertRaisesRegex(DevFlowError, "after the pause"):
            self.resolve(answer)

    def test_conflicting_question_replay_does_not_change_original_evidence(self):
        self.ready()
        payload = self.event("PostToolUse", tool_name="AskUserQuestion", tool_use_id="unique-question",
            tool_input={"questions": [{"question": "Q"}]}, tool_response="Original answer")
        identifier = self.record(payload)
        before = (self.run / "state.json").read_bytes()
        for change in ({"tool_response": "Different answer"}, {"tool_input": {"questions": [{"question": "Other Q"}]}}, {"is_error": True}):
            with self.subTest(change=change), self.assertRaisesRegex(DevFlowError, "replayed with different content"):
                self.hook(dict(payload, **change))
        self.assertEqual((self.run / "state.json").read_bytes(), before)
        self.assertEqual(read_json(self.run / "events" / (identifier + ".json"))["payload"]["tool_response"], "Original answer")

    def test_question_ids_are_scoped_to_owner_session_and_stale_payload_is_rejected(self):
        self.ready()
        old = self.event("PostToolUse", tool_name="AskUserQuestion", tool_use_id="same-local-id",
            tool_input={"questions": [{"question": "Q"}]}, tool_response="Old answer")
        original_id = self.record(old)
        self.submit(review_value("human_required", human=True))
        self.resume("new-owner")
        before = (self.run / "state.json").read_bytes()
        with self.assertRaisesRegex(DevFlowError, "stale session"):
            self.hook(old)
        with self.assertRaisesRegex(DevFlowError, "stale-session"):
            self.engine.store.append_event(self.run_id, old, "question")
        self.assertEqual((self.run / "state.json").read_bytes(), before)
        self.session = "new-owner"
        current = self.event("PostToolUse", tool_name="AskUserQuestion", tool_use_id="same-local-id",
            tool_input={"questions": [{"question": "Q"}]}, tool_response="New owner's answer")
        new_id = self.record(current)
        self.assertNotEqual(new_id, original_id)
        self.assertEqual(self.record(current), new_id)
        events = self.engine.status(self.run_id)["human_events"]
        self.assertEqual(events, [{"event_id": new_id, "kind": "question", "answer_text": "New owner's answer"}])

    def test_question_event_requires_identity_input_and_result_before_recording(self):
        self.ready()
        good = self.event("PostToolUse", tool_name="AskUserQuestion", tool_use_id="actual-id",
            tool_input={"questions": [{"question": "Q"}]}, tool_response="Yes")
        before = (self.run / "state.json").read_bytes()
        invalid = [dict(good, tool_use_id=value) for value in (None, "", "  ", "a" * 513)]
        invalid += [dict(good, tool_input=None), {key: value for key, value in good.items() if key != "tool_response"}]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaisesRegex(DevFlowError, "actual tool identity"):
                self.hook(payload)
        self.assertEqual((self.run / "state.json").read_bytes(), before)

    def test_state_lock_fails_safely_without_dispatch(self):
        self.ready()
        lock = self.run / "state.lock"
        engine = Engine(self.project, lock_wait=0.05)  # Do not wait for the runner's full lock timeout.
        with file_lock(lock), patch(RUN_REVIEW) as call:
            with self.assertRaisesRegex(DevFlowError, "locked"):
                engine.review(self.run_id)
            self.assertTrue(lock.exists())
            call.assert_not_called()
        self.assertFalse(lock.exists())
        self.assertEqual(self.engine.status(self.run_id)["review_count"], 0)

    def test_initial_human_answer_is_mandatory(self):
        self.start_interview(answer=False)
        with self.assertRaisesRegex(DevFlowError, "human answer"):
            self.seal()
        self.assertEqual(self.engine.status(self.run_id)["status"], "interview")

    def test_unsealed_interview_cannot_move_sessions(self):
        self.start_interview()
        before = (self.run / "state.json").read_bytes()
        with self.assertRaisesRegex(DevFlowError, "original Claude session"):
            self.resume("other-session")
        self.assertEqual((self.run / "state.json").read_bytes(), before)
        self.assertIsNone(self.bound_run("other-session"))
        self.assertEqual(self.bound_run(), self.run_id)

    def test_initial_unresolved_questions_prevent_sealing(self):
        self.start_interview()
        decisions = self.draft("decisions.json", {"summary": "Not ready.", "decisions": [], "constraints": [], "open_questions": ["Budget?"]})
        with self.assertRaisesRegex(DevFlowError, "Resolve initial human questions"):
            self.engine.seal(self.run_id, decisions)

    def test_maximum_seven_is_rejected_before_run_creation(self):
        (self.project / "devflow.config.json").write_text('{"max_reviews":7}', encoding="utf-8")
        with self.assertRaisesRegex(DevFlowError, "hard ceiling"):
            self.start_interview()
        self.assertEqual(list((self.project / ".devflow" / "runs" / "plan").glob("*")), [])

    def test_oversized_packet_does_not_consume_attempt(self):
        (self.project / "devflow.config.json").write_text('{"plan":{"max_packet_bytes":1024}}', encoding="utf-8")
        self.ready()
        with patch(RUN_REVIEW) as call:
            with self.assertRaisesRegex(DevFlowError, "packet exceeds"):
                self.engine.review(self.run_id)
            call.assert_not_called()
        self.assertEqual(self.engine.status(self.run_id)["review_count"], 0)

    def test_draft_traversal_and_external_file_rejected(self):
        self.start_interview()
        self.seal()
        outside = self.project / "source.md"
        outside.write_text("Do not use or edit me", encoding="utf-8")
        for path in (outside, self.draft_dir / ".." / "request.json", self.draft_dir / "../../../../../source.md"):
            with self.subTest(path=str(path)):
                with self.assertRaises(DevFlowError):
                    self.engine.plan(self.run_id, path)
        self.assertEqual(outside.read_text(encoding="utf-8"), "Do not use or edit me")

    def test_symlink_draft_input_rejected(self):
        self.start_interview()
        self.seal()
        outside = self.project / "source.md"
        outside.write_text("Original source", encoding="utf-8")
        link = self.draft_dir / "linked-plan.md"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks unavailable for this OS/account")
        with self.assertRaisesRegex(DevFlowError, "Symlink"):
            self.engine.plan(self.run_id, link)

    def test_terminal_guard_stays_until_next_real_human_prompt(self):
        self.ready()
        self.submit(review_value())
        self.submit(review_value())
        self.assertEqual(self.bound_run(), self.run_id)
        write = self.event("PreToolUse", tool_name="Write", tool_input={"file_path": str(self.project / "app.py"), "content": "unauthorized implementation"})
        denied = self.hook(write)
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        events_before = read_json(self.run / "state.json")["event_ids"]
        self.assertIsNone(self.hook(self.event(prompt="Now I have a separate request.")))
        self.assertIsNone(self.bound_run())
        self.assertEqual(router.status(self.project, self.session, None)["status"], "idle")
        # The prompt that released the finished run is not recorded in it.
        self.assertEqual(read_json(self.run / "state.json")["event_ids"], events_before)
        self.assertIsNone(self.hook(write))
        self.assertFalse((self.project / "app.py").exists())

    def test_terminal_resume_rebinds_owner_and_guard_without_resetting_budget(self):
        self.ready()
        self.submit(review_value())
        self.submit(review_value())
        self.session = "new-report-session"
        result = self.hook(self.event(prompt=f"/devflow:resume {self.run_id}"))
        self.assertIn("additionalContext", result["hookSpecificOutput"])
        state = self.engine.status(self.run_id)
        self.assertEqual(state["owner_session"], self.session)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["review_count"], 2)
        self.assertEqual(self.bound_run(), self.run_id)
        self.engine.require_owner(self.run_id, self.session)
        self.assertTrue(self.engine.report(self.run_id)["converged"])
        denied = self.hook(self.event("PreToolUse", tool_name="Write", tool_input={"file_path": str(self.project / "app.py"), "content": "not authorized"}))
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        with patch(RUN_REVIEW) as call:
            with self.assertRaises(DevFlowError):
                self.engine.review(self.run_id)
            call.assert_not_called()

    def test_no_second_active_run_and_no_double_seal(self):
        self.start_interview()
        with self.assertRaisesRegex(DevFlowError, "Finish or cancel"):
            self.hook(self.event(prompt="/devflow:plan a different request"))
        with self.assertRaisesRegex(DevFlowError, "unfinished plan run"):
            self.hook(self.event(prompt="/devflow:plan a different request", session="another-session"))
        self.assertEqual([path.name for path in self.run.parent.iterdir()], [self.run_id])
        self.assertEqual(self.bound_run(), self.run_id)
        self.seal()
        with self.assertRaises(DevFlowError):
            self.seal()

    def test_run_data_gitignored_without_modifying_project_gitignore(self):
        (self.project / ".gitignore").write_text("user-owned\n", encoding="utf-8")
        self.start_interview()
        self.assertEqual((self.project / ".gitignore").read_text(encoding="utf-8"), "user-owned\n")
        self.assertIn("*", (self.project / ".devflow" / ".gitignore").read_text(encoding="utf-8"))

    def test_unified_paths_do_not_create_or_import_legacy_runs(self):
        legacy = self.project / ("." + LEGACY) / "runs" / "legacy"
        legacy.mkdir(parents=True)
        (legacy / "state.json").write_text('{"legacy":true}', encoding="utf-8")
        (self.project / (LEGACY + ".config.json")).write_text('{"max_reviews":2}', encoding="utf-8")
        state = self.start_interview()
        self.assertEqual(self.run.parent, self.project / ".devflow" / "runs" / "plan")
        record = sessions.session_path(self.project, self.session)
        self.assertEqual(record.parent, self.project / ".devflow" / "sessions")
        self.assertTrue(record.is_file())
        self.assertEqual(state["flow"], "plan")
        self.assertEqual(state["config"]["max_reviews"], 6)
        self.assertEqual((legacy / "state.json").read_text(encoding="utf-8"), '{"legacy":true}')

    def test_publish_exact_approved_plan_with_issue_id_and_immutable_receipt(self):
        state = self.completed("/devflow:plan REVIEW-10のプランを作成して")
        self.assertEqual(state["publication"]["status"], "pending")
        self.assertFalse((self.project / "docs").exists())
        output = self.engine.publish(self.run_id)
        target = self.project / "docs" / "plans" / "REVIEW-10.md"
        self.assertEqual(output["publication"]["status"], "published")
        self.assertEqual(target.read_bytes(), Path(state["current_plan"]).read_bytes())
        receipt = (self.run / "publication.json").read_bytes()
        again = self.engine.publish(self.run_id)
        self.assertEqual(again["review_count"], 2)
        self.assertEqual((self.run / "publication.json").read_bytes(), receipt)
        report = self.engine.report(self.run_id)
        self.assertEqual(report["published_plan"], str(target))
        self.assertTrue(report["published_plan_matches_at_report"])
        self.assertIsNone(report["warning"])

    def test_publish_without_issue_uses_run_id(self):
        self.completed()
        result = self.engine.publish(self.run_id)
        self.assertEqual(result["publication"]["path"], f"docs/plans/plan-{self.run_id}.md")

    def test_publish_before_second_approval_is_refused(self):
        self.ready()
        self.submit(review_value())
        with self.assertRaisesRegex(DevFlowError, "both reviewers"):
            self.engine.publish(self.run_id)
        self.assertFalse((self.project / "docs").exists())
        self.assertEqual(self.engine.status(self.run_id)["review_count"], 1)

    def test_publish_exhausted_last_revision_is_refused(self):
        (self.project / "devflow.config.json").write_text('{"max_reviews":2}', encoding="utf-8")
        self.ready()
        self.reflect(self.submit(review_value("changes_required")))
        self.reflect(self.submit(review_value("changes_required")))
        with self.assertRaisesRegex(DevFlowError, "both reviewers"):
            self.engine.publish(self.run_id)
        self.assertFalse((self.project / "docs").exists())

    def test_publish_collision_preserves_old_plan_and_uses_run_suffix(self):
        state = self.completed("/devflow:plan REVIEW-10 を計画")
        target = self.project / "docs" / "plans" / "REVIEW-10.md"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"Human-owned existing plan\n")
        result = self.engine.publish(self.run_id)
        self.assertEqual(target.read_bytes(), b"Human-owned existing plan\n")
        self.assertEqual(result["publication"]["path"], f"docs/plans/REVIEW-10-{self.run_id}.md")
        self.assertEqual((self.project / result["publication"]["path"]).read_bytes(), Path(state["current_plan"]).read_bytes())

    def test_identical_existing_file_is_not_adopted_without_prior_intent(self):
        state = self.completed("/devflow:plan REVIEW-10")
        target = self.project / "docs" / "plans" / "REVIEW-10.md"
        target.parent.mkdir(parents=True)
        target.write_bytes(Path(state["current_plan"]).read_bytes())
        result = self.engine.publish(self.run_id)
        self.assertNotEqual(result["publication"]["path"], "docs/plans/REVIEW-10.md")

    def test_publish_both_colliding_filenames_stop_without_overwriting(self):
        self.completed("/devflow:plan REVIEW-10")
        directory = self.project / "docs" / "plans"
        directory.mkdir(parents=True)
        for name in ("REVIEW-10.md", f"REVIEW-10-{self.run_id}.md"):
            (directory / name).write_bytes(b"existing\n")
        with self.assertRaisesRegex(DevFlowError, "already exist"):
            self.engine.publish(self.run_id)
        state = self.engine.status(self.run_id)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["publication"]["status"], "error")
        self.assertFalse((self.run / "publication.json").exists())
        for path in directory.iterdir():
            self.assertEqual(path.read_bytes(), b"existing\n")

    def test_publish_symlink_directory_rejected(self):
        self.completed()
        outside = self.project / "outside"
        outside.mkdir()
        try:
            (self.project / "docs").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks unavailable")
        with self.assertRaisesRegex(DevFlowError, "Symlink"):
            self.engine.publish(self.run_id)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(self.engine.status(self.run_id)["publication"]["status"], "error")

    def test_publish_disabled_configuration_never_creates_docs(self):
        (self.project / "devflow.config.json").write_text('{"plan":{"publish":{"enabled":false}}}', encoding="utf-8")
        self.completed()
        state = self.engine.publish(self.run_id)
        self.assertEqual(state["publication"]["status"], "disabled")
        self.assertFalse((self.project / "docs").exists())
        self.assertIsNone(self.engine.report(self.run_id)["published_plan"])

    def test_publish_io_error_records_failure_and_can_retry_without_review(self):
        state = self.completed()
        original = engine_module.write_new
        def fail_export(path, data):
            if path.suffix == ".md" and path.is_relative_to(self.project / "docs"):
                raise OSError("Synthetic unavailable filesystem")
            return original(path, data)
        with patch("devflow_core.plan.engine.write_new", side_effect=fail_export):
            with self.assertRaisesRegex(DevFlowError, "publication failed"):
                self.engine.publish(self.run_id)
        failed = self.engine.status(self.run_id)
        self.assertEqual(failed["status"], "completed")
        self.assertEqual(failed["publication"]["status"], "error")
        self.assertIsNone(self.engine.report(self.run_id)["published_plan"])
        with patch(RUN_REVIEW) as reviewers:
            recovered = self.engine.publish(self.run_id)
        reviewers.assert_not_called()
        self.assertEqual(recovered["review_count"], state["review_count"])
        self.assertEqual(recovered["publication"]["status"], "published")

    def test_publish_crash_after_export_reuses_intent_not_another_name(self):
        self.completed("/devflow:plan REVIEW-10")
        original = self.engine._artifact
        def crash_before_receipt(state, relative, data):
            if relative == "publication.json":
                raise InjectedCrash("Export exists; receipt not committed")
            return original(state, relative, data)
        with patch.object(self.engine, "_artifact", side_effect=crash_before_receipt):
            with self.assertRaises(InjectedCrash):
                self.engine.publish(self.run_id)
        target = self.project / "docs" / "plans" / "REVIEW-10.md"
        before = target.read_bytes()
        self.assertEqual(self.engine.status(self.run_id)["publication"]["status"], "publishing")
        recovered = Engine(self.project).publish(self.run_id)
        self.assertEqual(recovered["publication"]["path"], "docs/plans/REVIEW-10.md")
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(len(list(target.parent.iterdir())), 1)

    def test_removed_or_changed_published_plan_never_silently_overwritten(self):
        self.completed("/devflow:plan REVIEW-10")
        self.engine.publish(self.run_id)
        target = self.project / "docs" / "plans" / "REVIEW-10.md"
        target.write_bytes(b"Human update after publication\n")
        report = self.engine.report(self.run_id)
        self.assertFalse(report["published_plan_matches_at_report"])
        with self.assertRaisesRegex(DevFlowError, "not overwritten"):
            self.engine.publish(self.run_id)
        self.assertEqual(target.read_bytes(), b"Human update after publication\n")
        target.unlink()
        for _ in range(2):
            with self.assertRaisesRegex(DevFlowError, "not silently recreated"):
                self.engine.publish(self.run_id)
            self.assertFalse(target.exists())

    def test_publish_configuration_directory_is_frozen(self):
        (self.project / "devflow.config.json").write_text('{"plan":{"publish":{"directory":"design/plans"}}}', encoding="utf-8")
        self.completed("/devflow:plan REVIEW-10")
        (self.project / "devflow.config.json").write_text('{"plan":{"publish":{"directory":"elsewhere"}}}', encoding="utf-8")
        result = self.engine.publish(self.run_id)
        self.assertEqual(result["publication"]["path"], "design/plans/REVIEW-10.md")

    def test_unsafe_publish_directory_rejected_before_model_calls(self):
        for path in ("../outside", "/absolute", "C:/absolute", "docs/../src", ".git/hooks", ".devflow/runs", "docs/CON", "docs/has space", "docs\\plans"):
            (self.project / "devflow.config.json").write_text(json.dumps({"plan":{"publish":{"directory":path}}}), encoding="utf-8")
            with self.subTest(path=path), self.assertRaisesRegex(DevFlowError, "publish.directory"):
                self.start_interview()

    # -- interrupted transactions: display reads never repair; resume completes them ------
    def test_seal_partial_commit_recovers_frozen_capture_without_recapturing(self):
        self.start_interview()
        original_atomic = runs_module.atomic_json
        def crash_state_commit(path, value):
            if path == self.run / "state.json":
                raise InjectedCrash("after interview artifacts, before state commit")
            return original_atomic(path, value)
        with patch("devflow_core.common.runs.atomic_json", side_effect=crash_state_commit):
            with self.assertRaises(InjectedCrash):
                self.seal()
        self.assertTrue((self.run / "transaction.json").is_file())
        frozen = (self.run / "interview.json").read_bytes()
        self.assertEqual(read_json(self.run / "state.json")["status"], "interview")
        # A display read reports the pending transaction without replaying it.
        self.assertEqual(self.engine.status(self.run_id)["status"], "interview")
        self.assertTrue(router.status(self.project, None, self.run_id)["recovery_required"])
        self.assertTrue((self.run / "transaction.json").is_file())
        # Recovery must replay the captured bytes, not recapture a newer or
        # now-incompatible conversation or changed draft decision file.
        with self.transcript.open("a", encoding="utf-8") as stream:
            stream.write('{"type":"unsupported-new-event"}\n')
        self.draft("decisions.json", {"summary": "changed after crash", "decisions": [], "constraints": [], "open_questions": ["new"]})
        with patch("devflow_core.plan.engine.capture_interview", side_effect=AssertionError("must not recapture")) as capture, patch(RUN_REVIEW) as provider:
            self.resume(self.session)
            recovered = Engine(self.project).status(self.run_id)
        self.assertEqual(recovered["status"], "planning")
        self.assertEqual((self.run / "interview.json").read_bytes(), frozen)
        self.assertEqual(read_json(self.run / "decisions.json")["summary"], "Human confirmed the downtime limit.")
        self.assertFalse((self.run / "transaction.json").exists())
        capture.assert_not_called()
        provider.assert_not_called()

    def test_apply_artifacts_before_state_commit_recover_exactly_once(self):
        self.ready()
        state = self.submit(review_value("changes_required"))
        base_revision = state["state_revision"]
        original_atomic = runs_module.atomic_json
        def crash_state_commit(path, value):
            if path == self.run / "state.json":
                raise InjectedCrash("after plan and ledger files")
            return original_atomic(path, value)
        with patch("devflow_core.common.runs.atomic_json", side_effect=crash_state_commit):
            with self.assertRaises(InjectedCrash):
                self.reflect(state)
        plan = self.run / "plans" / "plan-v001.md"
        ledger = self.run / "reviews" / "01" / "reflection.json"
        plan_bytes, ledger_bytes = plan.read_bytes(), ledger.read_bytes()
        self.assertEqual(read_json(self.run / "state.json")["state_revision"], base_revision)
        self.draft("revised.md", "Do not substitute this changed draft during recovery.")
        with patch(RUN_REVIEW) as provider:
            self.resume(self.session)
            recovered = Engine(self.project).status(self.run_id)
            repeated = self.engine.status(self.run_id)
        self.assertEqual(recovered["status"], "ready")
        self.assertEqual(recovered["review_count"], 1)
        self.assertEqual(recovered["plan_version"], 1)
        # One revision for the replayed commit, one for recording the resume request.
        self.assertEqual(recovered["state_revision"], base_revision + 2)
        self.assertEqual(repeated["state_revision"], recovered["state_revision"])
        self.assertEqual(plan.read_bytes(), plan_bytes)
        self.assertEqual(ledger.read_bytes(), ledger_bytes)
        self.assertEqual(len(list((self.run / "plans").glob("*.md"))), 2)
        self.assertFalse((self.run / "transaction.json").exists())
        self.engine.report(self.run_id)  # All immutable hashes remain consistent.
        provider.assert_not_called()

    def test_partial_artifact_materialization_reuses_identical_existing_plan(self):
        self.ready()
        state = self.submit(review_value("changes_required"))
        ledger = self.run / "reviews" / "01" / "reflection.json"
        original_write = runs_module.write_new
        def crash_on_ledger(path, data):
            if path == ledger:
                raise InjectedCrash("first artifact written; second not written")
            return original_write(path, data)
        with patch("devflow_core.common.runs.write_new", side_effect=crash_on_ledger):
            with self.assertRaises(InjectedCrash):
                self.reflect(state)
        plan = self.run / "plans" / "plan-v001.md"
        self.assertTrue(plan.is_file())
        self.assertFalse(ledger.exists())
        original_plan = plan.read_bytes()
        with patch("devflow_core.common.runs.write_new", wraps=original_write) as writes:
            self.resume(self.session)
        recovered = self.engine.status(self.run_id)
        self.assertEqual(recovered["status"], "ready")
        self.assertEqual(plan.read_bytes(), original_plan)
        self.assertTrue(ledger.is_file())
        # Only the missing ledger is materialized (plus the resume request's own event record).
        written = [c.args[0] for c in writes.call_args_list]
        self.assertEqual([path for path in written if path.parent.name != "events"], [ledger])

    def test_committed_state_with_unremoved_journal_is_idempotent(self):
        self.start_interview()
        self.seal()
        journal = self.run / "transaction.json"
        original_unlink = Path.unlink
        def crash_unlink(path, *args, **kwargs):
            if path == journal:
                raise InjectedCrash("state committed; journal cleanup interrupted")
            return original_unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", new=crash_unlink):
            with self.assertRaises(InjectedCrash):
                self.engine.plan(self.run_id, self.draft("plan.md", "# Frozen original plan\n"))
        committed = read_json(self.run / "state.json")
        self.assertEqual(committed["status"], "ready")
        self.assertTrue(journal.exists())
        self.engine.status(self.run_id)
        self.assertTrue(journal.exists())  # Display reads never repair.
        self.resume(self.session)
        recovered = self.engine.status(self.run_id)
        # Replaying the committed journal adds nothing; only the resume request is a new revision.
        self.assertEqual(recovered["state_revision"], committed["state_revision"] + 1)
        self.assertEqual(recovered["plan_hash"], committed["plan_hash"])
        self.assertEqual(recovered["plan_version"], 0)
        self.assertFalse(journal.exists())
        self.assertEqual(len(list((self.run / "plans").glob("*.md"))), 1)

    def test_reserved_attempt_wal_recovery_never_redispatches_provider(self):
        self.ready()
        original_finish = self.engine.store._finish_transaction
        def crash_after_journal(run_id):
            if (self.run / "transaction.json").exists():
                raise InjectedCrash("journal durable, no files committed")
            return original_finish(run_id)
        with patch.object(self.engine.store, "_finish_transaction", side_effect=crash_after_journal), patch(RUN_REVIEW) as provider:
            with self.assertRaises(InjectedCrash):
                self.engine.review(self.run_id)
            provider.assert_not_called()
        self.assertEqual(read_json(self.run / "state.json")["review_count"], 0)
        self.assertEqual(read_json(self.run / "transaction.json")["state"]["review_count"], 1)
        self.assertTrue(router.status(self.project, None, self.run_id)["recovery_required"])
        with patch(RUN_REVIEW) as provider:
            # The locked load inside review completes the reservation, so the
            # attempt stays consumed and is never dispatched a second time.
            with self.assertRaises(DevFlowError):
                Engine(self.project).review(self.run_id)
            recovered = self.engine.status(self.run_id)
            self.assertEqual(recovered["review_count"], 1)
            self.assertEqual(recovered["status"], "reviewing")
            self.assertFalse((self.run / "transaction.json").exists())
            with self.assertRaises(DevFlowError):
                self.engine.review(self.run_id)
            with self.assertRaises(DevFlowError):
                self.resume(self.session)
            provider.assert_not_called()
        self.engine.recover(self.run_id, confirmed=True, expected_review=1)
        self.resume(self.session)
        with patch(RUN_REVIEW, return_value=review_value()) as provider:
            next_review = self.engine.review(self.run_id)
        self.assertEqual(next_review["review_count"], 2)
        self.assertEqual(provider.call_args.args[0]["id"], "opencode")

    def test_prejournal_failure_reserves_nothing_and_can_retry_safe_empty_round_directory(self):
        self.ready()
        original_atomic = runs_module.atomic_json
        def crash_before_journal(path, value):
            if path == self.run / "transaction.json":
                raise InjectedCrash("journal never committed")
            return original_atomic(path, value)
        with patch("devflow_core.common.runs.atomic_json", side_effect=crash_before_journal), patch(RUN_REVIEW) as provider:
            with self.assertRaises(InjectedCrash):
                self.engine.review(self.run_id)
            provider.assert_not_called()
        self.assertFalse((self.run / "transaction.json").exists())
        self.assertFalse((self.run / "reviews" / "01" / "packet.md").exists())
        unchanged = self.engine.status(self.run_id)
        self.assertEqual(unchanged["review_count"], 0)
        self.assertEqual(unchanged["status"], "ready")
        self.assertEqual(self.submit(review_value())["review_count"], 1)

    # -- late results after cancel/recover -----------------------------------------------
    def test_late_failure_cannot_demote_new_inflight_review(self):
        self.ready()
        newer_state = {}
        calls = []
        def reviewer(provider, *args):
            calls.append(provider["id"])
            if len(calls) == 2:
                # A separate newer dispatch is now durably reserved. It remains
                # in-flight while the obsolete first invocation fails late.
                raise KeyboardInterrupt
            self.engine.recover(self.run_id, confirmed=True, expected_review=1)
            self.resume(self.session)
            try:
                self.engine.review(self.run_id)
            except KeyboardInterrupt:
                pass
            newer_state.update(read_json(self.run / "state.json"))
            raise RuntimeError("obsolete first reviewer failed late")
        with patch(RUN_REVIEW, side_effect=reviewer):
            with self.assertRaisesRegex(DevFlowError, "stale review"):
                self.engine.review(self.run_id)
        self.assertEqual(calls, ["codex", "opencode"])
        self.assertEqual(read_json(self.run / "state.json"), newer_state)
        self.assertEqual(newer_state["status"], "reviewing")
        self.assertEqual(newer_state["review_count"], 2)
        self.assertEqual(newer_state["inflight"]["number"], 2)
        late = list((self.run / "late").glob("review-001-*.json"))
        self.assertEqual(len(late), 1)
        value = read_json(late[0])
        self.assertEqual(value["reservation"]["number"], 1)
        self.assertIsNone(value["result"])
        self.assertIn("failed late", value["error"])
        self.assertFalse((self.run / "reviews" / "01" / "error.json").exists())
        self.assertFalse((self.run / "reviews" / "02" / "error.json").exists())

    def test_recover_keeps_the_base_signature(self):
        self.ready()
        with self.engine.store.lock(self.run_id):
            state = self.engine.store.load(self.run_id)
            state["review_count"] = 1
            self.engine.store.reserve(state, "reviewing", "review", 1, state["plan_hash"], "codex")
        with self.assertRaisesRegex(DevFlowError, "exact interrupted review number"):
            self.engine.recover(self.run_id, True)  # Positional confirmation, as in the base class.
        self.assertEqual(self.engine.recover(self.run_id, True, expected_review=1)["status"], "paused_error")

    def test_recover_compares_the_expected_review_with_the_locked_state(self):
        self.ready()
        with self.engine.store.lock(self.run_id):
            state = self.engine.store.load(self.run_id)
            self.engine.store.reserve(state, "reviewing", "review", 2, state["plan_hash"], "opencode")
            state = self.engine.store.load(self.run_id)
            state["review_count"] = 2
            self.engine.store.save(state)
        stale = dict(self.engine.store.read(self.run_id), review_count=1)
        with patch.object(self.engine.store, "read", return_value=stale):
            with self.assertRaisesRegex(DevFlowError, "exact interrupted review number"):
                self.engine.recover(self.run_id, confirmed=True, expected_review=1)
        after = read_json(self.run / "state.json")
        self.assertEqual((after["status"], after["inflight"]["number"]), ("reviewing", 2))

    def test_stale_success_after_recover_is_kept_late_without_changing_state(self):
        self.ready()
        after_recover = {}
        def reviewer(provider, *args):
            # A human confirmed the process stopped, but its result still arrives.
            self.engine.recover(self.run_id, confirmed=True, expected_review=1)
            after_recover.update(read_json(self.run / "state.json"))
            return review_value()
        with patch(RUN_REVIEW, side_effect=reviewer):
            with self.assertRaisesRegex(DevFlowError, "stale review"):
                self.engine.review(self.run_id)
        state = read_json(self.run / "state.json")
        self.assertEqual(state, after_recover)
        self.assertEqual(state["status"], "paused_error")
        self.assertIsNone(state["inflight"])
        self.assertEqual(state["passes"], [])
        self.assertIsNone(state["last_review"])
        self.assertFalse((self.run / "reviews" / "01" / "review.json").exists())
        late = list((self.run / "late").glob("review-001-*.json"))
        self.assertEqual(len(late), 1)
        value = read_json(late[0])
        self.assertEqual(value["result"], review_value())
        self.assertIsNone(value["error"])
        self.assertEqual(value["reservation"]["reviewer_id"], "codex")

    def test_stale_owner_cannot_record_human_evidence_after_handover(self):
        self.ready()
        old_payload = self.event(prompt="Obsolete session says yes.")
        self.resume("new-owner-session")
        before = read_json(self.run / "state.json")
        with self.assertRaisesRegex(DevFlowError, "owned by another session"):
            self.hook(old_payload)
        with self.assertRaisesRegex(DevFlowError, "stale-session"):
            self.engine.store.append_event(self.run_id, old_payload, "prompt")
        self.assertEqual(read_json(self.run / "state.json"), before)
        self.session = "new-owner-session"
        current = self.record(self.event(prompt="Current session decision."))
        after = read_json(self.run / "state.json")
        self.assertEqual(len(after["event_ids"]), len(before["event_ids"]) + 1)
        self.assertEqual(after["event_ids"][-1], current)


if __name__ == "__main__":
    unittest.main()
