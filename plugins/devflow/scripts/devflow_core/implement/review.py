"""The implement workflow's review contract; the reviewer CLIs live in common.reviewers."""
from __future__ import annotations

from pathlib import PurePosixPath
import re
from typing import Any

from ..common.errors import ReviewerError
from ..common.reviewers import text

REVIEW_KEYS = {"schema_version", "snapshot_id", "verdict", "summary", "findings", "questions"}
FINDING_KEYS = {"id", "severity", "category", "file", "line_start", "line_end",
                "evidence", "recommendation", "requires_human"}


def validate_review(value: Any) -> dict:
    """Validate exact fields and invariants. Never repair or salvage model JSON."""
    if not isinstance(value, dict) or set(value) != REVIEW_KEYS:
        raise ReviewerError("Review has missing or unknown fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ReviewerError("Unsupported review schema_version")
    text(value["snapshot_id"], "snapshot_id", 128)
    if not re.fullmatch(r"[A-Za-z0-9._:-]+", value["snapshot_id"]):
        raise ReviewerError("snapshot_id must be a plain identifier")
    if value["verdict"] not in ("pass", "changes_required", "human_required"):
        raise ReviewerError("Unsupported review verdict")
    text(value["summary"], "summary")
    findings, questions = value["findings"], value["questions"]
    if not isinstance(findings, list) or len(findings) > 128:
        raise ReviewerError("findings must be an array of at most 128 items")
    if not isinstance(questions, list) or len(questions) > 64:
        raise ReviewerError("questions must be an array of at most 64 strings")
    seen = set()
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != FINDING_KEYS:
            raise ReviewerError("Finding has missing or unknown fields")
        for field in ("id", "severity", "category", "file", "evidence", "recommendation"):
            text(finding[field], f"finding.{field}", 128 if field == "id" else 32768)
        if finding["id"] in seen:
            raise ReviewerError("Finding IDs must be unique within a review")
        seen.add(finding["id"])
        if finding["severity"] not in ("low", "medium", "high", "critical"):
            raise ReviewerError("Unsupported finding severity")
        path = finding["file"]
        if path != "*" and (PurePosixPath(path).is_absolute() or "\\" in path or
                            ":" in path or ".." in PurePosixPath(path).parts):
            raise ReviewerError("finding.file must be a relative POSIX path or '*' for a cross-cutting finding")
        start, end = finding["line_start"], finding["line_end"]
        if (type(start) is not int or type(end) is not int or start < 0 or end < start or
                (start == 0 and end != 0)):
            raise ReviewerError("Finding line range must be 0/0 or an ordered positive range")
        if type(finding["requires_human"]) is not bool:
            raise ReviewerError("requires_human must be a boolean")
    for question in questions:
        text(question, "question")
    human = bool(questions) or any(f["requires_human"] for f in findings)
    if value["verdict"] == "pass" and (findings or questions):
        raise ReviewerError("A pass cannot contain findings or questions")
    if value["verdict"] == "changes_required" and (not findings or human):
        raise ReviewerError("changes_required needs findings and cannot include human decisions")
    if value["verdict"] == "human_required" and not questions:
        raise ReviewerError("human_required needs at least one explicit human question")
    return value
