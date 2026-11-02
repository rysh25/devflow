"""Shared budget, approval and verdict mechanics; domain policy remains in each flow."""
import json

from .errors import DevFlowError, ReviewerError
from .runs import TERMINAL

HARD_MAX_REVIEWS = 6
# Field names in a rejected response come from the model: show a bounded,
# escaped, deterministic selection.
SHOWN_FIELD_NAMES = 8
FIELD_NAME_CHARS = 64


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


def _field_names(names: list[str]) -> str:
    if not names:
        return "none"
    # ensure_ascii escapes control and bidirectional characters, so a name
    # cannot disguise the message; the cut applies before escaping.
    shown = [json.dumps(name[:FIELD_NAME_CHARS]) for name in names[:SHOWN_FIELD_NAMES]]
    if len(names) > SHOWN_FIELD_NAMES:
        shown.append(f"and {len(names) - SHOWN_FIELD_NAMES} more")
    return ", ".join(shown)


def check_fields(value: object, expected: set[str] | frozenset[str], label: str) -> None:
    """Require exactly the expected keys, naming the missing and unknown ones."""
    if not isinstance(value, dict):
        raise ReviewerError(f"{label} must be a JSON object")
    actual = set(value)
    missing, unknown = sorted(expected - actual), sorted(actual - expected)
    if missing or unknown:
        raise ReviewerError(f"{label} has missing or unknown fields "
                            f"(missing: {_field_names(missing)}; unknown: {_field_names(unknown)})")


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


def check_review_config(config: object) -> None:
    """The review settings a run's frozen configuration must keep (for any workflow)."""
    if not isinstance(config, dict):
        raise DevFlowError("Frozen run configuration must be an object")
    validate_limit(config.get("max_reviews"))
    reviewers = config.get("reviewers")
    if (not isinstance(reviewers, list) or not reviewers
            or any(not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in reviewers)):
        raise DevFlowError("Frozen run configuration has no valid reviewers")


def review_budget(used: int, config: dict, status: str) -> dict:
    """Attempts used and remaining, and who reviews next (none once finished)."""
    limit = config["max_reviews"]
    upcoming = next_reviewer(config["reviewers"], used, limit) if used < limit and status not in TERMINAL else None
    return {"review_count": used, "remaining_reviews": limit - used,
            "next_reviewer": upcoming["id"] if upcoming else None}
