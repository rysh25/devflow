"""The review summary every workflow shows: one data shape, one Markdown form.

Each workflow converts its own records into a ``source`` (one entry per review
attempt and the run's approval facts); ``build`` adds what follows from those
facts, and ``render`` turns the data into fixed English Markdown that Claude shows
unchanged. Nothing is shortened. The models and efforts are the requested ones,
never a claim about what the provider ran.

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
                  "tokens", "record"}],
     "human_decisions": [{"at", "review_number", "questions", "answer", "decision",
                          "interpretation", "event_ids"}],
     "human_acceptance": {"event_ids", "answer", "version", "accepted_at", "record"} | None}

A review's ``status`` is ``succeeded``, ``failed`` or ``in_progress``; a missing
disposition (``action``/``reason``) is None, shown as undecided. ``cli_version`` comes
from the attempt's process record named by ``cli_version_source``, which is not a
hashed run record.
"""
from __future__ import annotations

from datetime import datetime, timedelta

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


def build(source: dict, now: str) -> dict:
    """The summary data: the source's reviews with times, limits and counts, the
    approval state, the remaining findings, the trend and the time totals."""
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
    approval_reviewers = []
    remaining, count = [], 0
    for reviewer in source["reviewers"]:
        own = [entry for entry in reviews if entry["reviewer_id"] == reviewer["id"]]
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


def _yes(value: bool) -> str:
    return "yes" if value else "no"


def _version(version: dict) -> str:
    return version["label"] + (f" ({version['id']})" if version.get("id") else "")


def _status(summary: dict) -> str:
    status = summary["status"]
    if status == "awaiting_human" and summary["pending_question"] == "review_budget":
        return "awaiting_human (review limit question)"
    return STATUS_TEXT.get(status, status)


def _finding(finding: dict, indent: str) -> list[str]:
    decision = ("undecided" if finding["action"] is None
                else f"{finding['action']} — {finding['reason']}")
    return [f"{indent}- {finding['id']} [{finding['severity']}] {finding['category']} — {finding['location']}",
            f"{indent}  - Recommendation: {finding['recommendation']}",
            f"{indent}  - Decision: {decision}"]


def _changes(fix: dict) -> str | None:
    change = fix.get("version_change")
    if change is not None:
        return (f"{change['from']} → {change['to']} "
                f"(+{_known(change['lines_added'])} −{_known(change['lines_removed'])} lines)")
    files = fix.get("file_changes")
    if files is not None:
        return "; ".join(f"{item['change']} {item['path']}" for item in files) or "no file changes"
    return None


