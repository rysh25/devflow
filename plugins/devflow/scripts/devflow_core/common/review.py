"""Shared budget, approval and verdict mechanics; domain policy remains in each flow."""
import re
from typing import Callable, Collection

from .errors import DevFlowError, ProcessCleanupError, ReviewerError, shown_name
from .hosts import host_profile
from .runs import TERMINAL
from .storage import now

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


#: How the run handles the review limit question: ``ask`` (every time the human),
#: ``continue`` (the host's recommendation to continue is applied by ``limit-auto``)
#: or ``continue_accept`` (also its recommendation to accept). Frozen at start.
LIMIT_AUTO_KEY = "review_limit_auto"
LIMIT_AUTO_MODES = ("ask", "continue", "continue_accept")
#: How many reviews automatic continuation may add beyond the frozen max_reviews.
LIMIT_AUTO_EXTRA_KEY = "review_limit_auto_extra_reviews"
#: The immutable records of automatic decisions: ``limit-auto/<question id>-<decision>.json``.
LIMIT_AUTO_DIR = "limit-auto/"
LIMIT_AUTO_SOURCE = "review_limit_auto"
LIMIT_AUTO_DECISIONS = ("continue", "accept")


def check_limit_auto(config: dict) -> None:
    """Both review limit settings are present with their exact JSON types.

    Python's ``True == 1`` makes a written ``true`` equal to a frozen 1 (and
    ``6.0`` to 6), so this runs on both sides of every frozen-configuration
    comparison (this and ``common.engine``'s). No upper bound: a value past the
    99-review ceiling works as the ceiling.
    """
    mode = config.get(LIMIT_AUTO_KEY)
    if type(mode) is not str or mode not in LIMIT_AUTO_MODES:
        raise DevFlowError('review_limit_auto must be "ask", "continue" or "continue_accept"')
    extra = config.get(LIMIT_AUTO_EXTRA_KEY)
    if type(extra) is not int or extra < 1:
        raise DevFlowError("review_limit_auto_extra_reviews must be an integer of at least 1")


def limit_auto_allows(config: dict, decision: str) -> bool:
    """Whether the frozen setting lets ``limit-auto`` apply this decision."""
    mode = config[LIMIT_AUTO_KEY]
    if decision == "continue":
        return mode in ("continue", "continue_accept")
    return decision == "accept" and mode == "continue_accept"


def auto_continue_fits(state: dict) -> bool:
    """Whether one more round of reviewers stays within review_limit_auto_extra_reviews
    beyond the frozen max_reviews (reviews a human added count too).

    The 99-review ceiling is not checked here: the question is open only when
    ``ask_at_limit`` found that one more round fits under it.
    """
    config = state["config"]
    return (state["review_limit"] + len(config["reviewers"]) - config["max_reviews"]
            <= config[LIMIT_AUTO_EXTRA_KEY])


def serious_reviewers(reviewers: list, reviews: list) -> tuple[list[dict], list[str]]:
    """The configured reviewers whose latest succeeded review holds a critical or
    high finding, whatever its disposition (``[{"reviewer_id", "review_number"}]``,
    in configured order), and the reviewers with no succeeded review in the run.

    ``reviews`` holds the summary source's entries (``reviewer_id``, ``number``,
    ``status``, ``findings`` with ``severity``). The one mechanical part of the
    acceptance criterion: the summary shows it and ``limit-auto`` accept requires
    the first list to be empty.
    """
    serious, never = [], []
    for reviewer in reviewers:
        latest = next((entry for entry in reversed(reviews)
                       if entry["reviewer_id"] == reviewer["id"] and entry["status"] == "succeeded"), None)
        if latest is None:
            never.append(reviewer["id"])
        elif any(finding["severity"] in ("critical", "high") for finding in latest["findings"]):
            serious.append({"reviewer_id": reviewer["id"], "review_number": latest["number"]})
    return serious, never


