"""The implement workflow's review contract (``devflow_core.implement.review``).

The reviewer CLIs themselves are covered by tests/common/test_reviewers.py.
"""

from __future__ import annotations

import copy
import unittest

from devflow_core.common.errors import ReviewerError
from devflow_core.implement.review import validate_review

PASS = {"schema_version": 1, "snapshot_id": "snapshot-abc123", "verdict": "pass",
        "summary": "問題なし", "findings": [], "questions": []}
FINDING = {"id": "F1", "severity": "medium", "category": "correctness", "file": "src/main.py",
           "line_start": 2, "line_end": 4, "evidence": "No empty-input handling",
           "recommendation": "Handle empty input", "requires_human": False}


class ValidationTests(unittest.TestCase):
    def test_pass_and_no_mutation(self):
        value = copy.deepcopy(PASS)
        self.assertEqual(validate_review(value), PASS)
        self.assertEqual(value, PASS)

    def test_snapshot_is_required(self):
        value = {k: v for k, v in PASS.items() if k != "snapshot_id"}
        with self.assertRaises(ReviewerError):
            validate_review(value)

    def test_unknown_fields(self):
        with self.assertRaises(ReviewerError):
            validate_review({**PASS, "execute": "rm -rf ."})

    def test_boolean_version_rejected(self):
        with self.assertRaises(ReviewerError):
            validate_review({**PASS, "schema_version": True})

    def test_bad_snapshot_identifiers(self):
        for snapshot in (None, "", "abc/../../x", "a b"):
            with self.subTest(snapshot=snapshot), self.assertRaises(ReviewerError):
                validate_review({**PASS, "snapshot_id": snapshot})

    def test_pass_cannot_hide_findings_or_questions(self):
        for change in ({"findings": [FINDING]}, {"questions": ["What scope?"]}):
            with self.subTest(change=change), self.assertRaises(ReviewerError):
                validate_review({**PASS, **change})

    def test_changes_need_findings(self):
        with self.assertRaises(ReviewerError):
            validate_review({**PASS, "verdict": "changes_required"})
        validate_review({**PASS, "verdict": "changes_required", "findings": [FINDING]})

    def test_human_required_needs_question(self):
        with self.assertRaises(ReviewerError):
            validate_review({**PASS, "verdict": "human_required"})
        validate_review({**PASS, "verdict": "human_required", "questions": ["Which public API?"]})

    def test_human_flag_cannot_be_automatic(self):
        with self.assertRaises(ReviewerError):
            validate_review({**PASS, "verdict": "changes_required",
                             "findings": [{**FINDING, "requires_human": True}]})

    def test_duplicate_finding_id(self):
        with self.assertRaises(ReviewerError):
            validate_review({**PASS, "verdict": "changes_required", "findings": [FINDING, FINDING]})

    def test_invalid_line_ranges(self):
        for start, end in ((-1, 0), (0, 1), (5, 2), (True, 2), (1, "2")):
            with self.subTest(start=start, end=end), self.assertRaises(ReviewerError):
                validate_review({**PASS, "verdict": "changes_required",
                                 "findings": [{**FINDING, "line_start": start, "line_end": end}]})

    def test_cross_cutting_finding(self):
        validate_review({**PASS, "verdict": "changes_required",
                         "findings": [{**FINDING, "file": "*", "line_start": 0, "line_end": 0}]})

    def test_paths_must_be_relative(self):
        for path in ("/tmp/a", "../a", "C:\\secret", "src/../secret", "src\\a"):
            with self.subTest(path=path), self.assertRaises(ReviewerError):
                validate_review({**PASS, "verdict": "changes_required",
                                 "findings": [{**FINDING, "file": path}]})


if __name__ == "__main__":
    unittest.main()
