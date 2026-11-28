"""review_limit_auto: an Implement run applies the host's recommendation at the review limit (limit-auto)."""
from __future__ import annotations

from unittest.mock import patch
import json
import os
import shutil
import subprocess
import unittest

from devflow_core.common import git as git_module
from devflow_core.common import runs as runs_module
from devflow_core.common.errors import DevFlowError, ProcessCleanupError
from devflow_core.common.process import Completed
from devflow_core.common.review import REVIEW_LIMIT_REASON
from devflow_core.common.settings import read_project_settings
from devflow_core.common.storage import atomic_write_json, digest, json_bytes, read_json
from devflow_core.implement.config import load_config, validate_config
from devflow_core.implement.engine import Engine
from devflow_core.workflows import WORKFLOWS
from tests.implement.test_engine import EngineFixture


class ConfigLayeringTests(EngineFixture):
    """The shared settings' precedence and strict validation for Implement."""

    def load(self):
        return load_config(read_project_settings(self.project, "claude", WORKFLOWS), WORKFLOWS["implement"])

    def test_defaults(self):
        config = self.load()
        self.assertEqual((config["review_limit_auto"], config["review_limit_auto_extra_reviews"],
                          config["review_limit_auto_final"]), ("ask", 6, "ask"))

    def test_shared_top_level_and_flow_overrides(self):
        atomic_write_json(self.project / "devflow.config.json",
                          {"schema_version": 1, "review_limit_auto": "continue", "review_limit_auto_extra_reviews": 2,
                           "implement": {**self.config, "review_limit_auto": "continue_accept"}})
        config = self.load()
        self.assertEqual((config["review_limit_auto"], config["review_limit_auto_extra_reviews"]),
                         ("continue_accept", 2))

    def test_local_overrides_shared(self):
        self.config["review_limit_auto"] = "continue_accept"
        self.write_config()
        atomic_write_json(self.project / "devflow.config.local.json",
                          {"schema_version": 1, "implement": {"review_limit_auto": "ask"}})
        self.assertEqual(self.load()["review_limit_auto"], "ask")

    def test_only_the_exact_types_are_accepted(self):
        for key, bad, message in (("review_limit_auto", "auto", "must be"), ("review_limit_auto", True, "must be"),
                                  ("review_limit_auto", 1, "must be"),
                                  ("review_limit_auto_extra_reviews", 0, "at least 1"),
                                  ("review_limit_auto_extra_reviews", -1, "at least 1"),
                                  ("review_limit_auto_extra_reviews", 1.5, "at least 1"),
                                  ("review_limit_auto_extra_reviews", "6", "at least 1"),
                                  ("review_limit_auto_extra_reviews", True, "at least 1"),
                                  ("review_limit_auto_final", "continue", '"ask" or "accept"'),
                                  ("review_limit_auto_final", True, '"ask" or "accept"'),
                                  ("review_limit_auto_final", None, '"ask" or "accept"')):
            with self.subTest(key=key, value=bad):
                with self.assertRaisesRegex(DevFlowError, message):
                    validate_config({**self.load(), key: bad})
        self.config["review_limit_auto"] = "enabled"
        self.write_config()
        with self.assertRaises(DevFlowError):
            self.load()


