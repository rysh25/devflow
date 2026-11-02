"""The plan workflow's review contract; the reviewer CLIs live in common.reviewers."""
from __future__ import annotations

from typing import Any

from ..common.errors import ReviewerError
from ..common.reviewers import text
from ..common.review import check_fields, check_verdict

REVIEW_KEYS = {"schema_version", "verdict", "summary", "findings", "questions"}
FINDING_KEYS = {"id", "severity", "category", "location", "evidence", "recommendation", "requires_human"}
QUESTION_KEYS = {"id", "question", "why_human", "options"}


def validate_review(value: Any) -> dict:
    """Validate the exact semantic contract, including cross-field invariants."""
    check_fields(value, REVIEW_KEYS, "Review")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ReviewerError("Unsupported review schema_version")
    text(value["summary"], "summary", 32768)
    findings, questions = value["findings"], value["questions"]
    if not isinstance(findings, list) or len(findings) > 128:
        raise ReviewerError("findings must be an array of at most 128 items")
    if not isinstance(questions, list) or len(questions) > 64:
        raise ReviewerError("questions must be an array of at most 64 items")
    seen: set[str] = set()
    for index, item in enumerate(findings):
        check_fields(item, FINDING_KEYS, f"findings[{index}]")
        for key in FINDING_KEYS - {"requires_human"}:
            text(item[key], f"finding.{key}", 32768 if key != "id" else 128)
        if item["id"] in seen:
            raise ReviewerError("Finding and question IDs must be unique")
        seen.add(item["id"])
        if item["severity"] not in ("critical", "high", "medium", "low"):
            raise ReviewerError("Unsupported finding severity")
        if type(item["requires_human"]) is not bool:
            raise ReviewerError("requires_human must be a boolean")
    for index, item in enumerate(questions):
        check_fields(item, QUESTION_KEYS, f"questions[{index}]")
        for key in ("id", "question", "why_human"):
            text(item[key], f"question.{key}", 32768 if key != "id" else 128)
        if item["id"] in seen:
            raise ReviewerError("Finding and question IDs must be unique")
        seen.add(item["id"])
        if not isinstance(item["options"], list) or len(item["options"]) > 12:
            raise ReviewerError("question.options must be an array of at most 12 strings")
        for option in item["options"]:
            text(option, "question.option")
    check_verdict(value["verdict"], findings, questions)
    return value