def render(summary: dict) -> str:
    """The fixed Markdown form of ``build``'s data."""
    approval, time = summary["approval"], summary["time"]
    lines = [f"## DevFlow review summary — {summary['flow']} run {summary['run_id']}", ""]
    lines.append(f"- Status: {_status(summary)}")
    lines.append(f"- Reviews: {summary['review_count']} of limit {summary['review_limit']}")
    missing = time["reviews_without_time"]
    lines.append(f"- Time: run {_duration(time['run_elapsed_seconds'])}, reviews {_duration(time['review_seconds_total'])}"
                 + (f" ({missing} review{'s' if missing != 1 else ''} without recorded time)" if missing else ""))
    lines.append(f"- Current version: {_version(approval['current_version'])}; "
                 f"reviewed: {_yes(approval['latest_version_reviewed'])}")
    lines.append("- Approvals of the current version: "
                 + (", ".join(f"{item['id']} {_yes(item['approves_current'])}" for item in approval["reviewers"])
                    or "none"))
    lines.append("- Approved versions: " + ("; ".join(
        f"{item['id']} " + (", ".join(f"{entry['version']['label']} (review {entry['review_number']})"
                                      for entry in item["approved"]) or "none")
        for item in approval["reviewers"]) or "none"))
    acceptance = approval["human_acceptance"]
    lines.append("- Human acceptance: " + (
        "none" if acceptance is None else
        f"event {', '.join(acceptance['event_ids'])}, version {_known(acceptance['version'])}, "
        f"at {_known(acceptance['accepted_at'])} ({acceptance['record']})"))

    lines += ["", "### Finding counts", ""]
    if summary["reviews"]:
        lines += ["| Review | Reviewer | Result | " + " | ".join(s.capitalize() for s in SEVERITIES) + " |",
                  "|---|---|---|" + "---|" * len(SEVERITIES)]
        for entry in summary["reviews"]:
            result = entry["verdict"] if entry["status"] == "succeeded" else entry["status"]
            counts = entry["finding_counts"]
            cells = [str(counts[s]) for s in SEVERITIES] if counts else [""] * len(SEVERITIES)
            lines.append(f"| {entry['number']} | {entry['reviewer_id']} | {result} | " + " | ".join(cells) + " |")
    else:
        lines.append("No reviews yet.")

    findings_by_review = {}
    for entry in summary["reviews"]:
        findings_by_review[entry["number"]] = {finding["id"]: finding for finding in entry["findings"]}
        lines += ["", f"### Review {entry['number']} — {entry['reviewer_id']}", ""]
        lines.append(f"- Model: {_known(entry['requested_model'])} (requested), "
                     f"effort: {_known(entry['requested_effort'])} (requested), CLI: {_known(entry['cli_version'])} "
                     f"(process record {entry['cli_version_source']}, not hashed)")
        lines.append(f"- Started: {_known(entry['started_at'])}, finished: {_known(entry['finished_at'])}, "
                     f"time: {_duration(entry['duration_seconds'])} of {_duration(entry['timeout_seconds'])}")
        counts = entry["finding_counts"]
        lines.append(f"- Version: {entry['version']['label']}; status: {entry['status']}; "
                     f"verdict: {_known(entry['verdict'])}"
                     + ("; findings: " + ", ".join(f"{s} {counts[s]}" for s in SEVERITIES) if counts else ""))
        tokens = entry["tokens"]
        lines.append("- Tokens: " + ("unknown" if not tokens else
                                     ", ".join(f"{name} {value:,}" for name, value in tokens.items())))
        lines.append(f"- Record: {_known(entry['record'])}")
        if entry["error"]:
            lines.append(f"- Failure: {entry['error']}")
        if entry["status"] == "succeeded":
            if entry["findings"]:
                lines.append("- Findings:")
                for finding in entry["findings"]:
                    lines += _finding(finding, "  ")
            else:
                lines.append("- Findings: none")
        fix = entry["fix"]
        if fix is not None:
            lines.append(f"- Fix: {fix['summary']}")
            changed = _changes(fix)
            if changed is not None:
                lines.append(f"- Changed: {changed}")

    remaining = summary["remaining_findings"]
    lines += ["", f"### Remaining findings ({remaining['count']})", ""]
    if remaining["items"]:
        records = {entry["number"]: entry["record"] for entry in summary["reviews"]}
        for item in remaining["items"]:
            lines.append(f"- {item['reviewer_id']}, review {item['review_number']} "
                         f"({_known(records.get(item['review_number']))})"
                         + ("" if item["finding_ids"] else ": no findings (human questions only)"))
            for identifier in item["finding_ids"]:
                lines += _finding(findings_by_review[item["review_number"]][identifier], "  ")
    else:
        lines.append("none")

    lines += ["", "### Human decisions", ""]
    if summary["human_decisions"]:
        for decision in summary["human_decisions"]:
            where = f", review {decision['review_number']}" if decision["review_number"] is not None else ""
            text = (f"- {_known(decision['at'])}{where}: question: {' / '.join(decision['questions'])} — "
                    f"answer: {decision['answer']} — decision: {_known(decision['decision'])}")
            if decision["interpretation"]:
                text += f" — interpretation: {decision['interpretation']}"
            lines.append(text + f" (event {', '.join(decision['event_ids'])})")
    else:
        lines.append("none")
    return "\n".join(lines) + "\n"