class LimitAutoTests(EngineFixture):
    def setUp(self):
        super().setUp()
        self.config["review_limit_auto"] = "continue_accept"
        self.config["max_reviews"] = 2
        self.write_config()
        self.severity = "medium"
        self.alter = lambda review: [finding.update(severity=self.severity) for finding in review["findings"]]

    def to_limit(self):
        """Two handled reviews at max_reviews 2; ready_review with a validated candidate."""
        self.start()
        self.ready()
        self.response_kind = "changes_required"
        for _ in range(2):
            self.review()
            self.reflect()
            self.ready()
        self.assertEqual(self.state()["status"], "ready_review")
        self.assertEqual(len(self.state()["attempts"]), 2)

    def question(self):
        asked = self.review()
        self.assertEqual(asked["status"], "awaiting_human")
        self.assertIn(REVIEW_LIMIT_REASON, asked["escalation"]["reason"])
        return asked

    def limit_auto(self, decision, text="Recommendation: accept and finish; no serious issue remains.",
                   escalation_id=None, session=None, path=None):
        escalation_id = escalation_id or self.engine.status(self.run_id)["escalation"]["id"]
        path = path or self.draft_text("recommendation.md", text)
        return self.engine.limit_auto(self.run_id, session or self.session, escalation_id, decision, path)

    def records(self):
        return sorted(name for name in self.state()["artifacts"] if name.startswith("limit-auto/"))

    def assertRefusedUnchanged(self, call, pattern):
        before = (self.run / "state.json").read_bytes()
        with self.assertRaisesRegex(DevFlowError, pattern):
            call()
        self.assertEqual((self.run / "state.json").read_bytes(), before)

    def test_ask_keeps_the_human_question(self):
        self.config.pop("review_limit_auto")
        self.write_config()
        self.to_limit()
        self.question()
        for decision in ("continue", "accept"):
            self.assertRefusedUnchanged(lambda: self.limit_auto(decision), 'review_limit_auto is "ask"')
        self.assertEqual(self.at_limit("stop")["status"], "exhausted")

    def test_continue_returns_to_ready_review_without_a_human_answer(self):
        self.config["review_limit_auto"] = "continue"
        self.write_config()
        self.to_limit()
        asked = self.question()
        calls = self.reviewer.call_count
        result = self.limit_auto("continue", "Recommendation: continue.")
        self.assertEqual(self.reviewer.call_count, calls)  # no review starts
        self.assertEqual((result["status"], result["review_limit"]), ("ready_review", 4))
        self.assertEqual(result["resolutions"], [])
        self.assertEqual(asked["escalation"]["id"], "E001")
        self.assertEqual(self.records(), ["limit-auto/E001-continue.json"])
        record = read_json(self.run / "limit-auto/E001-continue.json")
        self.assertEqual((record["version"], record["version_reviewed"], record["review_limit_after"]),
                         (self.state()["candidate"]["snapshot_id"], True, 4))  # the reviewed checkpoint again
        self.assertRefusedUnchanged(lambda: self.limit_auto("accept", escalation_id="E001"),
                                    "no pending review limit question")
        self.review()
        self.reflect()
        self.ready()
        self.review()
        self.reflect()
        self.ready()
        asked = self.question()
        self.assertRefusedUnchanged(lambda: self.limit_auto("accept"), "does not accept automatically")
        self.assertIn("continue by setting continue (limit 2 → 4)", asked["review_summary_markdown"])

    def test_accept_ends_auto_accepted_with_its_report(self):
        self.to_limit()
        # A checkpoint after the last review: the accepted snapshot was never reviewed.
        self.source.write_text("VALUE = 7\n", encoding="utf-8")
        self.checkpoint()
        self.assertEqual(self.engine.check(self.run_id, self.session)["status"], "ready_review")
        self.question()
        result = self.limit_auto("accept")
        self.assertEqual(result["status"], "auto_accepted")
        self.assertEqual(self.records(), ["limit-auto/E001-accept.json"])
        record = read_json(self.run / "limit-auto/E001-accept.json")
        self.assertEqual((record["version"], record["version_reviewed"], record["review_number"]),
                         (self.state()["candidate"]["snapshot_id"], False, 2))
        self.assertFinalReportSaved(result)
        report = result["report"]
        self.assertEqual(report["auto_acceptance"]["record"], "limit-auto/E001-accept.json")
        self.assertEqual(report["workspace_snapshot_id_at_report"], record["version"])
        self.assertIsNone(report["human_acceptance"])
        self.assertIn("review_limit_auto setting following the host's recommendation", report["warning"])
        self.assertIn("never reviewed", report["warning"])
        self.assertIn("accept by setting continue_accept (", result["review_summary_markdown"])
        # The final report quotes the recommendation the host recorded, so the human reads it.
        self.assertIn("    Host's recommendation:\n    > Recommendation: accept and finish; no serious issue remains.\n",
                      result["review_summary_markdown"])
        with self.assertRaises(DevFlowError):
            self.review()

    def test_stop_and_cancel_are_never_automatic(self):
        self.to_limit()
        self.question()
        for decision in ("stop", "cancel"):
            self.assertRefusedUnchanged(lambda: self.limit_auto(decision), "continue or accept")

    def test_refusals(self):
        self.to_limit()
        self.assertRefusedUnchanged(lambda: self.limit_auto("continue", escalation_id="E001"),
                                    "no pending review limit question")
        self.question()
        self.assertRefusedUnchanged(lambda: self.limit_auto("continue", escalation_id="E002"),
                                    "no pending review limit question")
        self.assertRefusedUnchanged(lambda: self.limit_auto("continue", session="another-session"),
                                    "does not own the run")
        self.assertRefusedUnchanged(lambda: self.limit_auto("continue", text=""), "nonempty text")
        outside = self.project / "recommendation.md"
        outside.write_text("Recommendation: continue.", encoding="utf-8")
        self.assertRefusedUnchanged(lambda: self.limit_auto("continue", path=outside), "")
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "another-conversation"}):
            code = self.run_cli("limit-auto", "--escalation-id", "E001", "--decision", "continue",
                                "--recommendation-file", str(self.draft_text("r.md", "Recommendation: continue.")))
        self.assertNotEqual(code, 0)
        self.answer("Continue with two more reviews.")
        self.assertRefusedUnchanged(lambda: self.limit_auto("continue"), "human answer is recorded")
        self.assertEqual(self.records(), [])

    def test_a_stale_id_another_kind_of_question_and_an_answer_before_a_transfer(self):
        self.to_limit()
        self.question()
        self.limit_auto("continue", "Recommendation: continue.")
        # A question of another kind is open: limit-auto answers no other question.
        asked = self.escalate()
        self.assertNotEqual(asked["escalation"]["detail"]["kind"], "review_budget")
        self.assertRefusedUnchanged(lambda: self.limit_auto("continue", escalation_id=asked["escalation"]["id"]),
                                    "no pending review limit question")
        evidence = self.answer("Implement VALUE = 1.")
        self.engine.resolve(self.run_id, self.session, evidence, "continue", "The user chose VALUE = 1.")
        for _ in range(2):
            self.review()
            self.reflect()
            self.ready()
        second = self.question()["escalation"]["id"]
        self.assertNotEqual(second, "E001")
        # A delayed call with the previous review limit question's ID is refused.
        self.assertRefusedUnchanged(lambda: self.limit_auto("continue", escalation_id="E001"),
                                    "no pending review limit question")
        # An answer recorded before the run moved to another session is not the new owner's answer.
        self.answer("An answer before the transfer.")
        self.assertRefusedUnchanged(lambda: self.limit_auto("continue"), "human answer is recorded")
        self.assertEqual(self.resume("another-session")["human_events"], [])
        self.assertEqual(self.limit_auto("continue", "Recommendation: continue.")["review_limit"], 6)

    def test_an_inconsistent_question_record_refuses_before_any_change(self):
        for decision in ("continue", "accept"):
            with self.subTest(decision=decision):
                if decision == "accept":
                    self.setUp()
                self.to_limit()
                self.question()
                # The question record (with its registered digest) disagrees with the review count.
                self.tamper("escalations/001.json", lambda question: question["detail"].update(review_number=1))
                self.assertRefusedUnchanged(lambda: self.limit_auto(decision),
                                            "Invalid or mismatched automatic decision record")
                self.assertEqual(self.records(), [])

    def test_a_typed_frozen_value_is_checked_in_each_copy(self):
        for frozen, value in ((6, 6.0), (1, True)):
            self.setUp()
            self.config["review_limit_auto_extra_reviews"] = frozen
            self.write_config()
            self.to_limit()
            self.assertEqual(self.state()["config"]["review_limit_auto_extra_reviews"], frozen)
            for copy in ("config.json", "state.json"):
                with self.subTest(copy=copy, value=value):
                    original = (self.run / "config.json").read_bytes(), (self.run / "state.json").read_bytes()
                    state = self.engine.store.read(self.run_id)
                    if copy == "config.json":
                        config = dict(read_json(self.run / "config.json"), review_limit_auto_extra_reviews=value)
                        data = json_bytes(config)
                        (self.run / "config.json").write_bytes(data)
                        state["artifacts"]["config.json"] = digest(data)
                    else:
                        state["config"]["review_limit_auto_extra_reviews"] = value
                    # Equal to an integer in Python, but not the same JSON value.
                    atomic_write_json(self.run / "state.json", state)
                    with self.assertRaisesRegex(DevFlowError, "review_limit_auto_extra_reviews must be an integer"):
                        self.engine.read_frozen_config(self.run_id)
                    (self.run / "config.json").write_bytes(original[0])
                    (self.run / "state.json").write_bytes(original[1])

    def test_an_accept_record_naming_another_version_is_not_listed(self):
        for label, change in (("version", lambda record: record.update(version="0" * 64)),
                              ("schema version", lambda record: record.update(schema_version=True))):
            with self.subTest(change=label):
                self.setUp()
                self.to_limit()
                self.question()
                self.limit_auto("accept")
                self.tamper("limit-auto/E001-accept.json", change)
                status = self.engine.status(self.run_id)
                self.assertIsNone(status["limit_auto_decisions"])
                self.assertIn("cannot be verified", status["limit_auto_decisions_error"])

    def run_cli(self, *args):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from devflow_core import cli
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return cli.main(["--host", "claude", "--project", str(self.project), "--session", self.session,
                             "--run", self.run_id, "implement", *args])

    def test_a_serious_latest_review_blocks_accept(self):
        self.severity = "high"
        self.to_limit()
        self.question()
        self.assertRefusedUnchanged(lambda: self.limit_auto("accept"),
                                    r"codex's latest succeeded review \(review 1\); opencode's latest succeeded "
                                    r"review \(review 2\) has a critical or high finding")
        self.assertEqual(self.limit_auto("continue")["status"], "ready_review")

    def test_accept_needs_validation_the_checkpoint_and_a_succeeded_review(self):
        self.start()
        self.ready()
        self.reviewer.side_effect = DevFlowError("provider down")
        self.review()
        self.review()
        self.question()
        self.assertRefusedUnchanged(lambda: self.limit_auto("accept"), "no review succeeded")
        self.reviewer.side_effect = self.respond
        self.limit_auto("continue")
        self.response_kind = "changes_required"
        self.review()
        self.reflect()
        self.ready()
        self.review()
        self.reflect()
        self.ready()
        self.question()
        self.source.write_text("VALUE = 99\n", encoding="utf-8")  # the workspace left the checkpoint
        self.assertRefusedUnchanged(lambda: self.limit_auto("accept"), "Cannot accept: Code changed")
        self.source.write_text("VALUE = 1\n", encoding="utf-8")
        state = self.engine.store.read(self.run_id)
        state["validation"] = None
        atomic_write_json(self.run / "state.json", state)
        self.assertRefusedUnchanged(lambda: self.limit_auto("accept"), "no passing checks or human validation")

    def test_the_ceiling_ends_exhausted_without_a_question(self):
        self.to_limit()
        state = self.engine.store.read(self.run_id)
        template = state["attempts"][-1]
        while len(state["attempts"]) < 98:
            state["attempts"].append(dict(template, number=len(state["attempts"]) + 1))
        state["review_limit"] = 98
        atomic_write_json(self.run / "state.json", state)
        self.assertEqual(self.review()["status"], "exhausted")
        self.assertRefusedUnchanged(lambda: self.limit_auto("accept", escalation_id="E001"),
                                    "no pending review limit question")

    # review_limit_auto_final: extra 1 with two reviewers, so the first question is already exhausted.

    def final_config(self, final="accept"):
        self.config.update(review_limit_auto_extra_reviews=1, review_limit_auto_final=final)
        self.write_config()

    def test_final_accept_ends_auto_accepted_with_high_findings(self):
        from devflow_core.common import summary as summary_module
        self.final_config()
        self.severity = "high"
        self.to_limit()
        self.question()
        result = self.limit_auto("continue", "Recommendation: continue; both high findings need another review.")
        self.assertEqual(result["status"], "auto_accepted")
        self.assertEqual(self.records(), ["limit-auto/E001-accept.json"])
        record = read_json(self.run / "limit-auto/E001-accept.json")
        self.assertEqual((record["schema_version"], record["decision"], record["recommended"],
                          record["review_limit_auto_final"], record["version_reviewed"]),
                         (2, "accept", "continue", "accept", True))
        self.assertEqual(result["limit_auto_decisions"],
                         [{"escalation_id": "E001", "review_number": 2, "decision": "accept",
                           "record": "limit-auto/E001-accept.json", "recommended": "continue", "extra_reviews": 1}])
        self.assertFinalReportSaved(result)
        report = result["report"]
        serious = "codex (review 1: F1 high), opencode (review 2: F1 high)"
        self.assertEqual(report["warning"],
                         "Accepted at the review limit by the run's review_limit_auto_final setting because another "
                         "round would exceed review_limit_auto_extra_reviews, without approval from every reviewer; "
                         "the host recommended continue (its recommendation and reasons: limit-auto/E001-accept.json). "
                         f"Critical or high findings in the latest succeeded reviews: {serious}. Inspect remaining "
                         "findings and whether the final revision was reviewed.")
        self.assertEqual((report["auto_acceptance"]["recommended"], report["auto_acceptance"]["extra_reviews"]),
                         ("continue", 1))
        self.assertIsNone(report["review_summary_error"])  # the saved summary is shown, not hidden
        text = summary_module.render(report["review_summary"])
        self.assertIn("- Status: auto_accepted — accepted at the review limit by the run's review_limit_auto_final "
                      "setting because another round would exceed review_limit_auto_extra_reviews 1, not approved by "
                      "every reviewer\n", text)
        self.assertIn("- Auto-accepted by the review_limit_auto_final setting because another round would exceed "
                      "review_limit_auto_extra_reviews 1; the host recommended continue; critical/high in the latest "
                      f"succeeded reviews: {serious} (record: limit-auto/E001-accept.json; accepted version "
                      "reviewed)\n", text)
        self.assertIn("  - After 2 reviews, review_budget: accept by setting continue_accept with "
                      "review_limit_auto_final accept (another round would exceed review_limit_auto_extra_reviews 1; "
                      "the host recommended continue) (", text)
        self.assertEqual(text, result["review_summary_markdown"])

    def test_final_accept_keeps_the_validation_and_checkpoint_checks(self):
        self.final_config()
        self.severity = "high"
        self.to_limit()
        self.question()
        self.source.write_text("VALUE = 99\n", encoding="utf-8")  # the workspace left the checkpoint
        self.assertRefusedUnchanged(lambda: self.limit_auto("continue"), "Cannot accept: Code changed")
        self.source.write_text("VALUE = 1\n", encoding="utf-8")
        state = self.engine.store.read(self.run_id)
        state["validation"] = None
        atomic_write_json(self.run / "state.json", state)
        self.assertRefusedUnchanged(lambda: self.limit_auto("continue"), "no passing checks or human validation")
        self.assertEqual(self.records(), [])

    def test_a_record_1_report_keeps_its_saved_summary(self):
        # An accept without critical/high writes record 1, whose views keep the 0.13.0 shape.
        self.final_config()
        self.to_limit()
        self.question()
        self.limit_auto("accept")
        self.assertEqual(read_json(self.run / "limit-auto/E001-accept.json")["schema_version"], 1)
        report = self.engine.status(self.run_id)["report"]
        self.assertIsNone(report["review_summary_error"])
        self.assertEqual(set(report["review_summary"]["automatic_decisions"][0]),
                         {"at", "review_number", "decision", "setting", "review_limit_before", "review_limit_after",
                          "recommendation", "record"})
        self.assertEqual(set(report["auto_acceptance"]), {"record", "source", "version", "accepted_at",
                                                         "version_reviewed", "recommendation"})
        self.assertIn("review_limit_auto setting following the host's recommendation", report["warning"])
        # One changed item of the saved summary still hides it.
        path = self.state()["report_path"]
        self.tamper(path, lambda saved: saved["review_summary"]["automatic_decisions"][0].update(setting="continue"))
        report = self.engine.status(self.run_id)["report"]
        self.assertIsNone(report["review_summary"])
        self.assertIn("differ from the run's verified records", report["review_summary_error"])

    def test_the_ceiling_ends_exhausted_without_a_question_with_final_accept(self):
        self.final_config()
        self.to_limit()
        state = self.engine.store.read(self.run_id)
        template = state["attempts"][-1]
        while len(state["attempts"]) < 98:
            state["attempts"].append(dict(template, number=len(state["attempts"]) + 1))
        state["review_limit"] = 98
        atomic_write_json(self.run / "state.json", state)
        self.assertEqual(self.review()["status"], "exhausted")
        self.assertEqual(self.records(), [])

    def test_resume_keeps_the_saved_report_after_a_workspace_edit(self):
        self.to_limit()
        self.question()
        self.limit_auto("accept")
        original = self.engine.status(self.run_id)["report"]
        self.source.write_text("VALUE = 42  # edited after the run ended\n", encoding="utf-8")
        with patch("devflow_core.implement.snapshot.capture",
                   side_effect=RuntimeError("the workspace must not be captured")):
            resumed = self.resume("another-session")
        self.assertEqual(resumed["status"], "auto_accepted")
        self.assertEqual(resumed["report"], original)

    def tamper(self, name, change):
        record = read_json(self.run / name)
        change(record)
        data = json_bytes(record)
        (self.run / name).write_bytes(data)
        state = self.engine.store.read(self.run_id)
        state["artifacts"][name] = digest(data)
        atomic_write_json(self.run / "state.json", state)

    def test_a_changed_record_is_reported_and_hides_the_saved_summary(self):
        for label, change in (("reviewed flag as a number", lambda record: record.update(version_reviewed=0)),
                              ("setting", lambda record: record.update(review_limit_auto="continue"))):
            with self.subTest(change=label):
                self.setUp() if label != "reviewed flag as a number" else None
                self.to_limit()
                self.question()
                self.limit_auto("accept")
                self.tamper("limit-auto/E001-accept.json", change)
                status = self.engine.status(self.run_id)
                self.assertEqual(status["status"], "auto_accepted")
                self.assertIsNone(status["limit_auto_decisions"])
                self.assertIsNone(status["review_summary"])
                self.assertIsNone(status["report"]["auto_acceptance"])
                self.assertIn("automatic accept record", status["report"]["auto_acceptance_error"])
                self.assertIsNone(status["report"]["review_summary"])
                self.assertIn("automatic", status["report"]["review_summary_error"])

    def test_a_changed_continue_record_hides_the_saved_summary(self):
        self.config["review_limit_auto"] = "continue"
        self.write_config()
        self.to_limit()
        self.question()
        self.limit_auto("continue")
        self.tamper("limit-auto/E001-continue.json", lambda record: record.update(review_number=1))
        self.assertIsNone(self.engine.status(self.run_id)["limit_auto_decisions"])
        self.engine.cancel(self.run_id, self.session, "test")
        status = self.engine.status(self.run_id)
        self.assertIsNone(status["report"]["review_summary"])
        self.assertIn("automatic decision records cannot be verified", status["report"]["review_summary_error"])

    def test_a_saved_report_naming_an_unregistered_decision_hides_its_summary(self):
        self.config["review_limit_auto"] = "continue"
        self.write_config()
        self.to_limit()
        self.question()
        self.limit_auto("continue", "Recommendation: continue.")
        self.engine.cancel(self.run_id, self.session, "test")
        saved = self.engine.status(self.run_id)["report"]["review_summary"]["automatic_decisions"]
        self.assertEqual([item["record"] for item in saved], ["limit-auto/E001-continue.json"])
        state = self.engine.store.read(self.run_id)
        del state["artifacts"]["limit-auto/E001-continue.json"]  # the record is no longer registered
        atomic_write_json(self.run / "state.json", state)
        status = self.engine.status(self.run_id)
        self.assertEqual(status["limit_auto_decisions"], [])
        self.assertIsNone(status["report"]["review_summary"])
        self.assertIn("differ from the run's verified records", status["report"]["review_summary_error"])

    def test_a_saved_report_quoting_another_recommendation_hides_its_summary(self):
        # Same record name, a different recommendation with its digest registered again:
        # the saved summary would quote text the verified record no longer holds.
        self.to_limit()
        self.question()
        self.limit_auto("accept")
        self.tamper("limit-auto/E001-accept.json",
                    lambda record: record.update(recommendation="Recommendation: accept; another reason."))
        status = self.engine.status(self.run_id)
        self.assertEqual([item["record"] for item in status["limit_auto_decisions"]], ["limit-auto/E001-accept.json"])
        self.assertEqual(status["report"]["auto_acceptance"]["recommendation"], "Recommendation: accept; another reason.")
        self.assertIsNone(status["report"]["review_summary"])
        self.assertIn("differ from the run's verified records", status["report"]["review_summary_error"])

    def test_an_accept_record_changed_without_its_digest_is_refused(self):
        self.to_limit()
        self.question()
        self.limit_auto("accept")
        record = read_json(self.run / "limit-auto/E001-accept.json")
        record["recommendation"] = "Recommendation: accept; another reason."
        (self.run / "limit-auto/E001-accept.json").write_bytes(json_bytes(record))  # digest kept
        status = self.engine.status(self.run_id)
        self.assertIsNone(status["limit_auto_decisions"])
        self.assertIn("changed or is missing", status["limit_auto_decisions_error"])
        self.assertIsNone(status["report"]["auto_acceptance"])
        self.assertIsNone(status["report"]["review_summary"])

    def test_a_report_saved_without_its_summary_keeps_its_own_error(self):
        from devflow_core.common import summary as summary_module
        from devflow_core.implement import workflow as workflow_module
        real = workflow_module.check_json_size

        def refuse_reports(name, data):
            if name.startswith("reports/"):
                raise DevFlowError("over the limit")
            return real(name, data)

        self.config["review_limit_auto"] = "continue"
        self.write_config()
        self.to_limit()
        self.question()
        self.limit_auto("continue", "Recommendation: continue.")
        with patch.object(workflow_module, "check_json_size", side_effect=refuse_reports):
            self.engine.cancel(self.run_id, self.session, "test")
        status = self.engine.status(self.run_id)
        self.assertEqual([item["record"] for item in status["limit_auto_decisions"]], ["limit-auto/E001-continue.json"])
        self.assertIsNone(status["report"]["review_summary"])
        self.assertEqual(status["report"]["review_summary_error"], summary_module.TOO_LARGE)

    def test_an_interrupted_save_keeps_the_question_and_applies_once_on_retry(self):
        for decision in ("continue", "accept"):
            with self.subTest(decision=decision):
                if decision == "accept":
                    self.setUp()
                self.to_limit()
                self.question()
                original, tripped = runs_module.atomic_json, {"hit": False}

                def flaky(path, value):
                    if os.path.basename(path) == "state.json" and not tripped["hit"]:
                        tripped["hit"] = True
                        raise OSError("simulated interrupted state replacement")
                    return original(path, value)

                with patch.object(runs_module, "atomic_json", flaky):
                    with self.assertRaises(OSError):
                        self.limit_auto(decision)
                name = f"limit-auto/E001-{decision}.json"
                self.assertTrue((self.run / name).exists())  # written, never committed
                if decision == "accept":
                    # The terminal report was written with the decision, before the state replacement.
                    self.assertTrue(list((self.run / "reports").glob("final-*.json")))
                status = self.engine.status(self.run_id)
                self.assertEqual((status["status"], status["escalation"]["id"], status["review_limit"]),
                                 ("awaiting_human", "E001", 2))
                self.assertEqual(status["limit_auto_decisions"], [])
                self.assertIsNone(status["report_path"])
                self.assertNotIn("report", status)
                self.assertIsNone(self.state()["report_path"])
                result = self.limit_auto(decision)
                self.assertEqual(result["status"], "ready_review" if decision == "continue" else "auto_accepted")
                self.assertEqual(self.records(), [name])

    # -- process-cleanup failures at the limit --------------------------------------------
    def test_a_cleanup_failure_at_the_limit_pauses_when_automatic(self):
        for mode in ("continue", "continue_accept"):
            with self.subTest(mode=mode):
                self.setUp()
                self.config["review_limit_auto"] = mode
                self.write_config()
                self.to_limit()
                with patch.object(Engine, "_current", side_effect=ProcessCleanupError("a helper lingers")):
                    result = self.review()
                self.assertEqual(result["status"], "paused_error")
                self.assertIn("a helper lingers", result["last_error"])
                self.assertIsNone(result["escalation"])
                self.assertEqual(self.resume()["status"], "ready_review")
                self.assertEqual(self.review()["status"], "awaiting_human")

    def test_with_ask_a_cleanup_failure_in_the_limit_check_opens_nothing(self):
        self.config["review_limit_auto"] = "ask"
        self.write_config()
        self.to_limit()
        before = (self.run / "state.json").read_bytes()
        with patch.object(Engine, "_current", side_effect=ProcessCleanupError("a helper lingers")):
            with self.assertRaises(ProcessCleanupError):
                self.review()
        self.assertEqual((self.run / "state.json").read_bytes(), before)

    def test_a_post_review_cleanup_failure_at_the_last_review(self):
        for mode, expected in (("continue", "paused_error"), ("continue_accept", "paused_error"),
                               ("ask", "ready_review")):
            with self.subTest(mode=mode):
                self.setUp()
                self.config["review_limit_auto"] = mode
                self.write_config()
                self.start()
                self.ready()
                self.response_kind = "changes_required"
                self.review()
                self.reflect()
                self.ready()
                calls, original = [], Engine._current

                def wrapped(engine_self, state):
                    calls.append(1)
                    if len(calls) > 1:  # prepare's read passed; the post-review read fails
                        raise ProcessCleanupError("untracked helper lingers")
                    return original(engine_self, state)

                with patch.object(Engine, "_current", wrapped):
                    result = self.review()
                self.assertEqual(result["status"], expected)
                if mode == "ask":
                    # The next review checks the workspace again, then asks the human.
                    self.assertEqual(self.review()["status"], "awaiting_human")

    def test_a_cleanup_failure_while_accepting_keeps_the_question(self):
        self.to_limit()
        self.question()
        with patch.object(Engine, "_current", side_effect=ProcessCleanupError("a helper lingers")):
            self.assertRefusedUnchanged(lambda: self.limit_auto("accept"), "Cannot accept: a helper lingers")
        self.assertEqual(self.engine.status(self.run_id)["escalation"]["id"], "E001")

    def test_a_wrapped_interrupt_in_the_limit_workspace_check_saves_the_pause(self):
        self.to_limit()
        failure = ProcessCleanupError("a descendant retained output pipes")
        failure.__cause__ = KeyboardInterrupt()
        with patch("devflow_core.implement.workspace._inspect", side_effect=failure):
            with self.assertRaises(KeyboardInterrupt):
                self.review()
        state = self.state()
        self.assertEqual(state["status"], "paused_error")
        self.assertIn("retained output pipes", state["last_error"])
        self.assertEqual(len(state["attempts"]), 2)  # no attempt consumed

    def test_a_wrapped_interrupt_below_the_limit_is_not_a_pause(self):
        self.start()
        self.ready()
        failure = ProcessCleanupError("a descendant retained output pipes")
        failure.__cause__ = KeyboardInterrupt()
        with patch("devflow_core.implement.workspace._inspect", side_effect=failure):
            with self.assertRaises(KeyboardInterrupt):
                self.review()
        self.assertEqual(self.state()["status"], "ready_review")

    def test_manual_attestation_and_binary_gates_still_apply(self):
        self.config["checks"] = []
        self.write_config()
        self.start()
        self.assertEqual(self.ready()["status"], "awaiting_human")
        evidence = self.answer("Yes, I independently verified this version.")
        self.engine.resolve(self.run_id, self.session, evidence, "continue", "The user verified this version.")
        self.response_kind = "changes_required"
        for _ in range(2):
            self.review()
            self.reflect()
            self.source.write_text("VALUE = 2\n", encoding="utf-8")
            self.checkpoint()
            self.engine.check(self.run_id, self.session)
            evidence = self.answer("Yes, I independently verified this version.")
            self.engine.resolve(self.run_id, self.session, evidence, "continue", "The user verified this version.")
        asked = self.question()
        self.assertEqual(asked["escalation"]["id"], "E004")
        self.assertEqual(self.limit_auto("accept")["status"], "auto_accepted")
        self.assertEqual(self.records(), ["limit-auto/E004-accept.json"])