def check_no_serious_reviewers(source: dict) -> None:
    """Refuse an automatic accept while a configured reviewer's latest succeeded
    review holds a critical or high finding (``source``: a summary source)."""
    serious = serious_reviewers(source["reviewers"], source["reviews"])[0]
    if serious:
        named = "; ".join(f"{item['reviewer_id']}'s latest succeeded review (review {item['review_number']})"
                          for item in serious)
        raise DevFlowError(f"Cannot accept automatically: {named} has a critical or high finding. "
                           "Ask the human the review limit question.")


def limit_auto_name(escalation_id: str, decision: str) -> str:
    return f"{LIMIT_AUTO_DIR}{escalation_id}-{decision}.json"


def limit_auto_record(state: dict, escalation: dict, decision: str, recommendation: str,
                      version: str, version_reviewed: bool, session: str) -> dict:
    """The immutable record of one automatic decision, made before the state changes."""
    config, before = state["config"], state["review_limit"]
    return {"schema_version": 1, "source": LIMIT_AUTO_SOURCE, "escalation_id": escalation["id"],
            "decision": decision, LIMIT_AUTO_KEY: config[LIMIT_AUTO_KEY],
            LIMIT_AUTO_EXTRA_KEY: config[LIMIT_AUTO_EXTRA_KEY],
            "review_number": escalation["detail"]["review_number"], "review_limit_before": before,
            "review_limit_after": before + len(config["reviewers"]) if decision == "continue" else before,
            "version": version, "version_reviewed": bool(version_reviewed), "recommendation": recommendation,
            "decided_at": now(), "host": state["owner_host"], "session": session}


def check_limit_auto_names(state: dict, id_pattern: str) -> None:
    """The state-only invariant of automatic decisions (records are not read).

    Every ``limit-auto/`` record name holds a question ID of the flow's form
    and a decision; an ``auto_accepted`` run has the frozen ``continue_accept``
    and exactly one accept record, and no other status has one.
    """
    accepts = 0
    for name in state["artifacts"]:
        if not name.startswith(LIMIT_AUTO_DIR):
            continue
        match = re.fullmatch(re.escape(LIMIT_AUTO_DIR) + f"({id_pattern})-(continue|accept)\\.json", name)
        if not match:
            raise DevFlowError(f"Invalid automatic decision record name: {name}")
        accepts += match.group(2) == "accept"
    if state["status"] == "auto_accepted":
        if state["config"][LIMIT_AUTO_KEY] != "continue_accept" or accepts != 1:
            raise DevFlowError("An auto_accepted run needs review_limit_auto continue_accept "
                               "and exactly one automatic accept record")
    elif accepts:
        raise DevFlowError("Only an auto_accepted run may hold an automatic accept record")


def check_limit_auto_record(state: dict, record: object, name: str, read, workflow) -> dict:
    """Verify one automatic decision record against the run's frozen facts.

    ``read(relative)`` reads a digest-verified record (the question it closed);
    ``workflow`` gives the flow's question record path and review count. Called
    when the decision is made and whenever a summary, report, publication or
    status presents it.
    """
    match = re.fullmatch(re.escape(LIMIT_AUTO_DIR) + r"(.+)-(continue|accept)\.json", name)
    config = state["config"]
    try:
        if not match or not isinstance(record, dict) or set(record) != LIMIT_AUTO_FIELDS:
            raise DevFlowError("fields")
        escalation_id, decision = match.groups()
        question = read(workflow.limit_auto_question(state, escalation_id))
        detail = question.get("detail") if isinstance(question, dict) else None
        before, after, number = (record["review_limit_before"], record["review_limit_after"],
                                 record["review_number"])
        if (type(record["schema_version"]) is not int or record["schema_version"] != 1
                or record["source"] != LIMIT_AUTO_SOURCE
                or record["decision"] != decision or record["escalation_id"] != escalation_id
                or question.get("id") != escalation_id or not isinstance(detail, dict)
                or detail.get("kind") != REVIEW_BUDGET
                or any(type(value) is not int for value in (before, after, number))
                or number != detail.get("review_number")
                # The question opens when the review count reaches the limit.
                or before != number
                or type(record[LIMIT_AUTO_KEY]) is not str or record[LIMIT_AUTO_KEY] != config[LIMIT_AUTO_KEY]
                or type(record[LIMIT_AUTO_EXTRA_KEY]) is not int
                or record[LIMIT_AUTO_EXTRA_KEY] != config[LIMIT_AUTO_EXTRA_KEY]
                or type(record["version_reviewed"]) is not bool
                or any(not isinstance(record[key], str) for key in
                       ("version", "decided_at", "host", "session", "recommendation"))
                or not record["recommendation"].strip()):
            raise DevFlowError("values")
        if decision == "continue":
            if after != before + len(config["reviewers"]):
                raise DevFlowError("limit")
        elif (before != after or after != state["review_limit"]
              or number != workflow.summary(state)["review_count"]):
            raise DevFlowError("limit")
    except (DevFlowError, KeyError, TypeError, AttributeError, ValueError) as exc:
        raise DevFlowError(f"Invalid or mismatched automatic decision record: {name}: {exc}") from exc
    return record


