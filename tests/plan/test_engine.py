"""Offline state-machine integration: real files/hooks, mocked reviewer calls."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest.mock import patch

from devflow_core import cli, router
from devflow_core.common import paths, sessions
from devflow_core.common import runs as runs_module
from devflow_core.common.errors import DevFlowError, ReviewerError
from devflow_core.common.review import REVIEW_LIMIT_REASON
from devflow_core.common.storage import atomic_write_json, file_lock, read_json
from devflow_core.plan import engine as engine_module
from devflow_core.plan import config as config_module
from devflow_core.plan.config import FILENAME_DEFAULT, load_config, publish_name, safe_name
from devflow_core.plan.engine import Engine
from devflow_core.plan.review import validate_review
from devflow_core.workflows import WORKFLOWS
from tests.common.test_common import limit_question

PLUGIN_ROOT = paths.PLUGIN_ROOT
RESOURCES = WORKFLOWS["plan"].resource_dir
RUN_REVIEW = "devflow_core.plan.engine.run_review"
PREFLIGHT = "devflow_core.plan.engine.preflight"
# What a successful local CLI probe returns; reviews never call a real CLI here.
PROBE = {"kind": "fixture", "executable": "/fixture/reviewer", "cli_version": "fixture 1.0.0",
         "requested_model": "fixture", "requested_effort": "high", "effective_model_attested": False}
LEGACY = "plan" + "flow"  # A pre-DevFlow name, assembled so no tracked file spells it.



def raise_unwritable(kind):
    """The real reader's error for CLI output holding text a record cannot hold (an unpaired surrogate)."""
    from devflow_core.common import reviewers
    if kind == "answer":
        reviewers.json_value('{"summary": "bad \\ud800"}')
    elif kind == "codex turn failure":
        reviewers.parse_codex_events([json.dumps({"type": "thread.started", "thread_id": "t"}),
                                      json.dumps({"type": "turn.failed", "error": {"message": "boom \ud800"}})])
    elif kind == "opencode error":
        reviewers.parse_opencode_events([json.dumps({"type": "error", "sessionID": "s", "error": {"message": "boom \ud800"}})])
    elif kind == "duplicate key":
        reviewers.json_value('{"\\ud800": 1, "\\ud800": 2}')
    raise AssertionError(f"{kind} did not raise")


#: Each kind, and what its recorded error text holds once escaped.
UNWRITABLE = {"answer": "Reviewer answer contains text that cannot be saved (an unpaired surrogate)",
              "codex turn failure": "Codex reported that the review turn failed: boom \\ud800",
              "opencode error": "OpenCode reported an error: boom \\ud800",
              "duplicate key": "Duplicate JSON key: \\ud800"}


def unwritable_activity():
    """What the real Codex reader counts for a tool and an event whose names a record cannot hold."""
    from devflow_core.common import reviewers
    return reviewers.parse_codex_events([
        json.dumps({"type": "thread.started", "thread_id": "t"}),
        json.dumps({"type": "item.completed", "item": {"id": "1", "type": "\ud800tool"}}),
        json.dumps({"type": "\udfffkind"}), json.dumps({"type": "turn.completed", "usage": {}})])

class InjectedCrash(RuntimeError):
    """A deterministic crash boundary, not an actual killed process."""


ACTIVITY = {"tool_call_count": 3, "tool_calls": {"read": 2, "websearch": 1}, "other_events": {}}