class GitLimitAutoTests(EngineFixture):
    """The cleanup pause at the limit exercised end to end through real Git calls."""

    def setUp(self):
        super().setUp()
        self.config["review_limit_auto"] = "continue_accept"
        self.config["max_reviews"] = 2

    def git(self, *args):
        environment = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        environment.pop("GIT_DIR", None)
        environment.pop("GIT_WORK_TREE", None)
        return subprocess.run([shutil.which("git"), *args], cwd=self.project, env=environment,
                              capture_output=True, check=True, text=True, timeout=15).stdout

    def to_limit(self):
        self.config["snapshot"]["mode"] = "git"
        self.write_config()
        self.git("init", "--quiet")
        self.git("add", "--all")
        self.git("-c", "user.name=DevFlow offline tests", "-c", "user.email=tests@example.invalid",
                 "commit", "--quiet", "-m", "Fixture baseline")
        self.start()
        self.ready()
        self.response_kind = "changes_required"
        for _ in range(2):
            self.review()
            self.reflect()
            self.ready()

    def test_a_git_wrapped_interrupt_in_the_limit_workspace_check_saves_the_pause(self):
        self.to_limit()
        failure = ProcessCleanupError("a descendant retained output pipes")
        failure.__cause__ = KeyboardInterrupt()
        with patch("devflow_core.common.git.run_bounded", side_effect=failure):
            with self.assertRaises(KeyboardInterrupt):
                self.review()
        state = self.state()
        self.assertEqual(state["status"], "paused_error")
        self.assertEqual(len(state["attempts"]), 2)
        self.assertIsNone(state["escalation"])

    def test_a_git_retained_pipes_result_after_the_last_review_pauses(self):
        self.to_limit()
        state = self.engine.store.read(self.run_id)
        state["review_limit"] = 4
        atomic_write_json(self.run / "state.json", state)
        self.review()
        self.reflect()
        self.ready()
        armed, original_respond, real_bounded = {"on": False}, self.respond, git_module.run_bounded

        def respond_then_arm(*args, **kwargs):
            armed["on"] = True
            return original_respond(*args, **kwargs)

        def flaky(*args, **kwargs):
            if armed["on"]:
                return Completed(exit_code=0, stdout=b"", stderr=b"", timed_out=False,
                                 retained_pipes=True, output_error=None, duration_seconds=0.01)
            return real_bounded(*args, **kwargs)

        self.reviewer.side_effect = respond_then_arm
        with patch("devflow_core.common.git.run_bounded", side_effect=flaky):
            result = self.review()
        self.assertEqual(result["status"], "paused_error")
        self.assertIn("retained output pipes", result["last_error"])
        self.assertEqual(len(self.state()["attempts"]), 4)


if __name__ == "__main__":
    unittest.main()
