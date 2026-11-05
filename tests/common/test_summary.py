"""The review summary shared by every workflow: its data and its short fixed Markdown."""
import unittest

from devflow_core.common import summary

REVIEWERS = [{"id": "codex", "requested_model": "gpt-x", "requested_effort": "xhigh", "timeout_seconds": 1800},
             {"id": "opencode", "requested_model": "deep", "requested_effort": "high", "timeout_seconds": 1800}]


def finding(identifier, severity="medium", action="accept", reason="Fixed in the next version."):
    return {"id": identifier, "severity": severity, "category": "correctness", "location": "plan.md:5",
            "recommendation": "Do the right thing.\nOn two lines.", "action": action, "reason": reason}


def review(number, reviewer, verdict="changes_required", findings=(), status="succeeded", **extra):
    entry = {"number": number, "reviewer_id": reviewer, "status": status, "cli_version": "cli 1.0",
             "cli_version_source": f"review-processes/{number:02d}/invocation.json",
             "started_at": "2026-11-05T00:00:00+00:00", "finished_at": "2026-11-05T00:23:05+00:00",
             "version": {"id": f"hash{number}", "label": f"v{number - 1:03d}"},
             "verdict": verdict if status == "succeeded" else None, "error": None,
             "findings": list(findings), "fix": None, "tokens": None, "tool_call_count": None,
             "record": f"reviews/{number:02d}/review.json"}
    entry.update(extra)
    return entry


def source(reviews, **extra):
    value = {"flow": "plan", "run_id": "20261105T000000Z-000000000000", "status": "exhausted",
             "pending_question": None, "created_at": "2026-11-05T00:00:00+00:00",
             "finished_at": "2026-11-05T03:05:44+00:00", "review_count": len(reviews), "review_limit": 6,
             "reviewers": REVIEWERS, "current_version": {"id": "hash9", "label": "v005"},
             "latest_version_reviewed": False, "approving_current": [], "reviews": reviews,
             "human_decisions": [], "human_acceptance": None}
    value.update(extra)
    return value



def decision(number, kind, choice=None, at="2026-11-05T03:05:44+00:00"):
    return {"at": at, "review_number": number, "question_kind": kind, "questions": ["A question?"],
            "answer": '{"answers": {}}', "decision": choice, "interpretation": "Read the answer.",
            "event_ids": ["e1"]}


