"""The plan workflow's runner actions: one table for the parser and dispatch."""
from __future__ import annotations

from ..common.environment import python_errors
from ..common.workflow import Command, Option
from .config import diagnose


def doctor(engine, args, run_id, session) -> dict:
    """Offline checks of the frozen (selected) or project configuration; repairs nothing."""
    frozen = engine.read_frozen_config(run_id) if run_id else None
    report = diagnose(engine.project, engine.workflow, effective_config=frozen)
    errors = python_errors()
    return {**report, "run_id": run_id or None, "warnings": engine.workflow.timeout_warnings(report["config"]),
            "errors": errors, "ready": report["ready"] and not errors}


COMMANDS = (
    Command("status", "Inspect the bound or named plan run", lambda e, a, run, s: e.status(run), needs_owner=False),
    Command("doctor", "Offline config/PATH checks only; no model calls", doctor, needs_run=False, needs_owner=False),
    Command("seal", "Freeze the verbatim interview and human decisions",
            lambda e, a, run, s: e.seal(run, a.decisions_file, a.context_file),
            (Option("--decisions-file", "draft"), Option("--context-file", "draft", required=False))),
    Command("plan", "Commit the initial plan from the draft directory",
            lambda e, a, run, s: e.plan(run, a.file), (Option("--file", "draft"),)),
    Command("review", "Dispatch exactly the next reviewer and reserve one attempt",
            lambda e, a, run, s: e.review(run)),
    Command("apply", "Commit this review's finding dispositions and the updated plan",
            lambda e, a, run, s: e.apply(run, a.plan_file, a.decisions_file, a.expected_hash),
            (Option("--plan-file", "draft"), Option("--decisions-file", "draft"), Option("--expected-hash", "hash"))),
    Command("pause", "Escalate: JSON {reason:string,questions:string[]}",
            lambda e, a, run, s: e.pause(run, a.reason_file), (Option("--reason-file", "draft"),)),
    Command("resolve", "Resolve with exact status.human_events answer_text and event_id values",
            lambda e, a, run, s: e.resolve(run, a.answer_file, s), (Option("--answer-file", "draft"),)),
    Command("cancel", "Keep all audit data and stop the run", lambda e, a, run, s: e.cancel(run)),
    Command("report", "Verify artifacts and produce a JSON handoff report", lambda e, a, run, s: e.report(run)),
    Command("publish", "Export the exact approved plan without overwriting any existing file",
            lambda e, a, run, s: e.publish(run)),
    Command("recover", "HUMAN ONLY: mark a stopped interrupted review as failed; consumes its attempt",
            lambda e, a, run, s: e.recover(run, a.confirm_process_stopped, expected_review=a.expected_review),
            (Option("--expected-review", "int"), Option("--confirm-process-stopped", "confirm")), needs_owner=False),
)
