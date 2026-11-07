"""The implement workflow's runner actions: one table for the parser and dispatch."""
from __future__ import annotations

import shutil

from ..common.hosts import executable_name

from .. import __version__
from ..common.environment import python_errors
from ..common.errors import DevFlowError
from ..common.reviewers import preflight
from ..common.workflow import Command, Option


def doctor(engine, args, run_id, session) -> dict:
    """Offline diagnostics of the frozen (or project) configuration; never calls a model."""
    workflow = engine.workflow
    config = engine.read_frozen_config(run_id) if run_id else workflow.load_config(engine.project)
    source = "frozen_run" if run_id else "project_settings"
    if not run_id and session:
        from ..common.startup import parse_start, override_reviewers
        from ..common.operations import public_command
        from .config import validate_config
        pending = getattr(args, "pending_start", None)
        if pending and pending["flow"] == workflow.name:
            prompt = pending["event"]["payload"]["prompt"]
            config = validate_config(override_reviewers(config, parse_start(public_command(prompt, engine.host)[1])))
            source = "pending_start"
    results = []
    for provider in config["reviewers"]:
        try:
            results.append({"id": provider["id"], "ready": True, "details": preflight(provider)})
        except DevFlowError as exc:
            results.append({"id": provider["id"], "ready": False, "error": str(exc)})
    errors = python_errors()
    return {"schema_version": 1, "plugin": "devflow", "workflow": workflow.name, "version": __version__,
            "project": str(engine.project), "run_id": run_id, "config": config, "reviewers": results,
            "configuration_source": source,
            "executables": {name: shutil.which(name) for name in ("uv", executable_name(engine.host), "git")},
            "ready": all(item["ready"] for item in results) and not errors, "errors": errors,
            "warnings": workflow.doctor(engine.project, config, host=engine.host).get("warnings", [])
                        + [text for item in results for text in item.get("details", {}).get("version_warnings", [])],
            "not_verified": ["CLI authentication and model access", "Actual model used by provider",
                             "Real Claude Code hook execution", "Native Windows/macOS behavior"],
            "note": "Offline help/version probes only. Does not install tools, authenticate, or incur model usage."}


COMMANDS = (
    Command("status", "Inspect the bound or named implementation run", lambda e, a, run, s: e.status(run), needs_owner=False),
    Command("doctor", "Offline executable/capability diagnostics; never calls a model", doctor, needs_run=False, needs_owner=False),
    Command("checkpoint", "Freeze the current code and an implementation summary",
            lambda e, a, run, s: e.checkpoint(run, s, a.summary_file), (Option("--summary-file", "draft"),)),
    Command("check", "Run the frozen, pre-approved project checks once", lambda e, a, run, s: e.check(run, s)),
    Command("review", "Dispatch the next reviewer and reserve one attempt; at the review limit, ask the human instead",
            lambda e, a, run, s: e.review(run, s)),
    Command("reflect", "Record a disposition for every finding of the last review",
            lambda e, a, run, s: e.reflect(run, s, a.decisions_file), (Option("--decisions-file", "draft"),)),
    Command("escalate", "Ask the human: JSON {reason:string,questions:string[]}",
            lambda e, a, run, s: e.escalate(run, s, a.questions_file), (Option("--questions-file", "draft"),)),
    Command("resolve", "Resolve with the event ID of a recorded human answer "
            "(continue/stop/accept for the review limit question, continue/cancel otherwise)",
            lambda e, a, run, s: e.resolve(run, s, a.event_id, a.decision, a.reason),
            (Option("--event-id", "event"), Option("--decision", "decision"), Option("--reason", "text"))),
    Command("cancel", "Keep all audit data and stop the run",
            lambda e, a, run, s: e.cancel(run, s, a.reason), (Option("--reason", "text"),)),
    Command("recover", "HUMAN ONLY: after confirming no associated process remains alive, mark it stopped",
            lambda e, a, run, s: e.recover(run, a.confirm_process_stopped),
            (Option("--confirm-process-stopped", "confirm"),), needs_owner=False),
)