class BuildTests(unittest.TestCase):
    def test_each_review_gets_its_limits_time_and_counts(self):
        data = summary.build(source([review(1, "codex", findings=[finding("F1", "high"), finding("F2", "low")]),
                                     review(2, "opencode", status="failed", error="timed out", finished_at=None)]),
                             "2026-11-05T09:00:00+00:00")
        first, second = data["reviews"]
        self.assertEqual((first["requested_model"], first["requested_effort"], first["timeout_seconds"]),
                         ("gpt-x", "xhigh", 1800))
        self.assertEqual(first["duration_seconds"], 1385)
        self.assertEqual(first["finding_counts"], {"critical": 0, "high": 1, "medium": 0, "low": 1})
        # A failed attempt has no counts and, here, no end time.
        self.assertEqual((second["finding_counts"], second["duration_seconds"]), (None, None))
        self.assertEqual(data["finding_trend"], [
            {"number": 1, "reviewer_id": "codex", "status": "succeeded", "counts": first["finding_counts"]},
            {"number": 2, "reviewer_id": "opencode", "status": "failed", "counts": None}])
        self.assertEqual(data["time"], {"run_elapsed_seconds": 11144, "review_seconds_total": 1385,
                                        "reviews_without_time": 1})

    def test_elapsed_time_runs_to_now_before_the_end(self):
        data = summary.build(source([], finished_at=None, status="awaiting_human"), "2026-11-05T01:00:00+00:00")
        self.assertEqual(data["time"]["run_elapsed_seconds"], 3600)

    def test_approvals_per_reviewer_and_of_the_current_version(self):
        data = summary.build(source([review(1, "codex", "pass"), review(2, "opencode", findings=[finding("F1")]),
                                     review(3, "codex", "pass")], approving_current=["codex"]), "now")
        approval = data["approval"]
        self.assertEqual(approval["reviewers"], [
            {"id": "codex", "approves_current": True,
             "approved": [{"review_number": 1, "version": {"id": "hash1", "label": "v000"}},
                          {"review_number": 3, "version": {"id": "hash3", "label": "v002"}}]},
            {"id": "opencode", "approves_current": False, "approved": []}])
        self.assertFalse(approval["all_reviewers_approved"])
        both = summary.build(source([], approving_current=["codex", "opencode"]), "now")
        self.assertTrue(both["approval"]["all_reviewers_approved"])

    def test_remaining_findings_are_each_reviewers_latest_review_unless_it_passed(self):
        data = summary.build(source([
            review(1, "codex", findings=[finding("F1")]),
            review(2, "opencode", findings=[finding("G1"), finding("G2", action=None, reason=None)]),
            review(3, "codex", "pass"),
            review(4, "opencode", status="failed", error="crashed")]), "now")
        # codex passed last; opencode's latest succeeded review still has findings (one undecided).
        self.assertEqual(data["remaining_findings"], {"count": 2, "items": [
            {"reviewer_id": "opencode", "review_number": 2, "finding_ids": ["G1", "G2"]}]})

    def test_tool_calls_and_question_kinds_are_copied(self):
        data = summary.build(source([review(1, "codex", "pass", tool_call_count=12)],
                                    human_decisions=[decision(1, "review_budget", "continue")]), "now")
        self.assertEqual(data["reviews"][0]["tool_call_count"], 12)
        self.assertEqual(data["human_decisions"][0]["question_kind"], "review_budget")

    def test_reviewer_totals_sum_each_reviewers_own_counts(self):
        data = summary.build(source([
            review(1, "codex", tokens={"output_tokens": 5, "input_tokens": 100}),
            review(2, "opencode", status="failed", error="timed out", cli_version=None),
            review(3, "codex", "pass", tokens={"input_tokens": 50, "cached_input_tokens": 7}, cli_version="cli 1.1"),
            review(4, "opencode", "pass", tokens={"input": 9})]), "now")
        self.assertEqual(data["reviewer_totals"], [
            {"id": "codex", "requested_model": "gpt-x", "requested_effort": "xhigh",
             # A CLI updated during the run: every version, once each, in order.
             "cli_versions": ["cli 1.0", "cli 1.1"],
             "tokens": {"cached_input_tokens": 7, "input_tokens": 150, "output_tokens": 5},
             "reviews_without_tokens": 0},
            # Never added across CLIs; a review without counts is counted, not guessed.
            {"id": "opencode", "requested_model": "deep", "requested_effort": "high", "cli_versions": ["cli 1.0"],
             "tokens": {"input": 9}, "reviews_without_tokens": 1}])
        empty = summary.build(source([review(1, "codex", "pass")]), "now")["reviewer_totals"]
        self.assertEqual([(item["tokens"], item["reviews_without_tokens"]) for item in empty], [(None, 1), (None, 0)])

    def test_a_total_a_record_cannot_hold_is_unknown(self):
        # Each count was writable, but their sum is past Python's integer-to-text limit.
        large = 9 * 10 ** 4299
        data = summary.build(source([review(1, "codex", tokens={"input_tokens": large}),
                                     review(2, "codex", "pass", tokens={"input_tokens": large})]), "now")
        self.assertIsNone(data["reviewer_totals"][0]["tokens"])
        summary.render(data)

    def test_text_is_never_cut_and_shown_once(self):
        long = "x" * 100000
        recommendation = "y" * 100000
        data = summary.build(source([review(1, "codex", findings=[
            {**finding("F1", reason=long), "recommendation": recommendation}])]), "now")
        # The data holds everything; the Markdown shows a remaining finding in full, once,
        # and leaves the disposition's reason to the data.
        self.assertEqual(data["reviews"][0]["findings"][0]["reason"], long)
        text = summary.render(data)
        self.assertNotIn(long, text)
        self.assertEqual(text.count(recommendation), 1)