def adapter_result(value):
    """What run_review returns: the validated review and what the reviewer did."""
    return {"review": value, "activity": dict(ACTIVITY)}


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
        self.engine = Engine(self.project, host="claude")
        self.run_id = None
        # The reviewer CLI check happens before every reservation; it is a
        # local process probe, so tests replace it with a successful result.
        preflight = patch(PREFLIGHT, return_value=PROBE)
        self.preflight = preflight.start()
        self.addCleanup(preflight.stop)

    # -- hooks and runner -----------------------------------------------------------------
    def event(self, name="UserPromptSubmit", session=None, **extra):
        return {"host": "claude", "hook_event_name": name, "cwd": str(self.project), "session_id": session or self.session,
                "transcript_path": str(self.transcript), **extra}

    def question(self, response, tool_use_id="question-1", session=None, **extra):
        return self.event("PostToolUse", session=session, tool_name="AskUserQuestion", tool_use_id=tool_use_id,
                          tool_input={"questions": [{"question": "Scope?"}]}, tool_response=response, **extra)

    def hook(self, payload):
        return router.handle_hook(payload, PLUGIN_ROOT, host="claude")

    def ask(self, payload):
        """The PreToolUse call before an AskUserQuestion result: the question as
        Claude sent it, before the question screen fills in the human's answers."""
        question = {key: value for key, value in payload["tool_input"].items()
                    if key not in sessions.SCREEN_FILLED_QUESTION_FIELDS}
        pre = {key: value for key, value in payload.items() if key not in {"tool_response", "is_error"}}
        self.assertIsNone(self.hook(dict(pre, hook_event_name="PreToolUse", tool_input=question)))

    def record(self, payload):
        """Send a human event through the router and return the event ID it reports.

        An AskUserQuestion result is preceded by its PreToolUse call, as in Claude Code."""
        if payload["hook_event_name"] == "PostToolUse" and payload.get("tool_name") == "AskUserQuestion":
            self.ask(payload)
        context = self.hook(payload)["hookSpecificOutput"]["additionalContext"]
        return re.search(r'(?:as event |"recorded_event_id": ")([a-f0-9]{32})', context).group(1)

    def assertNotAccepted(self, result, command, reason):
        """A DevFlow command the hook did not accept: a context message, never an exception."""
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn(f"DevFlow did not accept /devflow:{command}", context)
        self.assertIn(reason, context)

    def assertUnrecorded(self, result, reason):
        """A human event DevFlow could not record goes through with a warning."""
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("could not read or record", context)
        self.assertIn("was not recorded", context)
        self.assertIn(reason, context)

    def resume_prompt(self, session):
        return self.hook(self.event(prompt=f"/devflow:resume {self.run_id}", session=session))

    def resume(self, session):
        """``/devflow:resume RUN`` from ``session``; the hook performs the whole move."""
        result = self.resume_prompt(session)
        self.assertNotIn("did not accept", result["hookSpecificOutput"]["additionalContext"])
        return self.engine.status(self.run_id)

    def bound_run(self, session=None):
        record = sessions.read_record(self.project, session or self.session, WORKFLOWS, host="claude")
        return record["run"]["run_id"] if record and record["run"] else None

    def runner(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--host", "claude", "--project", str(self.project), *args])
        return code, json.loads(out.getvalue())

    def resolve(self, answer_file, session=None):
        return self.engine.resolve(self.run_id, answer_file, session or self.session)

    def limit_answer(self, text):
        """The human's answer to the pending question on the question screen: its recorded event."""
        tool_use_id = f"limit-{len(self.engine.status(self.run_id)['resolved_escalations'])}"
        event_id = self.record(self.question(text, tool_use_id=tool_use_id))
        return next(e for e in self.engine.status(self.run_id)["human_events"] if e["event_id"] == event_id)

    def at_limit(self, decision):
        """Answer the pending review limit question and resolve it with ``decision``."""
        actual = self.limit_answer({"continue": "Continue with two more reviews.", "stop": "Stop here.",
                                    "accept": "Accept and finish."}.get(decision, "Stop here."))
        return self.resolve(self.draft("limit-answer.json", {"answer": actual["answer_text"],
                                                            "event_ids": [actual["event_id"]], "decision": decision}))

    def ask_at_limit(self):
        """A review request at the limit: no reviewer CLI is probed or started."""
        self.preflight.reset_mock()
        with patch(RUN_REVIEW) as call:
            asked = self.engine.review(self.run_id)
            call.assert_not_called()
        self.preflight.assert_not_called()
        return asked

    # -- workflow steps -------------------------------------------------------------------
    def append(self, role, content):
        record = {"type": role, "message": {"role": role, "content": content}}
        with self.transcript.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    def start_interview(self, answer=True, request=None):
        self.raw_request = request or "/devflow:plan 実装せず、移行計画を作る。\nKeep exact wording."
        result = self.hook(self.event(prompt=self.raw_request))
        self.assertNotIn("did not accept", result["hookSpecificOutput"]["additionalContext"])
        self.run_id = router.status(self.project, self.session, None, host="claude")["run"]["run_id"]
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
        def reviewer(prepared, validate):
            work_dir, cwd, probe = prepared.work_dir, prepared.cwd, prepared.probe
            return adapter_result(validate(value))
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
        with patch(RUN_REVIEW, side_effect=[adapter_result(review_value()), adapter_result(review_value())]) as call:
            first = self.engine.review(self.run_id)
            second = self.engine.review(self.run_id)
        self.assertEqual(first["status"], "ready")
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["review_count"], 2)
        self.assertEqual(second["plan_hash"], initial["plan_hash"])
        self.assertTrue(second["final_plan_reviewed"])
        self.assertIsNone(second["inflight"])
        self.assertEqual([c.args[0].config["id"] for c in call.call_args_list], ["codex", "opencode"])
        self.assertTrue(self.engine.report(self.run_id)["converged"])
        self.assertTrue((self.run / "reviews" / "01" / "reflection.json").is_file())

    def test_packet_contains_raw_request_full_interview_and_plan(self):
        self.append("user", "PREVIOUS SESSION SECRET")
        initial = self.ready()
        captured = {}
        def reviewer(prepared, validate):
            work_dir, cwd, probe = prepared.work_dir, prepared.cwd, prepared.probe
            captured["packet"] = prepared.packet.decode("utf-8")
            captured["work_dir"] = work_dir
            captured["cwd"] = cwd
            return adapter_result(review_value())
        with patch(RUN_REVIEW, side_effect=reviewer):
            reviewed = self.engine.review(self.run_id)
        packet = captured["packet"]
        payload = json.loads(packet)
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["original_request"]["raw_prompt"], self.raw_request)
        self.assertEqual(payload["interview_verbatim"]["messages"][-1]["content"], self.answer)
        self.assertEqual(payload["decisions_and_constraints"]["summary"], read_json(self.run / "decisions.json")["summary"])
        self.assertEqual(payload["plan_sha256"], initial["plan_hash"])
        self.assertEqual(payload["revision_history"], [])
        self.assertEqual(payload["previous_reviews"], [])
        self.assertNotIn("PREVIOUS SESSION SECRET", packet)
        # The reviewer reads the project itself: it starts there and the packet names it.
        self.assertEqual(payload["project"], str(self.project))
        self.assertEqual(captured["cwd"], self.project)
        self.assertNotIn("repository_context", payload)
        self.assertNotIn("source", payload)
        # Its input, logs and raw output go to the process directory, as in implement;
        # reviews/NN keeps the run's own records.
        self.assertEqual(captured["work_dir"], self.run / "review-processes" / "01")
        # Each round shows what the reviewer did.
        self.assertEqual(reviewed["reviews"], [{"number": 1, "reviewer_id": "codex", "status": "succeeded",
                                                "verdict": "pass", "plan_hash": initial["plan_hash"], **ACTIVITY}])
        self.assertEqual(read_json(self.run / "reviews" / "01" / "review.json")["activity"], ACTIVITY)

    def test_a_later_packet_carries_each_earlier_review_in_full(self):
        """As in implement: the next reviewer sees each earlier round's findings and
        evidence, not only how they were dispositioned."""
        self.ready()
        first = self.submit(review_value("changes_required"))
        self.reflect(first)
        captured = {}

        def reviewer(prepared, validate):
            work_dir, cwd, probe = prepared.work_dir, prepared.cwd, prepared.probe
            captured["packet"] = json.loads(prepared.packet.decode("utf-8"))
            return adapter_result(review_value())
        with patch(RUN_REVIEW, side_effect=reviewer):
            self.engine.review(self.run_id)
        earlier = captured["packet"]["previous_reviews"]
        self.assertEqual([(item["number"], item["reviewer_id"], item["status"]) for item in earlier],
                         [(1, "codex", "succeeded")])
        self.assertEqual(earlier[0]["plan_hash"], first["plan_hash"])
        self.assertEqual(earlier[0]["review"], review_value("changes_required"))
        self.assertEqual(captured["packet"]["revision_history"][0]["decisions"][0]["finding_id"], "F1")

    def test_packet_is_review_data_without_reviewer_instructions(self):
        self.ready()
        captured = {}
        def reviewer(prepared, validate):
            work_dir, cwd, probe = prepared.work_dir, prepared.cwd, prepared.probe
            captured.update(packet=prepared.packet.decode("utf-8"), prompt=prepared.prompt.decode("utf-8"),
                            schema=prepared.schema, validate=validate, probe=probe)
            return adapter_result(review_value())
        with patch(RUN_REVIEW, side_effect=reviewer):
            self.engine.review(self.run_id)
        packet = captured["packet"]
        # The packet is review data only; common.reviewers adds the instructions once.
        self.assertIsInstance(json.loads(packet), dict)
        self.assertEqual(captured["schema"], (RESOURCES / "review.schema.json").read_bytes())
        self.assertIn((RESOURCES / "reviewer.md").read_text(encoding="utf-8").rstrip(), captured["prompt"])
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
        def reviewer(prepared, validate):
            provider = prepared.config
            seen.append(provider["id"])
            return adapter_result(review_value("changes_required"))
        with patch(RUN_REVIEW, side_effect=reviewer) as call:
            for index in range(1, 7):
                reviewed = self.engine.review(self.run_id)
                self.assertEqual(reviewed["review_count"], index)
                self.assertEqual(reviewed["status"], "awaiting_revision")
                result = self.reflect(reviewed)
                self.assertEqual(result["status"], "ready")
            # At the limit the request asks the human instead of starting a seventh reviewer.
            asked = self.engine.review(self.run_id)
            self.assertEqual(call.call_count, 6)
        self.assertEqual(seen, ["codex", "opencode"] * 3)
        self.assertEqual(asked["status"], "awaiting_human")
        self.assertEqual(asked["escalation"]["detail"], {"review_number": 6, "kind": "review_budget"})
        result = self.at_limit("stop")
        self.assertEqual(result["status"], "exhausted")
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

    def test_a_round_that_fails_after_the_reviewer_finished_keeps_what_it_did(self):
        self.ready()
        provider = self.engine.status(self.run_id)["config"]["reviewers"][0]
        with self.engine._lock(self.run_id):
            state = self.engine._state(self.run_id)
            state["review_count"] = 1
            # The reviewed plan hash no longer matches: the round fails.
            reservation = {"number": 1, "target": "0" * 64, "started_at": "2026-11-05T00:00:00+00:00"}
            self.engine._finish_review(state, reservation, "reviews/01", provider, adapter_result(review_value()), None)
            self.engine._save(state)
        status = self.engine.status(self.run_id)
        self.assertEqual(status["last_error"]["activity"], ACTIVITY)
        self.assertEqual([(r["status"], r["plan_hash"], r["tool_calls"]) for r in status["reviews"]],
                         [("failed", "0" * 64, ACTIVITY["tool_calls"])])

    def test_failed_attempt_consumed_resume_explicit_and_alternating(self):
        self.ready()
        with patch(RUN_REVIEW, side_effect=RuntimeError("mock provider unavailable")):
            failed = self.engine.review(self.run_id)
        self.assertEqual(failed["status"], "paused_error")
        self.assertEqual(failed["review_count"], 1)
        self.assertEqual(failed["next_reviewer"], "opencode")
        self.assertIsNone(failed["inflight"])
        self.assertEqual([(r["status"], r["reviewer_id"], r["plan_hash"]) for r in failed["reviews"]],
                         [("failed", "codex", failed["plan_hash"])])
        with patch(RUN_REVIEW) as no_call:
            with self.assertRaises(DevFlowError):
                self.engine.review(self.run_id)
            no_call.assert_not_called()
        resumed = self.resume("second-session")
        self.assertEqual(resumed["review_count"], 1)
        self.assertEqual(resumed["owner_session"], "second-session")
        with self.assertRaises(DevFlowError):
            self.engine.require_owner(self.run_id, self.session)
        with patch(RUN_REVIEW, return_value=adapter_result(review_value())) as call:
            next_state = self.engine.review(self.run_id)
        self.assertEqual(call.call_args.args[0].config["id"], "opencode")
        self.assertEqual(next_state["review_count"], 2)
        self.assertEqual(next_state["status"], "ready")

    def test_reviewer_failure_keeps_other_passes_for_the_same_plan(self):
        initial = self.ready()
        outcomes = [adapter_result(review_value()), ReviewerError("malformed opencode reply"),
                    ReviewerError("malformed codex reply"), adapter_result(review_value())]
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
        self.assertEqual([c.args[0].config["id"] for c in call.call_args_list], ["codex", "opencode", "codex", "opencode"])
        for number in ("02", "03"):
            self.assertTrue((self.run / "reviews" / number / "error.json").is_file(), number)

    def test_ownership_moving_during_preflight_stops_the_old_owner_from_reviewing(self):
        self.ready()
        caller = Engine(self.project, session=self.session, host="claude")

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
        with patch(RUN_REVIEW, return_value=adapter_result(review_value())) as call:
            reviewed = self.engine.review(self.run_id)
        self.assertEqual(call.call_args.args[0].config["id"], "codex")
        self.assertEqual(reviewed["review_count"], 1)

    def test_final_prompt_rejected_before_artifacts_and_reservation(self):
        self.ready()
        before = (self.run / "state.json").read_bytes()
        with patch("devflow_core.common.reviewers.MAX_PACKET_BYTES", 1), patch(RUN_REVIEW) as call:
            with self.assertRaisesRegex(ReviewerError, "byte limit"):
                self.engine.review(self.run_id)
        call.assert_not_called()
        self.assertEqual((self.run / "state.json").read_bytes(), before)
        self.assertFalse((self.run / "reviews").exists())
        self.assertFalse((self.run / "review-processes").exists())

    def test_invalid_semantic_review_is_a_consumed_failure(self):
        self.ready()
        result = self.submit({"unexpected": "not a review"})
        self.assertEqual(result["status"], "paused_error")
        self.assertEqual(result["review_count"], 1)
        self.assertTrue((self.run / "reviews" / "01" / "error.json").exists())

    def test_sixth_failed_attempt_is_not_retried_and_asks_the_human(self):
        self.ready()
        with patch(RUN_REVIEW, return_value=adapter_result(review_value("changes_required"))):
            for _ in range(5):
                self.reflect(self.engine.review(self.run_id))
        with patch(RUN_REVIEW, side_effect=RuntimeError("sixth call failed")):
            final = self.engine.review(self.run_id)
        self.assertEqual(final["status"], "ready")
        self.assertEqual(final["review_count"], 6)
        self.assertFalse(final["final_plan_reviewed"])
        resumed = self.resume("recovery-session")
        self.assertEqual(resumed["review_count"], 6)
        self.assertEqual(resumed["status"], "ready")
        asked = self.ask_at_limit()
        self.assertEqual((asked["status"], asked["review_count"]), ("awaiting_human", 6))
        self.assertIn("sixth call failed", asked["last_error"]["message"])

    def test_config_is_frozen_for_run_even_if_project_default_changes(self):
        self.ready()
        (self.project / "devflow.config.json").write_text('{"max_reviews":2}', encoding="utf-8")
        self.assertEqual(self.engine.status(self.run_id)["config"]["max_reviews"], 6)
        with patch(RUN_REVIEW, return_value=adapter_result(review_value("changes_required"))):
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
        # A round's status uses the same words as implement's attempts; every round has its target.
        self.assertEqual([(r["status"], r["reviewer_id"], r["plan_hash"]) for r in interrupted["reviews"]],
                         [("dispatched", "codex", interrupted["plan_hash"])])
        self.assertNotAccepted(self.resume_prompt("new-session"), "resume", "recover")
        self.assertEqual(self.engine.status(self.run_id)["owner_session"], self.session)
        with self.assertRaises(DevFlowError):
            self.engine.recover(self.run_id, confirmed=True, expected_review=2)
        recovered = self.engine.recover(self.run_id, confirmed=True, expected_review=1)
        self.assertEqual(recovered["status"], "paused_error")
        self.assertIsNone(recovered["inflight"])
        self.assertEqual([(r["status"], r["reviewer_id"], r["plan_hash"]) for r in recovered["reviews"]],
                         [("failed", "codex", interrupted["plan_hash"])])
        resumed = self.resume("new-session")
        self.assertEqual(resumed["review_count"], 1)
        self.assertEqual(resumed["next_reviewer"], "opencode")
        self.assertEqual(resumed["status"], "ready")

    def test_recover_at_the_limit_leads_to_the_question(self):
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
        self.assertEqual(recovered["status"], "ready")
        self.assertIsNone(recovered["inflight"])
        self.assertEqual(recovered["review_count"], 2)  # The interrupted attempt stays consumed.
        self.assertIsNone(recovered["next_reviewer"])
        self.assertTrue((self.run / "reviews" / "02" / "recovery.json").is_file())
        asked = self.ask_at_limit()
        self.assertEqual(asked["escalation"]["questions"], [
            limit_question(asked["plan_hash"], used=2, limit=2, approved="none", new_limit=4)])
        self.assertFalse(self.engine.report(self.run_id)["converged"])

    def reach_limit_with_opencode_approving(self):
        """Six reviews: five with findings, then opencode approves the current plan."""
        self.ready()
        for _ in range(5):
            self.reflect(self.submit(review_value("changes_required")))
        self.assertEqual(self.submit(review_value())["status"], "ready")
        return self.ask_at_limit()

    def test_the_limit_question_and_continue_completes_with_the_earlier_pass(self):
        asked = self.reach_limit_with_opencode_approving()
        self.assertEqual(asked["status"], "awaiting_human")
        self.assertEqual((asked["escalation"]["reason"], asked["escalation"]["questions"]),
                         (REVIEW_LIMIT_REASON, [limit_question(asked["plan_hash"])]))
        record = read_json(self.run / f"escalations/{asked['escalation']['id']}.json")
        self.assertEqual(record["detail"], {"review_number": 6, "kind": "review_budget"})
        # The resume command is never the answer.
        self.resume(self.session)
        self.assertEqual(self.engine.status(self.run_id)["human_events"], [])
        actual = self.limit_answer("Continue with two more reviews.")
        for value in ({"answer": actual["answer_text"], "event_ids": [actual["event_id"]]},
                      {"answer": actual["answer_text"], "event_ids": [actual["event_id"]], "decision": "cancel"}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(DevFlowError, "continue, stop or accept"):
                    self.resolve(self.draft("limit-answer.json", value))
        continued = self.resolve(self.draft("limit-answer.json", {
            "answer": actual["answer_text"], "event_ids": [actual["event_id"]], "decision": "continue"}))
        self.assertEqual((continued["status"], continued["review_limit"], continued["remaining_reviews"],
                          continued["next_reviewer"]), ("ready", 8, 2, "codex"))
        self.assertEqual(continued["config"]["max_reviews"], 6)
        answer = read_json(self.run / f"escalations/{asked['escalation']['id']}-answer.json")
        self.assertEqual(answer["decision"], "continue")
        seen = []
        def reviewer(prepared, validate):
            provider = prepared.config
            seen.append(provider["id"])
            return adapter_result(review_value())
        with patch(RUN_REVIEW, side_effect=reviewer):
            final = self.engine.review(self.run_id)
        # Codex's seventh review joins opencode's sixth pass of the same plan.
        self.assertEqual(seen, ["codex"])
        self.assertEqual((final["status"], final["review_count"]), ("completed", 7))

    def test_continuing_dispatches_reviews_seven_and_eight_in_order(self):
        self.reach_limit_with_opencode_approving()
        self.at_limit("continue")
        seen = []
        def reviewer(prepared, validate):
            provider = prepared.config
            seen.append(provider["id"])
            return adapter_result(review_value("changes_required" if len(seen) == 1 else "pass"))
        with patch(RUN_REVIEW, side_effect=reviewer):
            self.reflect(self.engine.review(self.run_id))  # codex finds an issue; the plan changes
            eighth = self.engine.review(self.run_id)       # opencode approves the new plan alone
        self.assertEqual(seen, ["codex", "opencode"])
        self.assertEqual((eighth["status"], eighth["review_count"]), ("ready", 8))
        asked = self.ask_at_limit()
        self.assertEqual(asked["escalation"]["questions"], [
            limit_question(asked["plan_hash"], used=8, limit=8, new_limit=10)])

    def test_a_limit_answer_grants_no_authority_over_later_findings(self):
        (self.project / "devflow.config.json").write_text('{"max_reviews":2}', encoding="utf-8")
        self.ready()
        self.reflect(self.submit(review_value("changes_required")))
        self.reflect(self.submit(review_value("changes_required")))
        self.ask_at_limit()
        self.at_limit("continue")
        state = self.submit(review_value("changes_required", severity="critical"))
        with self.assertRaisesRegex(DevFlowError, "recorded human resolution"):
            self.reflect(state, action="reject", change=False)

    def test_decision_belongs_only_to_the_limit_question(self):
        self.ready()
        self.submit(review_value("human_required", human=True))
        actual = self.limit_answer("Keep scope")
        with self.assertRaisesRegex(DevFlowError, "continue, stop or accept"):
            self.resolve(self.draft("answer.json", {"answer": actual["answer_text"], "event_ids": [actual["event_id"]],
                                                    "decision": "continue"}))
        resolved = self.resolve(self.draft("answer.json", {"answer": actual["answer_text"],
                                                          "event_ids": [actual["event_id"]]}))
        self.assertEqual(resolved["status"], "awaiting_revision")

    def test_cancel_while_the_limit_question_waits(self):
        (self.project / "devflow.config.json").write_text('{"max_reviews":2}', encoding="utf-8")
        self.ready()
        self.reflect(self.submit(review_value("changes_required")))
        self.reflect(self.submit(review_value("changes_required")))
        self.assertEqual(self.ask_at_limit()["status"], "awaiting_human")
        self.assertEqual(self.engine.cancel(self.run_id)["status"], "cancelled")

    def test_a_round_past_the_ceiling_ends_without_a_question(self):
        self.ready()
        stored = read_json(self.run / "state.json")
        atomic_write_json(self.run / "state.json", dict(stored, review_count=98, review_limit=98))
        ended = self.ask_at_limit()
        self.assertEqual((ended["status"], ended["escalation"]), ("exhausted", None))

    def test_a_review_limit_outside_whole_rounds_is_corrupt(self):
        self.ready()
        stored = read_json(self.run / "state.json")
        for limit in (7, 5, 100):
            with self.subTest(limit=limit):
                atomic_write_json(self.run / "state.json", dict(stored, review_limit=limit))
                with self.assertRaisesRegex(DevFlowError, "never reset"):
                    self.engine.status(self.run_id)
        # A state without the field is not this format: listed as invalid, never continued.
        atomic_write_json(self.run / "state.json", {key: value for key, value in stored.items() if key != "review_limit"})
        self.assertEqual(self.engine.store.inspect(self.run_id).format, "invalid")

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
        # Every question records its kind (0.7.2) and the number of reviews so far.
        self.assertEqual(state["escalation"]["detail"], {"review_number": 1, "kind": "review_decision"})
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
        self.assertEqual(router.status(self.project, None, self.run_id, host="claude")["human_events"], [])

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
        # tool_input before PostToolUse; record() first sends the PreToolUse
        # call without answers, which is how DevFlow knows the model did not
        # fill them in itself.
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
            with self.subTest(change=change):
                self.assertUnrecorded(self.hook(dict(payload, **change)), "replayed with different content")
        self.assertEqual((self.run / "state.json").read_bytes(), before)
        self.assertEqual(read_json(self.run / "events" / (identifier + ".json"))["payload"]["tool_response"], "Original answer")

    def test_question_ids_are_scoped_to_owner_session_and_stale_payload_is_not_recorded(self):
        self.ready()
        old = self.event("PostToolUse", tool_name="AskUserQuestion", tool_use_id="same-local-id",
            tool_input={"questions": [{"question": "Q"}]}, tool_response="Old answer")
        original_id = self.record(old)
        self.submit(review_value("human_required", human=True))
        self.resume("new-owner")
        before = (self.run / "state.json").read_bytes()
        # The former owner's hooks record nothing: its question, its answer and its stop.
        self.assertIsNone(self.hook(dict(old, hook_event_name="PreToolUse")))
        self.assertIsNone(self.hook(old))
        self.assertIsNone(self.hook(self.event("Stop")))
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
            with self.subTest(payload=payload):
                self.assertUnrecorded(self.hook(payload), "actual tool identity")
        self.assertEqual((self.run / "state.json").read_bytes(), before)

    def test_state_lock_fails_safely_without_dispatch(self):
        self.ready()
        lock = self.run / "state.lock"
        engine = Engine(self.project, lock_wait=0.05, host="claude")  # Do not wait for the runner's full lock timeout.
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
        self.assertNotAccepted(self.resume_prompt("other-session"), "resume", "original Claude session")
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
        self.assertNotAccepted(self.hook(self.event(prompt="/devflow:plan A request")), "plan", "hard ceiling")
        self.assertIsNone(self.bound_run())
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

    def test_finished_run_stays_bound_until_next_real_human_prompt(self):
        self.ready()
        self.submit(review_value())
        self.submit(review_value())
        self.assertEqual(self.bound_run(), self.run_id)
        write = self.event("PreToolUse", tool_name="Write", tool_input={"file_path": str(self.project / "app.py"), "content": "implementation"})
        events_before = read_json(self.run / "state.json")["event_ids"]
        # DevFlow does not stop the tool; the finished run stays bound for the rest of the turn.
        self.assertIsNone(self.hook(write))
        self.assertEqual(self.bound_run(), self.run_id)
        self.assertIsNone(self.hook(self.event(prompt="Now I have a separate request.")))
        self.assertIsNone(self.bound_run())
        self.assertEqual(router.status(self.project, self.session, None, host="claude")["status"], "idle")
        # The prompt that released the finished run is not recorded in it.
        self.assertEqual(read_json(self.run / "state.json")["event_ids"], events_before)
        self.assertIsNone(self.hook(write))

    def test_terminal_resume_rebinds_owner_without_resetting_budget(self):
        self.ready()
        self.submit(review_value())
        self.submit(review_value())
        self.session = "new-report-session"
        state = self.resume(self.session)
        self.assertEqual(state["owner_session"], self.session)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["review_count"], 2)
        self.assertEqual(self.bound_run(), self.run_id)
        self.engine.require_owner(self.run_id, self.session)
        self.assertTrue(self.engine.report(self.run_id)["converged"])
        self.assertIsNone(self.hook(self.event("PreToolUse", tool_name="Write",
                                               tool_input={"file_path": str(self.project / "app.py"), "content": "x"})))
        with patch(RUN_REVIEW) as call:
            with self.assertRaises(DevFlowError):
                self.engine.review(self.run_id)
            call.assert_not_called()

    def test_no_second_active_run_in_a_session_and_no_double_seal(self):
        self.start_interview()
        self.assertNotAccepted(self.hook(self.event(prompt="/devflow:plan a different request")),
                               "plan", "Finish or cancel")
        # Another session starts its own plan run beside this one.
        self.hook(self.event(prompt="/devflow:plan a different request", session="another-session"))
        other = self.bound_run("another-session")
        self.assertEqual(sorted(path.name for path in self.run.parent.iterdir()), sorted([self.run_id, other]))
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

    def test_publish_before_second_approval_is_not_published(self):
        self.ready()
        self.submit(review_value())
        with self.assertRaisesRegex(DevFlowError, "both reviewers"):
            self.engine.publish(self.run_id)
        self.assertFalse((self.project / "docs").exists())
        self.assertEqual(self.engine.status(self.run_id)["review_count"], 1)

    def test_publish_exhausted_last_revision_is_not_published(self):
        (self.project / "devflow.config.json").write_text('{"max_reviews":2}', encoding="utf-8")
        self.ready()
        self.reflect(self.submit(review_value("changes_required")))
        self.reflect(self.submit(review_value("changes_required")))
        self.ask_at_limit()
        self.assertEqual(self.at_limit("stop")["status"], "exhausted")
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

    def test_parallel_publications_of_one_name_serialize_and_the_later_takes_its_run_suffix(self):
        first = self.completed("/devflow:plan REVIEW-10")
        first_id = self.run_id
        second = self.completed("/devflow:plan REVIEW-10")
        second_id = self.run_id
        entered, release = threading.Event(), threading.Event()
        original = engine_module.write_new
        first_thread = []

        def paused_export(path, data):
            # The first publication stops after saving its intent, before writing outside the run.
            if threading.current_thread() in first_thread and path.is_relative_to(self.project / "docs"):
                entered.set()
                self.assertTrue(release.wait(10))
            return original(path, data)

        results, errors = {}, []

        def publish(run_id):
            try:
                results[run_id] = Engine(self.project, host="claude").publish(run_id)
            except Exception as exc:  # noqa: BLE001 - reported by the assertion below
                errors.append(exc)

        with patch("devflow_core.plan.engine.write_new", side_effect=paused_export):
            one = threading.Thread(target=publish, args=(first_id,))
            first_thread.append(one)
            one.start()
            self.assertTrue(entered.wait(10))
            two = threading.Thread(target=publish, args=(second_id,))
            two.start()
            two.join(0.3)
            self.assertTrue(two.is_alive())  # Waiting for the publication lock.
            release.set()
            one.join(10)
            two.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(results[first_id]["publication"]["path"], "docs/plans/REVIEW-10.md")
        self.assertEqual(results[second_id]["publication"]["path"], f"docs/plans/REVIEW-10-{second_id}.md")
        for state, result in ((first, results[first_id]), (second, results[second_id])):
            self.assertEqual((self.project / result["publication"]["path"]).read_bytes(),
                             Path(state["current_plan"]).read_bytes())
            self.assertTrue((self.project / result["publication"]["approval_path"]).is_file())
        self.assertFalse((self.project / ".devflow" / "publish.lock").exists())

    def test_a_failed_publication_keeps_its_name_from_another_run(self):
        first = self.completed("/devflow:plan REVIEW-10")
        first_id = self.run_id
        original = engine_module.write_new

        def fail_export(path, data):
            if path.suffix == ".md" and path.is_relative_to(self.project / "docs"):
                raise OSError("Synthetic unavailable filesystem")
            return original(path, data)

        with patch("devflow_core.plan.engine.write_new", side_effect=fail_export):
            with self.assertRaisesRegex(DevFlowError, "publication failed"):
                self.engine.publish(first_id)
        self.assertEqual(self.engine.status(first_id)["publication"]["path"], "docs/plans/REVIEW-10.md")
        second = self.completed("/devflow:plan REVIEW-10")
        second_id = self.run_id
        later = self.engine.publish(second_id)
        self.assertEqual(later["publication"]["path"], f"docs/plans/REVIEW-10-{second_id}.md")
        retried = self.engine.publish(first_id)
        self.assertEqual(retried["publication"]["status"], "published")
        self.assertEqual(retried["publication"]["path"], "docs/plans/REVIEW-10.md")
        for state, result in ((first, retried), (second, later)):
            self.assertEqual((self.project / result["publication"]["path"]).read_bytes(),
                             Path(state["current_plan"]).read_bytes())
            self.assertEqual((self.project / result["publication"]["approval_path"]).read_bytes(),
                             (Path(state["run_dir"]) / "approval.md").read_bytes())

    def test_a_reservation_holds_a_name_that_differs_only_in_case_where_the_os_folds_case(self):
        config = self.project / "devflow.config.json"
        config.write_text(json.dumps({"plan": {"publish": {"filename": "Release"}}}), encoding="utf-8")
        first = self.completed("/devflow:plan First release plan")
        first_id = self.run_id
        original = engine_module.write_new

        def fail_export(path, data):
            if path.suffix == ".md" and path.is_relative_to(self.project / "docs"):
                raise OSError("Synthetic unavailable filesystem")
            return original(path, data)

        with patch("devflow_core.plan.engine.write_new", side_effect=fail_export):
            with self.assertRaisesRegex(DevFlowError, "publication failed"):
                self.engine.publish(first_id)
        config.write_text(json.dumps({"plan": {"publish": {"filename": "release"}}}), encoding="utf-8")
        second = self.completed("/devflow:plan Second release plan")
        second_id = self.run_id
        # Compare paths the way a case-folding file system (Windows) does.
        with patch.object(engine_module, "path_key", side_effect=lambda path: paths.path_key(path).casefold()):
            later = self.engine.publish(second_id)
        self.assertEqual(later["publication"]["path"], f"docs/plans/release-{second_id}.md")
        retried = self.engine.publish(first_id)
        self.assertEqual(retried["publication"]["path"], "docs/plans/Release.md")
        for state, result in ((first, retried), (second, later)):
            self.assertEqual((self.project / result["publication"]["path"]).read_bytes(),
                             Path(state["current_plan"]).read_bytes())

    def test_another_runs_malformed_publication_never_blocks_a_healthy_publication(self):
        for ticket, damaged in (("REVIEW-11", "damaged"), ("REVIEW-12", {"path": 5, "approval_path": ["x"]}),
                                ("REVIEW-13", {"path": "docs/plans/bad\x00name.md"})):
            with self.subTest(publication=damaged):
                self.completed(f"/devflow:plan {ticket}")
                state_path = self.run / "state.json"
                atomic_write_json(state_path, dict(read_json(state_path), publication=damaged))
                before = state_path.read_bytes()
                healthy = self.completed(f"/devflow:plan {ticket}")
                result = self.engine.publish(self.run_id)
                self.assertEqual(result["publication"]["path"], f"docs/plans/{ticket}.md")
                self.assertEqual((self.project / result["publication"]["path"]).read_bytes(),
                                 Path(healthy["current_plan"]).read_bytes())
                self.assertEqual(state_path.read_bytes(), before)  # The damaged run is never touched.

    def test_another_runs_unresolvable_publication_path_never_blocks_a_healthy_publication(self):
        historic = self.completed("/devflow:plan REVIEW-14")
        historic_id = self.run_id
        published = self.engine.publish(historic_id)["publication"]["path"]
        docs = self.project / "docs"
        docs.rename(self.project / "docs-moved")
        try:
            docs.symlink_to(docs, target_is_directory=True)  # A loop: resolving it fails.
        except (OSError, NotImplementedError):
            self.skipTest("Creating symlinks requires OS permission")
        state_path = self.run.parent / historic_id / "state.json"
        before = state_path.read_bytes()
        (self.project / "devflow.config.json").write_text(
            json.dumps({"plan": {"publish": {"directory": "output/plans"}}}), encoding="utf-8")
        healthy = self.completed("/devflow:plan REVIEW-14")
        result = self.engine.publish(self.run_id)
        self.assertEqual(result["publication"]["path"], "output/plans/REVIEW-14.md")
        self.assertEqual((self.project / result["publication"]["path"]).read_bytes(),
                         Path(healthy["current_plan"]).read_bytes())
        self.assertEqual(state_path.read_bytes(), before)
        self.assertEqual(self.engine.status(historic_id)["publication"]["path"], published)

    def test_a_left_publication_lock_stops_publication_without_writing(self):
        self.completed("/devflow:plan REVIEW-10")
        lock = self.project / ".devflow" / "publish.lock"
        lock.write_text('{"token": "left by a stopped process"}', encoding="utf-8")
        engine = Engine(self.project, lock_wait=0.1, host="claude")
        with self.assertRaisesRegex(DevFlowError, "publish.lock"):
            engine.publish(self.run_id)
        self.assertFalse((self.project / "docs").exists())
        self.assertNotIn("path", self.engine.status(self.run_id)["publication"])
        self.assertTrue(lock.exists())  # Never removed because of its age.

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
        approval = target.with_name("REVIEW-10.approval.md")
        before, before_approval = target.read_bytes(), approval.read_bytes()
        self.assertEqual(self.engine.status(self.run_id)["publication"]["status"], "publishing")
        recovered = Engine(self.project, host="claude").publish(self.run_id)
        self.assertEqual((recovered["publication"]["path"], recovered["publication"]["approval_path"]),
                         ("docs/plans/REVIEW-10.md", "docs/plans/REVIEW-10.approval.md"))
        # The resumed publication writes the same bytes: the approval record was made once, with the intent.
        self.assertEqual((target.read_bytes(), approval.read_bytes()), (before, before_approval))
        self.assertEqual(sorted(path.name for path in target.parent.iterdir()), ["REVIEW-10.approval.md", "REVIEW-10.md"])

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
            with self.subTest(path=path):
                self.assertNotAccepted(self.hook(self.event(prompt="/devflow:plan A request")), "plan",
                                       "publish.directory")
                self.assertIsNone(self.bound_run())

    # -- the published plan's name: publish.filename ------------------------------------
    def publish_with_filename(self, template, request=None, existing=()):
        """Publish a completed run whose project sets ``publish.filename`` and return the
        plan's and the approval record's paths. ``existing`` names human-owned files
        (``{run_id}`` filled in) placed before publication; they are never overwritten."""
        (self.project / "devflow.config.json").write_text(
            json.dumps({"plan": {"publish": {"filename": template}}}), encoding="utf-8")
        state = self.completed(request)
        directory = self.project / "docs" / "plans"
        names = [name.format(run_id=self.run_id) for name in existing]
        for name in names:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / name).write_bytes(b"Human-owned existing plan\n")
        publication = self.engine.publish(self.run_id)["publication"]
        self.assertEqual(publication["status"], "published")
        self.assertEqual((self.project / publication["path"]).read_bytes(), Path(state["current_plan"]).read_bytes())
        self.assertTrue((self.project / publication["approval_path"]).is_file())
        for name in names:
            self.assertEqual((directory / name).read_bytes(), b"Human-owned existing plan\n")
        return publication["path"], publication["approval_path"]

    def assertPublishedAs(self, paths, name):
        self.assertEqual(paths, (f"docs/plans/{name}.md", f"docs/plans/{name}.approval.md"))

    def test_publish_filename_with_run_id_does_not_use_the_ticket(self):
        paths = self.publish_with_filename("plan-{run_id}", "/devflow:plan REVIEW-10 を計画")
        self.assertPublishedAs(paths, f"plan-{self.run_id}")

    def test_publish_filename_with_ticket_uses_the_ticket(self):
        paths = self.publish_with_filename("{ticket}-plan", "/devflow:plan REVIEW-10 を計画")
        self.assertPublishedAs(paths, "REVIEW-10-plan")

    def test_publish_filename_with_ticket_and_no_ticket_is_plan_and_run_id(self):
        paths = self.publish_with_filename("{ticket}-plan")
        self.assertPublishedAs(paths, f"plan-{self.run_id}")

    def test_publish_filename_with_text_around_run_id(self):
        paths = self.publish_with_filename("devflow-{run_id}", "/devflow:plan REVIEW-10 を計画")
        self.assertPublishedAs(paths, f"devflow-{self.run_id}")

    def test_publish_filename_without_values(self):
        self.assertPublishedAs(self.publish_with_filename("plan"), "plan")

    def test_publish_filename_without_values_in_use_adds_run_id(self):
        paths = self.publish_with_filename("plan", existing=("plan.md",))
        self.assertPublishedAs(paths, f"plan-{self.run_id}")

    def test_publish_filename_with_run_id_in_use_still_adds_run_id(self):
        paths = self.publish_with_filename("{run_id}", existing=("{run_id}.md",))
        self.assertPublishedAs(paths, f"{self.run_id}-{self.run_id}")

    def test_publish_filename_is_frozen(self):
        (self.project / "devflow.config.json").write_text('{"plan":{"publish":{"filename":"frozen-{ticket}"}}}',
                                                          encoding="utf-8")
        self.completed("/devflow:plan REVIEW-10")
        (self.project / "devflow.config.json").write_text('{"plan":{"publish":{"filename":"changed-{ticket}"}}}',
                                                          encoding="utf-8")
        result = self.engine.publish(self.run_id)
        self.assertEqual((result["publication"]["path"], result["publication"]["approval_path"]),
                         ("docs/plans/frozen-REVIEW-10.md", "docs/plans/frozen-REVIEW-10.approval.md"))

    def test_unsafe_publish_filename_rejected_before_model_calls(self):
        values = ["", "{date}", "{ticket", "ticket}", "{RUN_ID}", "a/b", "../x", ".hidden", "-x", "has space", "CON",
                  "nul.{run_id}", "x.", 1, None,
                  # Too long with the longest values: 129, 135 and 129 characters.
                  "a" * 129, "{ticket}" * 3, "{ticket}-" + "a" * 83]
        for value in values:
            (self.project / "devflow.config.json").write_text(json.dumps({"plan": {"publish": {"filename": value}}}),
                                                              encoding="utf-8")
            with self.subTest(value=value):
                self.assertNotAccepted(self.hook(self.event(prompt="/devflow:plan A request")), "plan",
                                       "publish.filename")
                self.assertIsNone(self.bound_run())
        (self.project / "devflow.config.json").write_text('{"plan":{"publish":{"name":"plan"}}}', encoding="utf-8")
        self.assertNotAccepted(self.hook(self.event(prompt="/devflow:plan A request")), "plan",
                               "publish must contain enabled and directory, and may contain filename.")
        self.assertIsNone(self.bound_run())

    def test_publish_filename_at_the_length_limit_is_accepted(self):
        # 128 characters, with the longest ticket ID (45 characters) for {ticket}.
        for value in ("a" * 128, "{ticket}-" + "a" * 82):
            (self.project / "devflow.config.json").write_text(json.dumps({"plan": {"publish": {"filename": value}}}),
                                                              encoding="utf-8")
            with self.subTest(value=value):
                self.assertEqual(load_config(self.project, WORKFLOWS["plan"])["publish"]["filename"], value)

    def start_before_filename(self, request):
        """A run started before 0.7.3: its frozen configuration has no publish.filename."""
        def without_filename(project, workflow):
            config = original(project, workflow)
            del config["publish"]["filename"]
            return config
        original = config_module.load_config
        with patch("devflow_core.plan.config.load_config", side_effect=without_filename):
            self.completed(request)
        self.assertNotIn("filename", self.engine.status(self.run_id)["config"]["publish"])
        self.assertNotIn("filename", read_json(self.run / "config.json")["publish"])

    def test_run_without_filename_publishes_with_the_ticket_and_passes_doctor(self):
        self.start_before_filename("/devflow:plan REVIEW-10 を計画")
        code, report = self.runner("--run", self.run_id, "plan", "doctor")
        self.assertEqual(code, 0, report)
        self.assertEqual(report["configuration_source"], "frozen_run")
        self.assertNotIn("filename", report["config"]["publish"])
        self.assertEqual(report["errors"], [])
        result = self.engine.publish(self.run_id)
        self.assertEqual((result["publication"]["path"], result["publication"]["approval_path"]),
                         ("docs/plans/REVIEW-10.md", "docs/plans/REVIEW-10.approval.md"))

    def test_run_without_filename_and_ticket_publishes_with_run_id(self):
        self.start_before_filename(None)
        result = self.engine.publish(self.run_id)
        self.assertEqual(result["publication"]["path"], f"docs/plans/plan-{self.run_id}.md")

    def test_default_filename_is_the_default_setting(self):
        self.assertEqual(FILENAME_DEFAULT, read_json(RESOURCES / "defaults.json")["publish"]["filename"])
        self.assertEqual(load_config(self.project, WORKFLOWS["plan"])["publish"]["filename"], FILENAME_DEFAULT)

    # -- stopped saves: a save that stops before state.json is replaced is not committed --
    def stop_commit(self, when=lambda value: True):
        """Stop the saves (chosen by the new state) just before ``state.json`` is replaced."""
        original_atomic = runs_module.atomic_json

        def crash_state_commit(path, value):
            if path == self.run / "state.json" and when(value):
                raise InjectedCrash("after the records, before the state commit")
            return original_atomic(path, value)
        return patch("devflow_core.common.runs.atomic_json", side_effect=crash_state_commit)

    def test_a_stopped_seal_is_not_committed_and_sealing_again_captures_anew(self):
        self.start_interview()
        with self.stop_commit(), self.assertRaises(InjectedCrash):
            self.seal()
        self.assertEqual(read_json(self.run / "state.json")["status"], "interview")
        self.assertNotIn("interview.json", read_json(self.run / "state.json")["artifacts"])
        self.assertTrue((self.run / "interview.json").is_file())  # A leftover nothing reads.
        decisions = self.draft("decisions.json", {"summary": "Sealed again after the stop.", "decisions": [],
                                                  "constraints": [], "open_questions": []})
        with patch(RUN_REVIEW) as provider:
            sealed = self.engine.seal(self.run_id, decisions)
        self.assertEqual(sealed["status"], "planning")
        self.assertEqual(read_json(self.run / "decisions.json")["summary"], "Sealed again after the stop.")
        self.engine.report(self.run_id)  # Every committed hash matches.
        provider.assert_not_called()

    def test_a_stopped_apply_keeps_the_committed_plan_and_applies_once_when_run_again(self):
        self.ready()
        state = self.submit(review_value("changes_required"))
        base_revision = state["state_revision"]
        with self.stop_commit(), self.assertRaises(InjectedCrash):
            self.reflect(state)
        stopped = read_json(self.run / "state.json")
        self.assertEqual((stopped["state_revision"], stopped["plan_version"]), (base_revision, 0))
        self.assertTrue((self.run / "plans" / "plan-v001.md").is_file())  # Leftover.
        with patch(RUN_REVIEW) as provider:
            applied = self.reflect(self.engine.status(self.run_id))
        self.assertEqual((applied["status"], applied["plan_version"]), ("ready", 1))
        self.assertEqual(applied["state_revision"], base_revision + 1)
        self.assertEqual(len(list((self.run / "plans").glob("*.md"))), 2)
        self.engine.report(self.run_id)  # The replaced leftover matches its committed hash.
        provider.assert_not_called()

    def test_a_stopped_reservation_never_dispatches_and_the_review_can_run(self):
        self.ready()
        with self.stop_commit(lambda value: value["status"] == "reviewing"), patch(RUN_REVIEW) as provider:
            with self.assertRaises(InjectedCrash):
                self.engine.review(self.run_id)
            provider.assert_not_called()
        unchanged = self.engine.status(self.run_id)
        self.assertEqual((unchanged["status"], unchanged["review_count"]), ("ready", 0))
        self.assertTrue((self.run / "reviews" / "01" / "packet.md").exists())  # Leftover.
        self.assertEqual(self.submit(review_value())["review_count"], 1)
        self.engine.report(self.run_id)

    def test_a_review_whose_last_save_stopped_waits_for_human_recovery(self):
        self.ready()
        with self.stop_commit(lambda value: value["status"] != "reviewing"), \
                patch(RUN_REVIEW, return_value=adapter_result(review_value())):
            with self.assertRaises(InjectedCrash):
                self.engine.review(self.run_id)
        stopped = self.engine.status(self.run_id)
        self.assertEqual((stopped["status"], stopped["review_count"]), ("reviewing", 1))
        with patch(RUN_REVIEW) as provider, self.assertRaises(DevFlowError):
            self.engine.review(self.run_id)
        provider.assert_not_called()
        recovered = self.engine.recover(self.run_id, confirmed=True, expected_review=1)
        self.assertEqual(recovered["review_count"], 1)
        self.assertEqual(recovered["reviews"][0]["status"], "failed")

    def test_a_stopped_publication_is_recorded_on_the_committed_state(self):
        self.completed("/devflow:plan REVIEW-10")
        original = runs_module.write_new
        failures = []

        def fail_receipt_once(path, data):
            if path == self.run / "publication.json" and not failures:
                failures.append(path)
                raise OSError("Synthetic receipt failure")
            return original(path, data)
        with patch("devflow_core.common.runs.write_new", side_effect=fail_receipt_once):
            with self.assertRaisesRegex(DevFlowError, "publication failed"):
                self.engine.publish(self.run_id)
        failed = self.engine.status(self.run_id)
        self.assertEqual(failed["publication"]["status"], "error")
        self.assertNotIn("publication.json", read_json(self.run / "state.json")["artifacts"])
        self.engine.report(self.run_id)  # Every listed record exists and matches.
        published = self.engine.publish(self.run_id)
        self.assertEqual(published["publication"]["path"], "docs/plans/REVIEW-10.md")
        self.assertIn("publication.json", read_json(self.run / "state.json")["artifacts"])

    def test_a_leftover_receipt_is_never_taken_as_the_publication(self):
        self.completed("/devflow:plan REVIEW-10")
        with self.stop_commit(lambda value: value["publication"].get("status") == "published"):
            with self.assertRaises(InjectedCrash):
                self.engine.publish(self.run_id)
        self.assertTrue((self.run / "publication.json").is_file())
        self.assertNotIn("publication.json", read_json(self.run / "state.json")["artifacts"])
        published = self.engine.publish(self.run_id)
        self.assertEqual(published["publication"]["status"], "published")
        self.assertIn("publication.json", read_json(self.run / "state.json")["artifacts"])
        self.engine.report(self.run_id)
        target = self.project / "docs" / "plans" / "REVIEW-10.md"
        target.unlink()
        with self.assertRaisesRegex(DevFlowError, "not silently recreated"):
            self.engine.publish(self.run_id)
        self.assertFalse(target.exists())

    def test_a_failure_before_the_save_commits_nothing_and_can_be_run_again(self):
        self.ready()
        with patch("devflow_core.common.runs.write_new", side_effect=InjectedCrash("before any record")), \
                patch(RUN_REVIEW) as provider:
            with self.assertRaises(InjectedCrash):
                self.engine.review(self.run_id)
            provider.assert_not_called()
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
        def reviewer(prepared, validate):
            provider = prepared.config
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
        def reviewer(prepared, validate):
            provider = prepared.config
            # A human confirmed the process stopped, but its result still arrives.
            self.engine.recover(self.run_id, confirmed=True, expected_review=1)
            after_recover.update(read_json(self.run / "state.json"))
            return adapter_result(review_value())
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
        self.assertEqual(value["result"], adapter_result(review_value()))
        self.assertIsNone(value["error"])
        self.assertEqual(value["reservation"]["reviewer_id"], "codex")

    def test_stale_owner_cannot_record_human_evidence_after_handover(self):
        self.ready()
        old_payload = self.event(prompt="Obsolete session says yes.")
        self.resume("new-owner-session")
        before = read_json(self.run / "state.json")
        # The old session may keep talking, but its prompt is never recorded as evidence.
        context = self.hook(old_payload)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("no longer owns DevFlow run", context)
        with self.assertRaisesRegex(DevFlowError, "stale-session"):
            self.engine.store.append_event(self.run_id, old_payload, "prompt")
        self.assertEqual(read_json(self.run / "state.json"), before)
        self.session = "new-owner-session"
        current = self.record(self.event(prompt="Current session decision."))
        after = read_json(self.run / "state.json")
        self.assertEqual(len(after["event_ids"]), len(before["event_ids"]) + 1)
        self.assertEqual(after["event_ids"][-1], current)


    # -- 0.7.0: review summary, acceptance at the limit, approval records ----------------------
    def submit_with_output(self, value, *, lines=(), error=None, raw=""):
        """A review whose CLI printed ``lines`` (then ``raw`` text) to its stdout log; ``error`` fails the attempt."""
        def reviewer(prepared, validate):
            work_dir, cwd, probe = prepared.work_dir, prepared.cwd, prepared.probe
            work_dir.mkdir(parents=True, exist_ok=True)
            (work_dir / "invocation.json").write_text(json.dumps({"cli_version": "codex-cli 9.9.9"}), encoding="utf-8")
            (work_dir / "stdout.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines) + raw,
                                                   encoding="utf-8")
            if error is not None:
                raise error
            return adapter_result(validate(value))
        with patch(RUN_REVIEW, side_effect=reviewer):
            return self.engine.review(self.run_id)

    def at_the_limit(self, max_reviews=2):
        """A run at its review limit after reviews with findings, waiting at the question."""
        (self.project / "devflow.config.json").write_text(json.dumps({"max_reviews": max_reviews}), encoding="utf-8")
        self.ready()
        for _ in range(max_reviews):
            self.reflect(self.submit(review_value("changes_required")))
        return self.ask_at_limit()

    def test_review_records_keep_times_tokens_and_the_plan_version(self):
        self.ready()
        usage = {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}}
        self.submit_with_output(review_value("changes_required"), lines=[usage])
        record = read_json(self.run / "reviews" / "01" / "review.json")
        self.assertEqual(record["schema_version"], 2)
        self.assertEqual(record["tokens"], {"input_tokens": 10, "output_tokens": 2})
        self.assertEqual(record["plan_version"], 0)
        self.assertLessEqual(record["started_at"], record["finished_at"])
        self.reflect(self.engine.status(self.run_id))
        reflection = read_json(self.run / "reviews" / "01" / "reflection.json")
        self.assertEqual((reflection["previous_plan_version"], reflection["plan_version"],
                          reflection["lines_added"], reflection["lines_removed"]), (0, 1, 2, 0))
        # A failed attempt (the second reviewer, OpenCode) keeps the counts its CLI printed,
        # though its answer was not accepted.
        step = {"type": "step_finish", "part": {"tokens": {"input": 5, "cache": {"read": 3}}}}
        self.submit_with_output(None, lines=[step, usage], error=ReviewerError("invalid answer"))
        failed = read_json(self.run / "reviews" / "02" / "error.json")
        self.assertEqual((failed["tokens"], failed["plan_version"]), ({"cache.read": 3, "input": 5}, 1))
        self.assertIn("started_at", failed)
        self.assertIn("finished_at", failed)

    def test_a_recovered_review_keeps_its_time_and_tokens(self):
        self.ready()
        def interrupted(prepared, validate):
            work_dir = prepared.work_dir
            work_dir.mkdir(parents=True, exist_ok=True)
            (work_dir / "stdout.jsonl").write_text(json.dumps(
                {"type": "turn.completed", "usage": {"input_tokens": 7}}) + "\n", encoding="utf-8")
            raise KeyboardInterrupt
        with patch(RUN_REVIEW, side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.engine.review(self.run_id)
        self.engine.recover(self.run_id, confirmed=True, expected_review=1)
        record = read_json(self.run / "reviews" / "01" / "recovery.json")
        self.assertEqual((record["tokens"], record["plan_version"]), ({"input_tokens": 7}, 0))
        self.assertLessEqual(record["started_at"], record["finished_at"])

    def test_line_counts_are_counted_once_and_exactly(self):
        count = engine_module.count_lines
        self.assertEqual(count("a\nb\nc\n", "a\nB\nc\nd\n"), (2, 1))
        # Lines starting like diff headers are ordinary lines.
        self.assertEqual(count("x\n--- y\n++ z\n", "x\n+++ y\n-- z\n"), (2, 2))
        # A one-line change among many repeated lines stays one line, quickly.
        repeated = "## Heading\n- the same bullet item\n" * 8000
        changed = repeated.replace("- the same bullet item\n", "- changed\n", 1)
        started = __import__("time").monotonic()
        self.assertEqual(count(repeated, changed), (1, 1))
        self.assertLess(__import__("time").monotonic() - started, 1.0)
        # The summary reads the stored numbers and never compares versions again.
        self.ready()
        self.reflect(self.submit(review_value("changes_required")))
        with patch.object(engine_module, "count_lines", side_effect=AssertionError("compared again")):
            summary = self.engine.report(self.run_id)["review_summary"]
        self.assertEqual(summary["reviews"][0]["fix"]["version_change"],
                         {"from": "v000", "to": "v001", "lines_added": 2, "lines_removed": 0})

    def test_versions_are_labelled_from_records_even_when_content_returns(self):
        self.ready()
        first = Path(self.engine.status(self.run_id)["current_plan"]).read_text(encoding="utf-8")
        self.reflect(self.submit(review_value("changes_required")))  # v001 differs
        state = self.submit(review_value("changes_required"))
        plan = self.draft("revised.md", first)  # v002 has v000's content again
        reflection = self.draft("reflection.json", {"review_number": 2, "summary": "Went back to the first text.",
                                                    "decisions": [{"finding_id": "F1", "action": "accept",
                                                                   "reason": "The first text was right."}]})
        self.engine.apply(self.run_id, plan, reflection, state["plan_hash"])
        summary = self.engine.report(self.run_id)["review_summary"]
        self.assertEqual([entry["version"]["label"] for entry in summary["reviews"]], ["v000", "v001"])
        self.assertEqual(summary["reviews"][1]["fix"]["version_change"]["to"], "v002")
        self.assertEqual(summary["approval"]["current_version"]["label"], "v002")

    def test_accepting_at_the_limit_ends_as_accepted_with_the_current_plan(self):
        asked = self.at_the_limit()
        self.assertEqual(asked["review_summary"]["status"], "awaiting_human")
        self.assertIn("Status: awaiting_human (review limit question)", asked["review_summary_markdown"])
        accepted = self.at_limit("accept")
        self.assertEqual((accepted["status"], accepted["review_limit"]), ("accepted", 2))
        self.assertIsNotNone(accepted["finished_at"])
        # The last revision was never reviewed, and is the version accepted.
        self.assertFalse(accepted["final_plan_reviewed"])
        answer = read_json(self.run / f"escalations/{asked['escalation']['id']}-answer.json")
        self.assertEqual((answer["decision"], answer["plan_hash"]), ("accept", accepted["plan_hash"]))
        self.assertIn("created_at", answer)
        report = self.engine.report(self.run_id)
        self.assertFalse(report["converged"])
        self.assertEqual(report["human_acceptance"], {
            "event_ids": answer["event_ids"], "answer": answer["answer"], "version": accepted["plan_hash"],
            "accepted_at": answer["created_at"], "record": f"escalations/{asked['escalation']['id']}-answer.json"})
        self.assertTrue(report["warning"].startswith(engine_module.ACCEPTED_WARNING))
        summary = report["review_summary"]
        self.assertFalse(summary["approval"]["all_reviewers_approved"])
        self.assertEqual(summary["approval"]["human_acceptance"], report["human_acceptance"])
        self.assertEqual(summary["human_decisions"][0]["decision"], "accept")
        self.assertIn("- Status: accepted — accepted by the human", report["review_summary_markdown"])
        # Nothing is written into report.json that is rendered for display.
        self.assertNotIn("review_summary_markdown", read_json(self.run / "report.json"))

    def test_the_summary_is_in_status_only_at_the_limit_question_and_after_the_end(self):
        self.ready()
        self.assertNotIn("review_summary", self.engine.status(self.run_id))
        self.submit(review_value("human_required", human=True))
        self.assertNotIn("review_summary", self.engine.status(self.run_id))  # another question
        self.engine.cancel(self.run_id)
        ended = self.engine.status(self.run_id)
        self.assertEqual((ended["review_summary"]["status"], ended["review_summary_error"]), ("cancelled", None))

    def test_a_summary_exists_for_every_end_including_runs_without_a_plan(self):
        self.start_interview()
        cancelled = self.engine.cancel(self.run_id)
        summary = cancelled["review_summary"]
        self.assertEqual((summary["approval"]["current_version"], summary["reviews"]),
                         ({"id": None, "label": "none (no plan yet)"}, []))
        self.assertIn("No reviews yet.", cancelled["review_summary_markdown"])
        self.assertEqual(self.engine.report(self.run_id)["review_summary"]["status"], "cancelled")

    def test_an_exhausted_run_has_a_summary_and_report(self):
        self.at_the_limit()
        stopped = self.at_limit("stop")
        self.assertEqual(stopped["review_summary"]["status"], "exhausted")
        report = self.engine.report(self.run_id)
        self.assertEqual(report["review_summary"]["remaining_findings"]["count"], 2)
        self.assertTrue(report["warning"].startswith("Not a converged plan."))

    def test_a_summary_that_cannot_be_built_is_reported_not_raised(self):
        self.completed()
        with patch.object(engine_module, "summary_source", side_effect=DevFlowError("damaged")):
            status = self.engine.status(self.run_id)
            report = self.engine.report(self.run_id)
        self.assertEqual((status["review_summary"], status["review_summary_markdown"]), (None, None))
        self.assertIn("damaged", status["review_summary_error"])
        self.assertIn("damaged", report["review_summary_error"])

    def test_an_oversized_summary_is_left_out_of_report_json_but_shown(self):
        self.completed()
        with patch.object(engine_module, "check_json_size", side_effect=DevFlowError("too large")):
            report = self.engine.report(self.run_id)
        saved = read_json(self.run / "report.json")
        self.assertEqual((saved["review_summary"], saved["review_summary_error"]),
                         (None, engine_module.summary.TOO_LARGE))
        self.assertIsNotNone(report["review_summary"])
        self.assertIn("DevFlow review summary", report["review_summary_markdown"])

    # -- 0.7.2: the short summary's tool calls and question kinds ------------------------------
    def test_the_summary_counts_tool_calls_from_the_records(self):
        self.ready()
        self.reflect(self.submit(review_value("changes_required")))
        # A reviewer that did not run to the end recorded no tool calls.
        self.submit_with_output(None, error=ReviewerError("timed out"))
        self.engine.cancel(self.run_id)
        report = self.engine.report(self.run_id)
        self.assertEqual([entry["tool_call_count"] for entry in report["review_summary"]["reviews"]],
                         [ACTIVITY["tool_call_count"], None])
        text = report["review_summary_markdown"]
        self.assertIn("| 1 | codex | v000 | changes_required | medium 1; accept 1 | 3 |", text)
        self.assertIn("| 2 | opencode | v001 | failed | — | unknown |", text)
        self.assertIn("  - Review 2 (opencode): timed out", text)

    def test_every_question_records_its_kind_and_the_summary_shows_it(self):
        self.ready()
        self.submit(review_value("human_required", human=True))
        answer = self.limit_answer("Keep the five-minute limit.")
        resolved = self.resolve(self.draft("answer.json", {"answer": answer["answer_text"],
                                                           "event_ids": [answer["event_id"]]}))
        self.reflect(resolved)
        paused = self.engine.pause(self.run_id, self.draft("pause.json", {
            "reason": "A scope question.", "questions": ["Include the rollback?"]}))
        self.assertEqual(paused["escalation"]["detail"], {"review_number": 1, "kind": "plan_decision"})
        answer = self.limit_answer("Include it.")
        self.resolve(self.draft("answer.json", {"answer": answer["answer_text"], "event_ids": [answer["event_id"]]}))
        self.engine.cancel(self.run_id)
        report = self.engine.report(self.run_id)
        self.assertEqual([(item["review_number"], item["question_kind"], item["decision"])
                          for item in report["review_summary"]["human_decisions"]],
                         [(1, "review_decision", None), (1, "plan_decision", None)])
        self.assertRegex(report["review_summary_markdown"],
                         r"- Human decisions:\n  - After 1 review, review_decision: answered \(.+\)\n"
                         r"  - After 1 review, plan_decision: answered \(.+\)\n$")

    def test_a_question_recorded_without_a_kind_is_a_question(self):
        # Before 0.7.2 only the review limit question recorded its kind.
        records = {"escalations/x.json": {"questions": ["Include the rollback?"], "detail": {"review_number": 2}},
                   "escalations/x-answer.json": {"created_at": "2026-11-05T00:00:00+00:00", "answer": "Yes.",
                                                 "event_ids": ["e1"]}}
        decisions, acceptance = engine_module.human_decisions(
            {"resolved_escalations": [{"id": "x", "review_number": 2}]}, records.__getitem__)
        self.assertEqual((decisions[0]["review_number"], decisions[0]["question_kind"], acceptance), (2, None, None))

    def test_completed_publication_writes_the_approval_record(self):
        self.completed("/devflow:plan REVIEW-10")
        published = self.engine.publish(self.run_id)
        publication = published["publication"]
        self.assertEqual(publication["approval_path"], "docs/plans/REVIEW-10.approval.md")
        receipt = read_json(self.run / "publication.json")
        self.assertEqual((receipt["schema_version"], receipt["approval"], receipt["approval_path"]),
                         (2, "reviewers", "docs/plans/REVIEW-10.approval.md"))
        self.assertEqual(receipt["published_at"], publication["intent_at"])
        state = read_json(self.run / "state.json")
        record = (self.project / "docs" / "plans" / "REVIEW-10.approval.md").read_text(encoding="utf-8")
        self.assertEqual(record, "\n".join([
            "# DevFlow plan approval record", "",
            "This file records how the plan beside it was approved. The plan file is the exact approved bytes "
            "and is not changed by DevFlow.", "",
            "- Plan: REVIEW-10.md", "- Approval: reviewers", "- Approved by: codex, opencode",
            f"- Approved plan SHA-256: {state['plan_hash']}", f"- Run ID: {self.run_id}",
            f"- Approved at: {state['finished_at']}", f"- Published at: {publication['intent_at']}",
            "- Reviews: 2 (limit 6)", "- Remaining findings: 0", "- Last version reviewed: yes"]) + "\n")
        self.assertEqual((self.run / "approval.md").read_text(encoding="utf-8"), record)
        report = self.engine.report(self.run_id)
        self.assertTrue(report["published_plan_matches_at_report"])
        self.assertTrue(report["published_approval_matches_at_report"])
        self.assertIsNone(report["warning"])

    def test_an_accepted_plan_is_published_as_a_human_approval(self):
        self.at_the_limit()
        accepted = self.at_limit("accept")
        published = self.engine.publish(self.run_id)
        target = self.project / published["publication"]["path"]
        self.assertEqual(target.read_bytes(), Path(accepted["current_plan"]).read_bytes())
        receipt = read_json(self.run / "publication.json")
        self.assertEqual(receipt["approval"], "human")
        record = (self.project / published["publication"]["approval_path"]).read_text(encoding="utf-8")
        answer = read_json(self.run / f"escalations/{accepted['resolved_escalations'][-1]['id']}-answer.json")
        self.assertIn("- Approval: human\n", record)
        self.assertIn(f"- Approved by: human (AskUserQuestion answer event {answer['event_ids'][0]})\n", record)
        self.assertIn("- Remaining findings: 2\n- Last version reviewed: no\n", record)
        self.assertTrue(record.endswith(
            "- Note: accepted by the human at the review limit, not approved by every reviewer.\n"))
        self.assertEqual(self.engine.report(self.run_id)["warning"], engine_module.ACCEPTED_WARNING)

    def test_only_completed_or_accepted_plans_are_published(self):
        self.at_the_limit()
        self.at_limit("stop")
        with self.assertRaisesRegex(DevFlowError, "or the plan the human accepted"):
            self.engine.publish(self.run_id)

    def test_publication_names_need_both_the_plan_and_its_record_free(self):
        for existing in ("REVIEW-10.md", "REVIEW-10.approval.md"):
            with self.subTest(existing=existing):
                self.setUp()
                self.completed("/devflow:plan REVIEW-10")
                directory = self.project / "docs" / "plans"
                directory.mkdir(parents=True)
                (directory / existing).write_text("Someone else's file\n", encoding="utf-8")
                published = self.engine.publish(self.run_id)["publication"]
                self.assertEqual((published["path"], published["approval_path"]),
                                 (f"docs/plans/REVIEW-10-{self.run_id}.md",
                                  f"docs/plans/REVIEW-10-{self.run_id}.approval.md"))
                self.assertEqual((directory / existing).read_text(encoding="utf-8"), "Someone else's file\n")
        self.setUp()
        self.completed("/devflow:plan REVIEW-10")
        directory = self.project / "docs" / "plans"
        directory.mkdir(parents=True)
        (directory / "REVIEW-10.approval.md").write_text("x\n", encoding="utf-8")
        (directory / f"REVIEW-10-{self.run_id}.md").write_text("y\n", encoding="utf-8")
        with self.assertRaisesRegex(DevFlowError, "or their approval records"):
            self.engine.publish(self.run_id)

    def test_a_removed_or_changed_approval_record_is_never_rewritten(self):
        self.completed("/devflow:plan REVIEW-10")
        self.engine.publish(self.run_id)
        record = self.project / "docs" / "plans" / "REVIEW-10.approval.md"
        record.write_text("Changed by hand\n", encoding="utf-8")
        report = self.engine.report(self.run_id)
        self.assertEqual((report["published_plan_matches_at_report"], report["published_approval_matches_at_report"]),
                         (True, False))
        self.assertIn("approval record is missing or changed", report["warning"])
        with self.assertRaisesRegex(DevFlowError, "it was not overwritten"):
            self.engine.publish(self.run_id)
        self.assertEqual(record.read_text(encoding="utf-8"), "Changed by hand\n")
        record.unlink()
        with self.assertRaisesRegex(DevFlowError, "approval record was removed"):
            self.engine.publish(self.run_id)
        self.assertFalse(record.exists())

    def test_accepted_runs_keep_the_acceptance_warning_with_publication_warnings(self):
        self.at_the_limit()
        self.at_limit("accept")
        pending = self.engine.report(self.run_id)["warning"]
        self.assertEqual(pending, engine_module.ACCEPTED_WARNING + " Reviews completed, but no successful "
                         "publication has been recorded. Run plan publish or resolve the reported error.")
        directory = self.project / "docs" / "plans"
        directory.mkdir(parents=True)
        with patch.object(engine_module, "write_new", side_effect=OSError("disk full")):
            with self.assertRaises(DevFlowError):
                self.engine.publish(self.run_id)
        failed = self.engine.report(self.run_id)["warning"]
        self.assertTrue(failed.startswith(engine_module.ACCEPTED_WARNING + " Reviews completed"))
        published = self.engine.publish(self.run_id)
        (self.project / published["publication"]["path"]).write_text("changed\n", encoding="utf-8")
        changed = self.engine.report(self.run_id)["warning"]
        self.assertEqual(changed, engine_module.ACCEPTED_WARNING + " The plan was published, but its current file "
                         "or approval record is missing or changed. Inspect before implementation.")

    def test_the_cli_version_names_its_unhashed_source(self):
        self.ready()
        self.submit_with_output(review_value("changes_required"))
        entry = self.engine.report(self.run_id)["review_summary"]["reviews"][0]
        self.assertEqual((entry["cli_version"], entry["cli_version_source"]),
                         ("codex-cli 9.9.9", "review-processes/01/invocation.json"))

    def test_deeply_nested_process_output_never_stops_a_review_or_recovery(self):
        deep = "[" * 100000 + "]" * 100000 + "\n"
        usage = {"type": "turn.completed", "usage": {"input_tokens": 3}}
        self.ready()
        finished = self.submit_with_output(review_value("changes_required"), lines=[usage], raw=deep)
        self.assertEqual(finished["status"], "awaiting_revision")
        self.assertEqual(read_json(self.run / "reviews" / "01" / "review.json")["tokens"], {"input_tokens": 3})
        self.reflect(finished)
        def interrupted(prepared, validate):
            work_dir = prepared.work_dir
            work_dir.mkdir(parents=True, exist_ok=True)
            (work_dir / "stdout.jsonl").write_text(deep, encoding="utf-8")
            (work_dir / "invocation.json").write_text(deep, encoding="utf-8")
            raise KeyboardInterrupt
        with patch(RUN_REVIEW, side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.engine.review(self.run_id)
        recovered = self.engine.recover(self.run_id, confirmed=True, expected_review=2)
        self.assertIsNone(recovered["inflight"])
        self.assertIsNone(read_json(self.run / "reviews" / "02" / "recovery.json")["tokens"])
        self.assertIsNone(self.engine.report(self.run_id)["review_summary"]["reviews"][1]["cli_version"])

    def test_a_refused_retry_keeps_reporting_the_earlier_publication(self):
        self.completed("/devflow:plan REVIEW-10")
        self.engine.publish(self.run_id)
        (self.project / "docs" / "plans" / "REVIEW-10.approval.md").unlink()
        with self.assertRaisesRegex(DevFlowError, "approval record was removed"):
            self.engine.publish(self.run_id)
        report = self.engine.report(self.run_id)
        self.assertEqual(report["publication"]["status"], "error")
        self.assertEqual((report["published_plan_matches_at_report"], report["published_approval_matches_at_report"]),
                         (True, False))
        self.assertTrue(report["published_approval"].endswith("REVIEW-10.approval.md"))
        self.assertEqual(report["warning"], "The plan was published, but its current file or approval record is "
                         "missing or changed. Inspect before implementation.")

    def test_metadata_a_record_cannot_hold_never_stops_a_review_or_recovery(self):
        bad = {"type": "turn.completed", "usage": {"\ud800": 5, "input_tokens": 2}}
        self.ready()
        finished = self.submit_with_output(review_value("changes_required"), lines=[bad])
        self.assertEqual(finished["status"], "awaiting_revision")
        self.assertEqual(read_json(self.run / "reviews" / "01" / "review.json")["tokens"], {"input_tokens": 2})
        self.reflect(finished)
        def interrupted(prepared, validate):
            work_dir = prepared.work_dir
            work_dir.mkdir(parents=True, exist_ok=True)
            (work_dir / "stdout.jsonl").write_text(json.dumps({"type": "step_finish", "part": {"tokens": {"\ud800": 1}}}),
                                                   encoding="utf-8")
            (work_dir / "invocation.json").write_text(json.dumps({"cli_version": "\ud800"}), encoding="utf-8")
            raise KeyboardInterrupt
        with patch(RUN_REVIEW, side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.engine.review(self.run_id)
        recovered = self.engine.recover(self.run_id, confirmed=True, expected_review=2)
        self.assertIsNone(recovered["inflight"])
        self.assertIsNone(read_json(self.run / "reviews" / "02" / "recovery.json")["tokens"])
        self.assertIsNone(self.engine.report(self.run_id)["review_summary"]["reviews"][1]["cli_version"])

    def test_token_counts_that_cannot_be_written_never_stop_a_review_or_recovery(self):
        huge = "".join('{"type": "turn.completed", "thread_id": "%s", "usage": {"input_tokens": %s}}\n' % (thread, "9" * 4300)
                       for thread in ("main", "sub"))
        self.ready()
        finished = self.submit_with_output(review_value("changes_required"), raw=huge)
        self.assertEqual(finished["status"], "awaiting_revision")
        self.assertIsNone(read_json(self.run / "reviews" / "01" / "review.json")["tokens"])
        self.reflect(finished)
        def interrupted(prepared, validate):
            work_dir = prepared.work_dir
            work_dir.mkdir(parents=True, exist_ok=True)
            (work_dir / "stdout.jsonl").write_text(huge, encoding="utf-8")
            raise KeyboardInterrupt
        with patch(RUN_REVIEW, side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.engine.review(self.run_id)
        recovered = self.engine.recover(self.run_id, confirmed=True, expected_review=2)
        self.assertIsNone(recovered["inflight"])

    def test_reviewer_text_a_record_cannot_hold_never_stops_a_save(self):
        self.ready()
        for number, kind in enumerate(UNWRITABLE, start=1):
            with self.subTest(kind=kind):
                with patch(RUN_REVIEW, side_effect=lambda *args, kind=kind, **kwargs: raise_unwritable(kind)):
                    failed = self.engine.review(self.run_id)
                self.assertEqual(failed["status"], "paused_error")
                record = read_json(self.run / "reviews" / f"{number:02d}" / "error.json")
                self.assertEqual(record["message"], UNWRITABLE[kind])
                self.assertEqual(failed["last_error"]["message"], UNWRITABLE[kind])
                json.dumps(self.engine.status(self.run_id), ensure_ascii=False).encode("utf-8")  # runner output
                self.resume(self.session)
        # A successful review whose activity names cannot be held counts them as unnamed.
        with patch(RUN_REVIEW, return_value={"review": validate_review(review_value("changes_required")),
                                             "activity": unwritable_activity()}):
            reviewed = self.engine.review(self.run_id)
        self.assertEqual(read_json(self.run / "reviews" / "05" / "review.json")["activity"]["tool_calls"], {"(unnamed)": 1})
        self.reflect(reviewed)
        # A late failure (after recovery) is kept with its text escaped.
        def recovered_then_failed(*args, **kwargs):
            self.engine.recover(self.run_id, confirmed=True, expected_review=6)
            raise_unwritable("codex turn failure")
        with patch(RUN_REVIEW, side_effect=recovered_then_failed):
            with self.assertRaisesRegex(DevFlowError, "stale review"):
                self.engine.review(self.run_id)
        late = list((self.run / "late").glob("review-006-*.json"))
        self.assertEqual(len(late), 1)
        self.assertEqual(read_json(late[0])["error"], UNWRITABLE["codex turn failure"])
        cancelled = self.engine.cancel(self.run_id)
        self.assertEqual(cancelled["status"], "cancelled")
        json.dumps(self.engine.report(self.run_id), ensure_ascii=False).encode("utf-8")


class PublishNameTests(unittest.TestCase):
    RUN = "20261106T050301Z-0017ac3e5025"

    def test_publish_name(self):
        cases = [
            # The default: the first ticket ID, or plan-<run-id> without one.
            (FILENAME_DEFAULT, "REVIEW-10 の計画", "REVIEW-10"),
            (FILENAME_DEFAULT, "ABC-1 then DEF-2", "ABC-1"),
            (FILENAME_DEFAULT, "計画だけ", "plan-" + self.RUN),
            # {ticket} without a ticket ID makes the whole name plan-<run-id>.
            ("{ticket}-plan", "REVIEW-10", "REVIEW-10-plan"),
            ("{run_id}-{ticket}", "計画だけ", "plan-" + self.RUN),
            ("{run_id}-{ticket}", "REVIEW-10", self.RUN + "-REVIEW-10"),
            # Without {ticket}, the ticket ID is not used.
            ("plan-{run_id}", "REVIEW-10", "plan-" + self.RUN),
            ("plan", "REVIEW-10", "plan"),
        ]
        for template, request, name in cases:
            with self.subTest(template=template, request=request):
                self.assertEqual(publish_name(template, request, self.RUN), name)

    def test_safe_name_is_the_directory_component_rule(self):
        for part in ("docs", "plans", "_drafts", "v1.2-x", "COM0", "CONSOLE", "REVIEW-10", self.RUN):
            with self.subTest(part=part):
                self.assertTrue(safe_name(part))
        for part in ("", ".", "..", ".git", ".devflow", ".claude", ".codex", ".opencode", ".agents", "CON", "con",
                     "nul.md", "COM1.x", "LPT9", "x.", "-x", ".x", "a b", "a/b", "a\\b", "C:", "{run_id}", "é"):
            with self.subTest(part=part):
                self.assertFalse(safe_name(part))


if __name__ == "__main__":
    unittest.main()
