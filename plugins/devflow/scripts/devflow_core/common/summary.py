"""The review summary every workflow shows: one data shape, one short Markdown form.

Each workflow converts its own records into a ``source`` (one entry per review
attempt and the run's approval facts); ``build`` adds what follows from those
facts, and ``render`` turns the data into fixed English Markdown that Claude shows
unchanged. The data holds everything; the Markdown shows what a decision needs (one
table row per review, one line per reviewer, and the failures, remaining findings
and human decisions), and the rest is read from the data and the run's records.
Text is never cut. The models and efforts are the requested ones, never a claim
about what the provider ran.

``source``::

    {"flow", "run_id", "status", "pending_question", "created_at", "finished_at",
     "review_count", "review_limit",
     "reviewers": [{"id", "requested_model", "requested_effort", "timeout_seconds"}],
     "current_version": {"id", "label"}, "latest_version_reviewed", "approving_current": [ids],
     "reviews": [{"number", "reviewer_id", "status", "cli_version", "cli_version_source",
                  "started_at", "finished_at",
                  "version": {"id", "label"}, "verdict", "error",
                  "findings": [{"id", "severity", "category", "location", "recommendation",
                                "action", "reason"}],
                  "fix": {"summary", "version_change", "file_changes"} | None,
                  "tokens", "tool_call_count", "record"}],
     "human_decisions": [{"at", "review_number", "question_kind", "questions", "answer",
                          "decision", "interpretation", "event_ids"}],
     "human_acceptance": {"event_ids", "answer", "version", "accepted_at", "record"} | None}

A review's ``status`` is ``succeeded``, ``failed`` or ``in_progress``; a missing
disposition (``action``/``reason``) is None, shown as undecided. ``cli_version`` comes
from the attempt's process record named by ``cli_version_source``, which is not a
hashed run record. ``tool_call_count`` is None when the reviewer did not run to the
end. A human decision's ``review_number`` is the number of reviews when the question
was asked and ``question_kind`` the kind the question was recorded with (None when
an older record has neither).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .review import REVIEW_BUDGET
from .storage import json_bytes

SCHEMA_VERSION = 1
#: Why a saved report holds no summary although one was built: the summary is never cut.
TOO_LARGE = ("The review summary is larger than the 64 MiB record limit; "
             "read the review records listed in reviews")
SEVERITIES = ("critical", "high", "medium", "low")
STATUS_TEXT = {
    "completed": "completed — every reviewer approved the same version",
    "accepted": "accepted — accepted by the human at the review limit, not approved by every reviewer",
    "exhausted": "exhausted — ended without approval from every reviewer",
    "cancelled": "cancelled",
}


def _seconds(start: object, end: object) -> int | None:
    """Whole seconds between two recorded times (``now()`` form), or None."""
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        return round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())
    except ValueError:
        return None


def _reviewer_totals(reviewer: dict, own: list[dict]) -> dict:
    """One reviewer over the run: its requested model and effort, the CLI versions its
    reviews recorded (in order, once each) and its token counts summed by the names its
    CLI printed. Counts are never added across CLIs. A total a record cannot hold (past
    Python's integer-to-text limit) is unknown, as a single review's counts are."""
    versions = []
    for entry in own:
        if entry["cli_version"] is not None and entry["cli_version"] not in versions:
            versions.append(entry["cli_version"])
    tokens, without = {}, 0
    for entry in own:
        if not entry["tokens"]:
            without += 1
            continue
        for name, value in entry["tokens"].items():
            tokens[name] = tokens.get(name, 0) + value
    try:
        json_bytes(tokens)
    except ValueError:
        tokens = {}
    return {"id": reviewer["id"], "requested_model": reviewer["requested_model"],
            "requested_effort": reviewer["requested_effort"], "cli_versions": versions,
            "tokens": dict(sorted(tokens.items())) or None, "reviews_without_tokens": without}


def build(source: dict, now: str) -> dict:
    """The summary data: the source's reviews with times, limits and counts, the
    approval state, the remaining findings, the trend, each reviewer's totals and
    the time totals."""
    limits = {item["id"]: item for item in source["reviewers"]}
    reviews = []
    for entry in source["reviews"]:
        reviewer = limits.get(entry["reviewer_id"], {})
        counts = None
        if entry["status"] == "succeeded":
            counts = {severity: sum(1 for finding in entry["findings"] if finding["severity"] == severity)
                      for severity in SEVERITIES}
        reviews.append({**entry, "requested_model": reviewer.get("requested_model"),
                        "requested_effort": reviewer.get("requested_effort"),
                        "timeout_seconds": reviewer.get("timeout_seconds"),
                        "duration_seconds": _seconds(entry["started_at"], entry["finished_at"]),
                        "finding_counts": counts})
    approving = set(source["approving_current"])
    approval_reviewers, totals = [], []
    remaining, count = [], 0
    for reviewer in source["reviewers"]:
        own = [entry for entry in reviews if entry["reviewer_id"] == reviewer["id"]]
        totals.append(_reviewer_totals(reviewer, own))
        approval_reviewers.append({
            "id": reviewer["id"], "approves_current": reviewer["id"] in approving,
            "approved": [{"review_number": entry["number"], "version": entry["version"]}
                         for entry in own if entry["status"] == "succeeded" and entry["verdict"] == "pass"]})
        # A reviewer's remaining findings are those of its latest succeeded review, unless it passed.
        latest = next((entry for entry in reversed(own) if entry["status"] == "succeeded"), None)
        if latest is not None and latest["verdict"] != "pass":
            identifiers = [finding["id"] for finding in latest["findings"]]
            remaining.append({"reviewer_id": reviewer["id"], "review_number": latest["number"],
                              "finding_ids": identifiers})
            count += len(identifiers)
    durations = [entry["duration_seconds"] for entry in reviews]
    return {
        "schema_version": SCHEMA_VERSION, "flow": source["flow"], "run_id": source["run_id"],
        "status": source["status"], "pending_question": source["pending_question"],
        "review_count": source["review_count"], "review_limit": source["review_limit"],
        "reviews": reviews,
        "finding_trend": [{"number": entry["number"], "reviewer_id": entry["reviewer_id"], "status": entry["status"],
                           "counts": entry["finding_counts"]} for entry in reviews],
        "approval": {
            "current_version": source["current_version"],
            "latest_version_reviewed": source["latest_version_reviewed"],
            "all_reviewers_approved": bool(source["reviewers"]) and all(
                reviewer["id"] in approving for reviewer in source["reviewers"]),
            "reviewers": approval_reviewers,
            "human_acceptance": source["human_acceptance"]},
        "remaining_findings": {"count": count, "items": remaining},
        "reviewer_totals": totals,
        "human_decisions": source["human_decisions"],
        "time": {"run_elapsed_seconds": _seconds(source["created_at"], source["finished_at"] or now),
                 "review_seconds_total": sum(value for value in durations if value is not None),
                 "reviews_without_time": sum(1 for value in durations if value is None)},
    }


# -- Markdown ------------------------------------------------------------------------------
def _known(value: object) -> str:
    return "unknown" if value is None else str(value)


def _duration(seconds: object) -> str:
    return "unknown" if seconds is None else str(timedelta(seconds=int(seconds)))


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" + ("" if number == 1 else "s")


def _status(summary: dict) -> str:
    status = summary["status"]
    if status == "awaiting_human" and summary["pending_question"] == REVIEW_BUDGET:
        return "awaiting_human (review limit question)"
    return STATUS_TEXT.get(status, status)


def _findings(entry: dict) -> str:
    """A succeeded review's findings: the count per severity, then per disposition."""
    if entry["status"] != "succeeded":
        return "—"
    if not entry["findings"]:
        return "none"
    counts = entry["finding_counts"]
    dispositions = {}
    for finding in entry["findings"]:
        action = finding["action"] or "undecided"
        dispositions[action] = dispositions.get(action, 0) + 1
    order = sorted(action for action in dispositions if action != "undecided")
    order += ["undecided"] if "undecided" in dispositions else []
    return (", ".join(f"{severity} {counts[severity]}" for severity in SEVERITIES if counts[severity])
            + "; " + ", ".join(f"{action} {dispositions[action]}" for action in order))


def render(summary: dict) -> str:
    """The fixed Markdown form of ``build``'s data. Table cells hold only fixed values
    (numbers, IDs, labels, counts, times); free text goes to the lists below the table."""
    approval, time, reviews = summary["approval"], summary["time"], summary["reviews"]
    lines = [f"## DevFlow review summary — {summary['flow']} run {summary['run_id']}", ""]
    lines.append(f"- Status: {_status(summary)}")
    missing = time["reviews_without_time"]
    lines.append(f"- Reviews: {summary['review_count']} of limit {summary['review_limit']}; "
                 f"time: run {_duration(time['run_elapsed_seconds'])}, reviews {_duration(time['review_seconds_total'])}"
                 + (f" ({_count(missing, 'review')} without recorded time)" if missing else ""))
    approvals = ", ".join(f"{item['id']} {'yes' if item['approves_current'] else 'no'}"
                          for item in approval["reviewers"]) or "none"
    lines.append(f"- Current version: {approval['current_version']['label']}, "
                 f"{'reviewed' if approval['latest_version_reviewed'] else 'not reviewed'}; approvals: {approvals}")

    lines.append("")
    if reviews:
        lines += ["| # | Reviewer | Version | Result | Findings | Tool calls | Time |",
                  "|---|---|---|---|---|---|---|"]
        for entry in reviews:
            result = entry["verdict"] if entry["status"] == "succeeded" else entry["status"]
            lines.append(f"| {entry['number']} | {_known(entry['reviewer_id'])} | {entry['version']['label']} | "
                         f"{_known(result)} | {_findings(entry)} | {_known(entry['tool_call_count'])} | "
                         f"{_duration(entry['duration_seconds'])} of {_duration(entry['timeout_seconds'])} |")
    else:
        lines.append("No reviews yet.")

    lines.append("")
    for item in summary["reviewer_totals"]:
        tokens = ("unknown" if item["tokens"] is None
                  else ", ".join(f"{name} {value:,}" for name, value in item["tokens"].items()))
        without = item["reviews_without_tokens"]
        lines.append(f"- {item['id']}: {_known(item['requested_model'])}, {_known(item['requested_effort'])} "
                     f"(requested), {', '.join(item['cli_versions']) or 'CLI unknown'}; tokens: {tokens}"
                     + (f" ({_count(without, 'review')} without token counts)" if without else ""))

    failures = [entry for entry in reviews if entry["status"] != "succeeded" and entry["error"]]
    if failures:
        lines.append("- Failures:")
        lines += [f"  - Review {entry['number']} ({_known(entry['reviewer_id'])}): {entry['error']}"
                  for entry in failures]
    else:
        lines.append("- Failures: none")

    remaining = summary["remaining_findings"]
    if remaining["items"]:
        findings = {entry["number"]: {finding["id"]: finding for finding in entry["findings"]} for entry in reviews}
        lines.append(f"- Remaining findings ({remaining['count']}):")
        for item in remaining["items"]:
            where = f"  - {item['reviewer_id']}, review {item['review_number']}"
            if not item["finding_ids"]:
                lines.append(f"{where}: no findings (human questions only)")
            for identifier in item["finding_ids"]:
                finding = findings[item["review_number"]][identifier]
                lines.append(f"{where}: {finding['id']} [{finding['severity']}] {finding['category']} — "
                             f"{finding['location']}: {finding['recommendation']}")
    else:
        lines.append("- Remaining findings: none")

    if summary["human_decisions"]:
        lines.append("- Human decisions:")
        for decision in summary["human_decisions"]:
            number = decision["review_number"]
            after = ("After an unknown number of reviews" if number is None
                     else f"After {_count(number, 'review')}")
            kind = decision["question_kind"] or "question"
            choice = _known(decision["decision"]) if kind == REVIEW_BUDGET else "answered"
            lines.append(f"  - {after}, {kind}: {choice} ({_known(decision['at'])})")
    else:
        lines.append("- Human decisions: none")
    return "\n".join(lines) + "\n"