class RenderTests(unittest.TestCase):
    def render(self, **extra):
        reviews = extra.pop("reviews", None) or [
            review(1, "codex", findings=[finding("F1", "high")], tokens={"input_tokens": 2171213, "output_tokens": 5},
                   tool_call_count=12,
                   fix={"summary": "Added the case.", "file_changes": None,
                        "version_change": {"from": "v000", "to": "v001", "lines_added": 40, "lines_removed": 12}}),
            review(2, "opencode", status="failed", error="opencode exited with 1", cli_version=None,
                   finished_at=None, record=None)]
        return summary.render(summary.build(source(reviews, **extra), "2026-11-05T09:00:00+00:00"))

    def test_the_fixed_form(self):
        text = self.render(status="accepted", human_acceptance={
            "event_ids": ["e1"], "answer": "Accept", "version": "hash9", "accepted_at": "2026-11-05T03:05:44+00:00",
            "record": "escalations/x-answer.json"}, human_decisions=[decision(2, "review_budget", "accept")])
        self.assertEqual(text, "\n".join([
            "## DevFlow review summary — plan run 20261105T000000Z-000000000000", "",
            "- Status: accepted — accepted by the human at the review limit, not approved by every reviewer",
            "- Reviews: 2 of limit 6; time: run 3:05:44, reviews 0:23:05 (1 review without recorded time)",
            "- Current version: v005, not reviewed; approvals: codex no, opencode no", "",
            "| # | Reviewer | Version | Result | Findings | Tool calls | Time |",
            "|---|---|---|---|---|---|---|",
            "| 1 | codex | v000 | changes_required | high 1; accept 1 | 12 | 0:23:05 of 0:30:00 |",
            "| 2 | opencode | v001 | failed | — | unknown | unknown of 0:30:00 |", "",
            "- codex: gpt-x, xhigh (requested), cli 1.0; tokens: input_tokens 2,171,213, output_tokens 5",
            "- opencode: deep, high (requested), CLI unknown; tokens: unknown (1 review without token counts)",
            "- Failures:",
            "  - Review 2 (opencode): opencode exited with 1",
            "- Remaining findings (1):",
            "  - codex, review 1: F1 [high] correctness — plan.md:5: Do the right thing.\nOn two lines.",
            "- Human decisions:",
            "  - After 2 reviews, review_budget: accept (2026-11-05T03:05:44+00:00)"]) + "\n")

    def test_what_stays_in_the_data(self):
        text = self.render(status="accepted", human_acceptance={
            "event_ids": ["e1"], "answer": "Accept", "version": "hash9", "accepted_at": "2026-11-05T03:05:44+00:00",
            "record": "escalations/x-answer.json"}, human_decisions=[decision(2, "review_budget", "accept")])
        for hidden in ("hash", "2026-11-05T00:00:00", "reviews/01/review.json", "review-processes/",
                       "Approved versions", "Human acceptance", "escalations/x-answer.json",
                       "Fixed in the next version.", "Added the case.", "+40", "A question?", "answers",
                       "Read the answer.", "e1"):
            self.assertNotIn(hidden, text)

    def test_empty_parts_say_none(self):
        text = self.render(reviews=[review(1, "codex", "pass")], status="completed",
                           approving_current=["codex", "opencode"], latest_version_reviewed=True)
        self.assertIn("- Status: completed — every reviewer approved the same version", text)
        self.assertIn("- Current version: v005, reviewed; approvals: codex yes, opencode yes", text)
        self.assertIn("| 1 | codex | v000 | pass | none | unknown | 0:23:05 of 0:30:00 |", text)
        self.assertIn("- codex: gpt-x, xhigh (requested), cli 1.0; tokens: unknown (1 review without token counts)\n"
                      "- opencode: deep, high (requested), CLI unknown; tokens: unknown\n"
                      "- Failures: none\n- Remaining findings: none\n- Human decisions: none\n", text)
        no_reviews = summary.render(summary.build(source([], current_version={"id": None, "label": "none (no plan yet)"}),
                                                  "now"))
        self.assertIn("- Current version: none (no plan yet), not reviewed; approvals: codex no, opencode no\n\n"
                      "No reviews yet.\n", no_reviews)

    def test_each_review_adds_one_table_row(self):
        counts = []
        for number in range(1, 5):
            reviews = [review(index, ("codex", "opencode")[index % 2], "pass") for index in range(1, number + 1)]
            counts.append(len(self.render(reviews=reviews).splitlines()))
        self.assertEqual([later - earlier for earlier, later in zip(counts, counts[1:])], [1, 1, 1])

    def test_free_text_never_enters_the_table(self):
        text = self.render(reviews=[
            review(1, "codex", findings=[{**finding("F1"), "recommendation": "Use a | b.\n| not | a row |",
                                         "location": "x | y"}]),
            review(2, "opencode", status="failed", error="failed | badly\n| 1 | 2 |")])
        lines = text.splitlines()
        start = lines.index("| # | Reviewer | Version | Result | Findings | Tool calls | Time |")
        end = lines.index("", start)
        self.assertEqual(end - start, 4)
        self.assertTrue(all(line.count("|") == 8 for line in lines[start:end]))
        self.assertIn("  - Review 2 (opencode): failed | badly\n| 1 | 2 |", text)
        self.assertIn("  - codex, review 1: F1 [medium] correctness — x | y: Use a | b.\n| not | a row |", text)

    def test_findings_count_severities_then_dispositions(self):
        text = self.render(reviews=[
            review(1, "codex", findings=[finding("F1", "critical", action="reject", reason="No."),
                                         finding("F2", "low", action=None, reason=None),
                                         finding("F3", "low", action="already_addressed", reason="Done."),
                                         finding("F4", "medium")]),
            review(2, "opencode", status="in_progress", finished_at=None)])
        self.assertIn("| 1 | codex | v000 | changes_required | critical 1, medium 1, low 2; "
                      "accept 1, already_addressed 1, reject 1, undecided 1 | unknown |", text)
        self.assertIn("| 2 | opencode | v001 | in_progress | — | unknown | unknown of 0:30:00 |", text)
        # An attempt in progress has no error to list.
        self.assertIn("- Failures: none", text)

    def test_human_decisions(self):
        text = self.render(human_decisions=[
            decision(6, "review_budget", "continue"), decision(1, "review_decision"), decision(0, "plan_decision"),
            decision(None, "implementation_decision", "cancel"), decision(2, None)])
        self.assertIn("- Human decisions:\n"
                      "  - After 6 reviews, review_budget: continue (2026-11-05T03:05:44+00:00)\n"
                      "  - After 1 review, review_decision: answered (2026-11-05T03:05:44+00:00)\n"
                      "  - After 0 reviews, plan_decision: answered (2026-11-05T03:05:44+00:00)\n"
                      "  - After an unknown number of reviews, implementation_decision: answered "
                      "(2026-11-05T03:05:44+00:00)\n"
                      "  - After 2 reviews, question: answered (2026-11-05T03:05:44+00:00)\n", text)

    def test_the_limit_question(self):
        text = self.render(status="awaiting_human", pending_question="review_budget", finished_at=None)
        self.assertIn("- Status: awaiting_human (review limit question)", text)
        self.assertIn("- Reviews: 2 of limit 6; time: run 9:00:00, reviews 0:23:05", text)

    def test_human_questions_only(self):
        text = self.render(reviews=[review(1, "codex", "human_required")])
        self.assertIn("- Remaining findings (0):\n  - codex, review 1: no findings (human questions only)\n", text)


if __name__ == "__main__":
    unittest.main()
