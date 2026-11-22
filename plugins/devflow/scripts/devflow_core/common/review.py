"""Shared budget, approval and verdict mechanics; domain policy remains in each flow."""
from typing import Callable, Collection

from .errors import DevFlowError, ProcessCleanupError, ReviewerError, shown_name
from .hosts import host_profile
from .runs import TERMINAL

# The ceiling of both the configured max_reviews (the initial limit) and one
# run's total: review records use two-digit numbers (reviews/NN).
TOTAL_REVIEW_CEILING = 99
# The kind of the human question opened at the review limit (escalation detail).
REVIEW_BUDGET = "review_budget"
REVIEW_LIMIT_REASON = "The review limit is reached without approval from every reviewer"
REVIEW_LIMIT_QUESTION = ("Review limit reached: {used} of {limit} reviews used. "
                         "Reviewers approving the current version: {approved}. "
                         "Choose one: (1) continue with {grant} more reviews (new limit {new_limit}; "
                         "next reviewers in order: {order}); (2) stop and end the run as exhausted; "
                         "(3) accept the current version ({version}) and end the run as accepted "
                         "(accepted by the human, not approved by every reviewer).")
#: The answers to the review limit question, in the order it lists them.
LIMIT_DECISIONS = ("continue", "stop", "accept")
#: ``last_error`` gains this prefix when one reviewer fails twice in a row.
REPEATED_FAILURE_REASON = ("Reviewer {reviewer} failed in reviews {previous} and {current} in a row; "
                           "fix the cause (for example sign-in, a usage limit or Devin's directory trust), "
                           "then use {resume} {run_id}.")
# Field names in a rejected response come from the model: show a bounded,
# escaped, deterministic selection.
SHOWN_FIELD_NAMES = 8


def validate_limit(value: int) -> int:
    if type(value) is not int or not 1 <= value <= TOTAL_REVIEW_CEILING:
        raise DevFlowError(f"max_reviews must be an integer from 1 to {TOTAL_REVIEW_CEILING}")
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
    """The status after a round a human should look at — a recovered round, or a
    round whose reviewed content changed during the review: paused while attempts
    remain; at the limit ``ready_status``, from which the next review request
    asks the human (``ask_at_limit``). Ordinary reviewer failures take
    ``after_review_failure`` instead."""
    return "paused_error" if used < state["review_limit"] else ready_status


def after_review_failure(used: int, state: dict, ready_status: str, reviewer_id: str,
                         earlier: list[dict], review_error: BaseException | None,
                         content_changed: bool) -> tuple[str, str | None]:
    """The status after a review round that failed or was interrupted, and the
    reason the human should see, if one applies.

    ``used`` is the review's number after the attempt was reserved (the count of
    attempts used); ``earlier`` the rounds before it, each holding ``number``,
    ``reviewer_id`` and ``status`` (``succeeded``, ``failed`` or, for an
    interrupted round left dispatched, ``dispatched``); ``review_error`` the
    exception the reviewer execution raised (``None`` when it succeeded); and
    ``content_changed`` whether the runner's own check after the review found
    the reviewed content changed.

    In order: a ``ProcessCleanupError`` from the reviewer execution pauses as
    ``paused_error`` whatever the limit (a child may still run); a content
    change follows ``after_failed_review`` (paused while attempts remain,
    ``ready_status`` at the limit); at or beyond the review limit the status is
    ``ready_status`` so the next review request asks the human the budget
    question; otherwise, when the reviewer's own most recent earlier round
    (found scanning ``earlier`` backwards) also failed, it pauses as
    ``paused_error`` with ``REPEATED_FAILURE_REASON`` — a recovered round's
    record reads ``failed``, so it counts — and any other failure returns
    ``ready_status`` so the next configured reviewer runs. Returns ``(status,
    reason)``; ``reason`` is ``None`` unless the run pauses on a repeated
    failure.
    """
    if isinstance(review_error, ProcessCleanupError):
        return "paused_error", None
    if content_changed:
        return after_failed_review(used, state, ready_status), None
    if used >= state["review_limit"]:
        return ready_status, None
    for round_ in reversed(earlier):
        if round_["reviewer_id"] == reviewer_id:
            if round_["status"] == "failed":
                return "paused_error", REPEATED_FAILURE_REASON.format(
                    reviewer=reviewer_id, previous=round_["number"], current=used,
                    resume=host_profile(state["owner_host"]).command_prefix + "resume",
                    run_id=state["run_id"])
            break
    return ready_status, None


def ask_at_limit(state: dict, used: int, approved: Collection[str], version: str,
                 escalate: Callable[[str, list[str]], None]) -> bool:
    """At the review limit, ask the human whether to add one round of reviewers,
    stop, or accept the current version.

    Returns False while reviews remain. At the limit it calls ``escalate(reason,
    questions)`` with the review_budget question, or ends the run as exhausted
    when one more round would pass TOTAL_REVIEW_CEILING, and returns True.
    ``approved`` holds the reviewers approving the current version, and
    ``version`` names that version (a plan hash or snapshot ID).
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
        new_limit=limit + grant, order=", ".join(order), version=version)])
    return True


def decide_at_limit(state: dict, decision: str, return_status: str) -> None:
    """Apply the human's answer to the review_budget question: continue adds one
    round of reviewers and returns to ``return_status``; stop ends as exhausted;
    accept ends as accepted. Stop and accept change neither the limit nor approvals."""
    if decision == "continue":
        state["review_limit"] += len(state["config"]["reviewers"])
        state["status"] = return_status
    elif decision == "stop":
        state["status"] = "exhausted"
    elif decision == "accept":
        state["status"] = "accepted"
    else:
        raise DevFlowError("The review limit question takes continue, stop or accept")


VERDICTS = ("pass", "changes_required", "human_required")


def _field_names(names: list[str]) -> str:
    if not names:
        return "none"
    shown = [shown_name(name) for name in names[:SHOWN_FIELD_NAMES]]
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
