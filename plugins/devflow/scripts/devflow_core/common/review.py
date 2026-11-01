"""Shared budget, approval and verdict mechanics; domain policy remains in each flow."""
from .errors import DevFlowError, ReviewerError

HARD_MAX_REVIEWS = 6


def validate_limit(value: int) -> int:
    if type(value) is not int or not 2 <= value <= HARD_MAX_REVIEWS:
        raise DevFlowError("max_reviews must be an integer from 2 to 6; six is a hard ceiling")
    return value


def next_reviewer(reviewers: list, used: int, limit: int) -> dict:
    validate_limit(limit)
    if type(used) is not int or used < 0 or used >= limit:
        raise DevFlowError("Review budget exhausted; resuming cannot reset the counter")
    if not reviewers:
        raise DevFlowError("No reviewers configured")
    return reviewers[used % len(reviewers)]


def all_approved(reviewers: list, approvals: dict, artifact_digest: str) -> bool:
    return bool(reviewers) and all(approvals.get(r["id"]) == artifact_digest for r in reviewers)


VERDICTS = ("pass", "changes_required", "human_required")


def check_verdict(verdict: str, findings: list, questions: list) -> None:
    """Cross-field rules every review shares, whatever its finding fields."""
    if verdict not in VERDICTS:
        raise ReviewerError("Unsupported review verdict")
    human = bool(questions) or any(finding["requires_human"] for finding in findings)
    if verdict == "pass" and (findings or questions):
        raise ReviewerError("A pass cannot contain findings or questions")
    if verdict == "changes_required" and (not findings or human):
        raise ReviewerError("changes_required needs findings and cannot include human decisions")
    if verdict == "human_required" and not questions:
        raise ReviewerError("human_required needs at least one explicit human question")
