"""The plan workflow's review contract (``devflow_core.plan.review``).

The reviewer CLIs themselves are covered by tests/common/test_reviewers.py.
"""

from __future__ import annotations

import copy
import unittest

from devflow_core.common.errors import DevFlowError, ReviewerError
from devflow_core.plan.review import validate_review
from tests.common.test_common import FIELD_ERROR


PASS = {"schema_version": 1, "verdict": "pass", "summary": "問題なし", "findings": [], "questions": []}
FINDING = {
    "id": "F1", "severity": "medium", "category": "testability", "location": "section 3",
    "evidence": "No failure recovery check", "recommendation": "Add a recovery test", "requires_human": False,
}
QUESTION = {"id": "Q1", "question": "Which scope?", "why_human": "Scope is undecided", "options": ["A", "B"]}


class ValidationTests(unittest.TestCase):
    def test_extra_finding_field_is_named(self):
        # The shape of a real OpenCode reply: a finding with an added field.
        value = {**PASS, "verdict": "changes_required", "findings": [{**FINDING, "consequence": "Breaks X"}]}
        with self.assertRaises(ReviewerError) as caught:
            validate_review(value)
        self.assertEqual(str(caught.exception), FIELD_ERROR)

    def test_missing_review_field_is_named(self):
        value = {key: item for key, item in PASS.items() if key != "questions"}
        with self.assertRaisesRegex(ReviewerError, r'^Review has missing or unknown fields \(missing: "questions"; unknown: none\)$'):
            validate_review(value)

    def test_pass(self):
        self.assertEqual(validate_review(PASS), PASS)

    def test_errors_are_reviewer_errors(self):
        self.assertTrue(issubclass(ReviewerError, DevFlowError))

    def test_unknown_fields(self):
        with self.assertRaises(ReviewerError):
            validate_review({**PASS, "command": "execute this"})

    def test_bool_schema_version_is_not_an_integer(self):
        with self.assertRaises(ReviewerError):
            validate_review({**PASS, "schema_version": True})

    def test_pass_cannot_hide_findings(self):
        with self.assertRaises(ReviewerError):
            validate_review({**PASS, "findings": [FINDING]})

    def test_changes_need_findings(self):
        with self.assertRaises(ReviewerError):
            validate_review({**PASS, "verdict": "changes_required"})
        validate_review({**PASS, "verdict": "changes_required", "findings": [FINDING]})

    def test_human_decision_cannot_be_marked_automatic(self):
        with self.assertRaises(ReviewerError):
            validate_review({**PASS, "verdict": "changes_required", "findings": [{**FINDING, "requires_human": True}]})

    def test_human_required_needs_evidence(self):
        with self.assertRaises(ReviewerError):
            validate_review({**PASS, "verdict": "human_required"})
        validate_review({**PASS, "verdict": "human_required", "questions": [QUESTION]})

    def test_human_required_finding_without_question_is_rejected(self):
        with self.assertRaisesRegex(ReviewerError, "at least one explicit human question"):
            validate_review({
                **PASS, "verdict": "human_required",
                "findings": [{**FINDING, "requires_human": True}], "questions": [],
            })

    def test_duplicate_ids(self):
        with self.assertRaises(ReviewerError):
            validate_review({**PASS, "verdict": "changes_required", "findings": [FINDING, FINDING]})

    def test_event_wrapper_is_not_a_review(self):
        wrapper = {"type": "text", "sessionID": "s1",
                   "part": {"id": "p1", "messageID": "m1", "text": "{}"}}
        with self.assertRaises(ReviewerError):
            validate_review(wrapper)

    def test_no_mutation_during_validation(self):
        value = copy.deepcopy(PASS)
        validate_review(value)
        self.assertEqual(value, PASS)


if __name__ == "__main__":
    unittest.main()
