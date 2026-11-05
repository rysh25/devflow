"""The review summary shared by every workflow: its data and its fixed Markdown."""
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
             "findings": list(findings), "fix": None, "tokens": None, "record": f"reviews/{number:02d}/review.json"}
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

    def test_nothing_is_shortened_and_no_text_is_copied_twice(self):
        long = "x" * 100000
        data = summary.build(source([review(1, "codex", findings=[finding("F1", reason=long)])]), "now")
        self.assertEqual(data["reviews"][0]["findings"][0]["reason"], long)
        self.assertEqual(data["remaining_findings"]["items"][0]["finding_ids"], ["F1"])
        self.assertIn(long, summary.render(data))


class RenderTests(unittest.TestCase):
    def render(self, **extra):
        reviews = extra.pop("reviews", None) or [
            review(1, "codex", findings=[finding("F1", "high")], tokens={"input_tokens": 2171213, "output_tokens": 5},
                   fix={"summary": "Added the case.", "file_changes": None,
                        "version_change": {"from": "v000", "to": "v001", "lines_added": 40, "lines_removed": 12}}),
            review(2, "opencode", status="failed", error="opencode exited with 1", cli_version=None,
                   finished_at=None, record=None)]
        return summary.render(summary.build(source(reviews, **extra), "2026-11-05T09:00:00+00:00"))

    def test_the_fixed_form(self):
        text = self.render(status="accepted", human_acceptance={
            "event_ids": ["e1"], "answer": "Accept", "version": "hash9", "accepted_at": "2026-11-05T03:05:44+00:00",
            "record": "escalations/x-answer.json"}, human_decisions=[{
                "at": "2026-11-05T03:05:44+00:00", "review_number": 2, "questions": ["Continue?"],
                "answer": "Accept", "decision": "accept", "interpretation": None, "event_ids": ["e1"]}])
        self.assertEqual(text.splitlines()[:9], [
            "## DevFlow review summary — plan run 20261105T000000Z-000000000000", "",
            "- Status: accepted — accepted by the human at the review limit, not approved by every reviewer",
            "- Reviews: 2 of limit 6",
            "- Time: run 3:05:44, reviews 0:23:05 (1 review without recorded time)",
            "- Current version: v005 (hash9); reviewed: no",
            "- Approvals of the current version: codex no, opencode no",
            "- Approved versions: codex none; opencode none",
            "- Human acceptance: event e1, version hash9, at 2026-11-05T03:05:44+00:00 (escalations/x-answer.json)"])
        self.assertIn("| Review | Reviewer | Result | Critical | High | Medium | Low |\n|---|---|---|---|---|---|---|\n"
                      "| 1 | codex | changes_required | 0 | 1 | 0 | 0 |\n| 2 | opencode | failed |  |  |  |  |", text)
        self.assertIn("### Review 1 — codex\n\n"
                      "- Model: gpt-x (requested), effort: xhigh (requested), CLI: cli 1.0 "
                      "(process record review-processes/01/invocation.json, not hashed)\n"
                      "- Started: 2026-11-05T00:00:00+00:00, finished: 2026-11-05T00:23:05+00:00, time: 0:23:05 of 0:30:00\n"
                      "- Version: v000; status: succeeded; verdict: changes_required; findings: critical 0, high 1, medium 0, low 0\n"
                      "- Tokens: input_tokens 2,171,213, output_tokens 5\n"
                      "- Record: reviews/01/review.json\n"
                      "- Findings:\n"
                      "  - F1 [high] correctness — plan.md:5\n"
                      "    - Recommendation: Do the right thing.\nOn two lines.\n"
                      "    - Decision: accept — Fixed in the next version.\n"
                      "- Fix: Added the case.\n"
                      "- Changed: v000 → v001 (+40 −12 lines)", text)
        self.assertIn("### Review 2 — opencode\n\n"
                      "- Model: deep (requested), effort: high (requested), CLI: unknown "
                      "(process record review-processes/02/invocation.json, not hashed)\n"
                      "- Started: 2026-11-05T00:00:00+00:00, finished: unknown, time: unknown of 0:30:00\n"
                      "- Version: v001; status: failed; verdict: unknown\n"
                      "- Tokens: unknown\n- Record: unknown\n- Failure: opencode exited with 1", text)
        self.assertIn("### Remaining findings (1)\n\n- codex, review 1 (reviews/01/review.json)\n"
                      "  - F1 [high] correctness — plan.md:5\n"
                      "    - Recommendation: Do the right thing.\nOn two lines.\n"
                      "    - Decision: accept — Fixed in the next version.", text)
        self.assertTrue(text.endswith("### Human decisions\n\n- 2026-11-05T03:05:44+00:00, review 2: question: "
                                      "Continue? — answer: Accept — decision: accept (event e1)\n"))

    def test_empty_parts_say_none(self):
        text = self.render(reviews=[review(1, "codex", "pass")], status="completed",
                           approving_current=["codex", "opencode"], latest_version_reviewed=True)
        self.assertIn("- Status: completed — every reviewer approved the same version", text)
        self.assertIn("- Findings: none", text)
        self.assertIn("### Remaining findings (0)\n\nnone", text)
        self.assertIn("### Human decisions\n\nnone", text)
        self.assertIn("- Human acceptance: none", text)
        no_reviews = summary.render(summary.build(source([], current_version={"id": None, "label": "none (no plan yet)"}),
                                                  "now"))
        self.assertIn("No reviews yet.", no_reviews)
        self.assertIn("- Current version: none (no plan yet); reviewed: no", no_reviews)

    def test_the_limit_question_and_file_changes(self):
        text = self.render(status="awaiting_human", pending_question="review_budget", finished_at=None, reviews=[
            review(1, "codex", findings=[finding("F1", action=None, reason=None)],
                   fix={"summary": "Fixed.", "version_change": None,
                        "file_changes": [{"path": "a.py", "change": "modified"}, {"path": "b.py", "change": "added"}]}),
            review(2, "opencode", fix={"summary": "Nothing to change.", "version_change": None, "file_changes": []})])
        self.assertIn("- Status: awaiting_human (review limit question)", text)
        self.assertIn("    - Decision: undecided", text)
        self.assertIn("- Changed: modified a.py; added b.py", text)
        self.assertIn("- Changed: no file changes", text)


if __name__ == "__main__":
    unittest.main()
