"""The plan workflow's review contract; the reviewer CLIs live in common.reviewers."""
from __future__ import annotations

from typing import Any

from ..common.errors import ReviewerError
from ..common.reviewers import text

REVIEW_KEYS = {"schema_version", "verdict", "summary", "findings", "questions"}
FINDING_KEYS = {"id", "severity", "category", "location", "evidence", "recommendation", "requires_human"}
QUESTION_KEYS = {"id", "question", "why_human", "options"}


def validate_review(value: Any) -> dict:
    """Validate the exact semantic contract, including cross-field invariants."""
    if not isinstance(value, dict) or set(value) != REVIEW_KEYS:
        raise ReviewerError("Review must contain exactly schema_version, verdict, summary, findings, questions")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ReviewerError("Unsupported review schema_version")
    if value["verdict"] not in ("pass", "changes_required", "human_required"):
        raise ReviewerError("Unsupported review verdict")
    text(value["summary"], "summary", 32768)
    findings, questions = value["findings"], value["questions"]
    if not isinstance(findings, list) or len(findings) > 128:
        raise ReviewerError("findings must be an array of at most 128 items")
    if not isinstance(questions, list) or len(questions) > 64:
        raise ReviewerError("questions must be an array of at most 64 items")
    seen: set[str] = set()
    for item in findings:
        if not isinstance(item, dict) or set(item) != FINDING_KEYS:
            raise ReviewerError("Finding has missing or unknown fields")
        for key in FINDING_KEYS - {"requires_human"}:
            text(item[key], f"finding.{key}", 32768 if key != "id" else 128)
        if item["id"] in seen:
            raise ReviewerError("Finding and question IDs must be unique")
        seen.add(item["id"])
        if item["severity"] not in ("critical", "high", "medium", "low"):
            raise ReviewerError("Unsupported finding severity")
        if type(item["requires_human"]) is not bool:
            raise ReviewerError("requires_human must be a boolean")
    for item in questions:
        if not isinstance(item, dict) or set(item) != QUESTION_KEYS:
            raise ReviewerError("Question has missing or unknown fields")
        for key in ("id", "question", "why_human"):
            text(item[key], f"question.{key}", 32768 if key != "id" else 128)
        if item["id"] in seen:
            raise ReviewerError("Finding and question IDs must be unique")
        seen.add(item["id"])
        if not isinstance(item["options"], list) or len(item["options"]) > 12:
            raise ReviewerError("question.options must be an array of at most 12 strings")
        for option in item["options"]:
            text(option, "question.option")
    human = bool(questions) or any(f["requires_human"] for f in findings)
    if value["verdict"] == "pass" and (findings or questions):
        raise ReviewerError("A pass cannot contain findings or questions")
    if value["verdict"] == "changes_required" and (not findings or human):
        raise ReviewerError("changes_required needs findings and cannot include human decisions")
    if value["verdict"] == "human_required" and not questions:
        raise ReviewerError("human_required needs at least one explicit question for the human")
    return value