LIMIT_AUTO_FIELDS = frozenset({"schema_version", "source", "escalation_id", "decision", LIMIT_AUTO_KEY,
                               LIMIT_AUTO_EXTRA_KEY, "review_number", "review_limit_before",
                               "review_limit_after", "version", "version_reviewed", "recommendation",
                               "decided_at", "host", "session"})


def limit_auto_records(state: dict, store) -> list[tuple[str, dict]]:
    """Every automatic decision record of the run, verified, in review order.

    Records are read through the run store's digest-checked reader."""
    def read(relative: str) -> dict:
        return store.record_json(state, relative)
    entries = [(name, check_limit_auto_record(state, read(name), name, read, store.workflow))
               for name in state["artifacts"] if name.startswith(LIMIT_AUTO_DIR)]
    return sorted(entries, key=lambda item: item[1]["review_number"])


def limit_auto_decisions(entries: list[tuple[str, dict]]) -> list[dict]:
    """The ``status`` view of the automatic decisions."""
    return [{"escalation_id": record["escalation_id"], "review_number": record["review_number"],
             "decision": record["decision"], "record": name} for name, record in entries]


def automatic_decisions(entries: list[tuple[str, dict]]) -> list[dict]:
    """The summary source's view of the automatic decisions."""
    return [{"at": record["decided_at"], "review_number": record["review_number"], "decision": record["decision"],
             "setting": record[LIMIT_AUTO_KEY], "review_limit_before": record["review_limit_before"],
             "review_limit_after": record["review_limit_after"], "recommendation": record["recommendation"],
             "record": name} for name, record in entries]


def accept_entry(entries: list[tuple[str, dict]]) -> tuple[str, dict] | None:
    """The run's automatic accept record, if any (``check_limit_auto_names`` allows one)."""
    return next(((name, record) for name, record in entries if record["decision"] == "accept"), None)


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

    Returns False while reviews remain. At the limit, when one more round would
    pass the ceiling the run ends as exhausted without a question; otherwise the
    review_budget question opens (``limit-auto`` may answer it for the run's
    frozen review_limit_auto setting). The bare presence of an earlier reviewer
    failure (``last_error``) is not part of the decision. ``approved`` holds the
    reviewers approving the current version, and ``version`` names that version
    (a plan hash or snapshot ID).
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


def decide_at_limit(state: dict, decision: str, return_status: str, accepted_status: str = "accepted") -> None:
    """Apply an answer to the review_budget question: continue adds one round of
    reviewers and returns to ``return_status``; stop ends as exhausted; accept ends
    as ``accepted_status`` (``auto_accepted`` for ``limit-auto``). Stop and accept
    change neither the limit nor approvals."""
    if decision == "continue":
        state["review_limit"] += len(state["config"]["reviewers"])
        state["status"] = return_status
    elif decision == "stop":
        state["status"] = "exhausted"
    elif decision == "accept":
        state["status"] = accepted_status
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
    check_limit_auto(config)
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
