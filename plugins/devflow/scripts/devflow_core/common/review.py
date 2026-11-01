"""Shared budget and approval mechanics; domain policy remains in each flow."""
from .storage import DevFlowError

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
