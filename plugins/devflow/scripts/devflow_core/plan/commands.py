"""The plan workflow's runner actions: one table for the parser and dispatch."""
from __future__ import annotations

from ..common.environment import python_errors
from ..common.hosts.codex import diagnose_conversation
from ..common.workflow import Command, Option
from .config import diagnose


def doctor(engine, args, run_id, session) -> dict:
    """Check settings; probe Codex only for the explicit session owning this run."""
    frozen = engine.read_frozen_config(run_id) if run_id else None
    report = diagnose(engine.project, engine.workflow, effective_config=frozen, host=engine.host)
    errors = python_errors()
    checks = engine.workflow.doctor(engine.project, report["config"], host=engine.host)
    warnings = checks["warnings"]
    if engine.host == "codex":
        owner = engine.store.read(run_id) if run_id else None
        if owner and (owner["owner_host"] != engine.host or owner["owner_session"] != session):
            api = {**checks["conversation_api"],
                   "reason": "The caller's host and session do not match this run's owner session; API not probed",
                   "scope": "Frozen run settings only; no conversation API probe for a different owner"}
        else:
            api = diagnose_conversation(engine.project, session)
        report["conversation_api"] = api
        if api["checked"]:
            report["ready"] = report["ready"] and api["ready"]
            report["readiness_scope"] = "Settings/PATH and native conversation API initialization, root identity and first page only"
            report["note"] = "No model calls. Full pagination, question availability and later seal capture remain unverified."
    return {**report, "run_id": run_id or None, "warnings": warnings, "errors": errors,
            "ready": report["ready"] and not errors}


COMMANDS = (
    Command("status", "Inspect the bound or named plan run", lambda e, a, run, s: e.status(run), needs_owner=False),
    Command("doctor", "Config/PATH checks; Codex --session also probes the native conversation API; no model calls",
            doctor, needs_run=False, needs_owner=False),
    Command("seal", "Freeze the verbatim interview and human decisions",
            lambda e, a, run, s: e.seal(run, a.decisions_file),
            (Option("--decisions-file", "draft"),)),
    Command("plan", "Commit the initial plan from the draft directory",
            lambda e, a, run, s: e.plan(run, a.file), (Option("--file", "draft"),)),
    Command("review", "Dispatch the next reviewer and reserve one attempt; at the review limit, ask the human instead",
            lambda e, a, run, s: e.review(run)),
    Command("apply", "Commit this review's finding dispositions and the updated plan",
            lambda e, a, run, s: e.apply(run, a.plan_file, a.decisions_file, a.expected_hash),
            (Option("--plan-file", "draft"), Option("--decisions-file", "draft"), Option("--expected-hash", "hash"))),
    Command("pause", "Escalate: JSON {reason:string,questions:string[]}",
            lambda e, a, run, s: e.pause(run, a.reason_file), (Option("--reason-file", "draft"),)),
    Command("resolve", "Resolve with exact status.human_events answer_text and event_id values "
            "(and decision continue/stop/accept for the review limit question)",
            lambda e, a, run, s: e.resolve(run, a.answer_file, s), (Option("--answer-file", "draft"),)),
    Command("cancel", "Keep all audit data and stop the run", lambda e, a, run, s: e.cancel(run)),
    Command("report", "Verify artifacts and produce a JSON handoff report", lambda e, a, run, s: e.report(run)),
    Command("publish", "Export the exact approved plan without overwriting any existing file",
            lambda e, a, run, s: e.publish(run)),
    Command("recover", "HUMAN ONLY: mark a stopped interrupted review as failed; consumes its attempt",
            lambda e, a, run, s: e.recover(run, a.confirm_process_stopped, expected_review=a.expected_review),
            (Option("--expected-review", "int"), Option("--confirm-process-stopped", "confirm")), needs_owner=False),
)
