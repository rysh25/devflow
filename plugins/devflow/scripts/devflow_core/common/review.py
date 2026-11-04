"""Shared budget, approval and verdict mechanics; domain policy remains in each flow."""
import json
from typing import Callable, Collection

from .errors import DevFlowError, ReviewerError
from .runs import TERMINAL

HARD_MAX_REVIEWS = 6  # The ceiling of the configured max_reviews (the initial limit).
# One run's total: review records use two-digit numbers (reviews/NN).
TOTAL_REVIEW_CEILING = 99
# The kind of the human question opened at the review limit (escalation detail).
REVIEW_BUDGET = "review_budget"
REVIEW_LIMIT_REASON = "The review limit is reached without approval from every reviewer"
REVIEW_LIMIT_QUESTION = ("Review limit reached: {used} of {limit} reviews used. "
                         "Reviewers approving the current version: {approved}. "
                         "Continue with {grant} more reviews (new limit {new_limit}; "
                         "next reviewers in order: {order}), or stop and end the run as exhausted?")
# Field names in a rejected response come from the model: show a bounded,
# escaped, deterministic selection.
SHOWN_FIELD_NAMES = 8
FIELD_NAME_CHARS = 64


def validate_limit(value: int) -> int:
    if type(value) is not int or not 2 <= value <= HARD_MAX_REVIEWS:
        raise DevFlowError("max_reviews must be an integer from 2 to 6; six is a hard ceiling")
    return value


def next_reviewer(reviewers: list, used: int, limit: int) -> dict:
    if type(used) is not int or type(limit) is not int or not 0 <= used < limit <= TOTAL_REVIEW_CEILING:
        raise DevFlowError("Review limit reached; the next review request asks the human")
    if not reviewers:
        raise DevFlowError("No reviewers configured")
    return reviewers[used % len(reviewers)]


def all_approved(reviewers: list, approvals: dict, artifact_digest: str) -> bool:
    return bool(reviewers) and all(approvals.get(r["id"]) == artifact_digest for r in reviewers)


def after_failed_review(used: int, state: dict, ready_status: str) -> str:
    """The status after a failed or interrupted round, which used its attempt: paused
    for a retry while attempts remain; at the limit ``ready_status``, from which the
    next review request asks the human (``ask_at_limit``)."""
    return "paused_error" if used < state["review_limit"] else ready_status


def ask_at_limit(state: dict, used: int, approved: Collection[str],
                 escalate: Callable[[str, list[str]], None]) -> bool:
    """At the review limit, ask the human whether to add one round of reviewers.

    Returns False while reviews remain. At the limit it calls ``escalate(reason,
    questions)`` with the review_budget question, or ends the run as exhausted
    when one more round would pass TOTAL_REVIEW_CEILING, and returns True.
    ``approved`` holds the reviewers approving the current version.
    """
    limit = state["review_limit"]
    if used < limit:
        return False
    reviewers = state["config"]["reviewers"]
    grant = len(reviewers)
    if limit + grant > TOTAL_REVIEW_CEILING:
        state["status"] = "exhausted"
        return True
    approving = [reviewer["id"] for reviewer in reviewers if reviewer["id"] in approved]
    order = [reviewers[(used + index) % grant]["id"] for index in range(grant)]
    escalate(REVIEW_LIMIT_REASON, [REVIEW_LIMIT_QUESTION.format(
        used=used, limit=limit, approved=", ".join(approving) or "none", grant=grant,
        new_limit=limit + grant, order=", ".join(order))])
    return True


def decide_at_limit(state: dict, decision: str, return_status: str) -> None:
    """Apply the human's answer to the review_budget question: continue adds one
    round of reviewers and returns to ``return_status``; stop ends as exhausted."""
    if decision == "continue":
        state["review_limit"] += len(state["config"]["reviewers"])
        state["status"] = return_status
    elif decision == "stop":
        state["status"] = "exhausted"
    else:
        raise DevFlowError("The review limit question takes continue or stop")


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


def check_review_budget(state: dict, used: object) -> None:
    """The run's review limit starts at max_reviews, grows only in whole rounds of
    reviewers, never passes TOTAL_REVIEW_CEILING and never falls below the reviews used."""
    limit, initial = state["review_limit"], state["config"]["max_reviews"]
    if (type(limit) is not int or not initial <= limit <= TOTAL_REVIEW_CEILING
            or (limit - initial) % len(state["config"]["reviewers"])
            or type(used) is not int or not 0 <= used <= limit):
        raise DevFlowError("Incompatible/corrupt run state; never reset the review count")


def review_budget(state: dict, used: int) -> dict:
    """Attempts used, the limit and what remains, and who reviews next (none at the
    limit or once finished)."""
    limit = state["review_limit"]
    upcoming = (next_reviewer(state["config"]["reviewers"], used, limit)
                if used < limit and state["status"] not in TERMINAL else None)
    return {"review_count": used, "review_limit": limit, "remaining_reviews": limit - used,
            "next_reviewer": upcoming["id"] if upcoming else None}
