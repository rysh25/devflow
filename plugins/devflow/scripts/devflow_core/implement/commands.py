"""The implement workflow's runner actions: one table for the parser and dispatch."""
from __future__ import annotations

import shutil

from ..common.hosts import executable_name, host_profile

from ..common.environment import python_errors
from ..common.errors import DevFlowError
from ..common.reviewers import readiness
from ..common.workflow import Command, Option


def doctor(engine, args, run_id, session) -> dict:
    """Local diagnostics of the frozen (or project) configuration; normally never calls a model.

    Each reviewer is checked in the directory a review would start it in: the selected run's
    verified workspace, or the project when no run is selected (``start`` checks the workspace
    it chooses)."""
    workflow = engine.workflow
    config = engine.read_frozen_config(run_id) if run_id else workflow.load_config(args.settings_documents)
    source = "frozen_run" if run_id else "project_settings"
    if not run_id and session:
        from ..common.startup import apply_startup, parse_start
        from ..common.operations import public_command
        from .config import validate_config
        pending = getattr(args, "pending_start", None)
        if pending and pending["flow"] == workflow.name:
            prompt = pending["event"]["payload"]["prompt"]
            config = validate_config(apply_startup(config, parse_start(public_command(prompt, engine.host)[1])))
            source = "pending_start"
    # Resolved-settings diagnostics run before any reviewer CLI probe; a selected
    # run's frozen configuration keeps its own comparison path instead.
    workflow_report = workflow.doctor(engine.project, config, host=engine.host, frozen=bool(run_id))
    reviewer_cwd, results = engine.project, None
    if run_id:
        from .workspace import verify_workspace
        frozen = engine.store.read(run_id)["workspace"]
        try:
            reviewer_cwd = verify_workspace(engine.project, frozen, config["snapshot"]["mode"])
        except DevFlowError as exc:
            # Never start a reviewer CLI in a workspace that failed verification; report the
            # run's recorded workspace, the directory its reviews would start in.
            reviewer_cwd = frozen["path"]
            results = [{"id": provider["id"], "ready": False,
                        "error": f"Workspace verification failed before any reviewer CLI ran: {exc}"}
                       for provider in config["reviewers"]]
    if results is None:
        results = [readiness(provider, reviewer_cwd) for provider in config["reviewers"]]
    errors = python_errors()
    return {"schema_version": 1, "workflow": workflow.name,
            "project": str(engine.project), "run_id": run_id, "config": config, "reviewers": results,
            "reviewer_cwd": str(reviewer_cwd),
            "configuration_source": source,
            "executables": {name: shutil.which(name) for name in ("uv", executable_name(engine.host), "git")},
            "ready": all(item["ready"] for item in results) and not errors, "errors": errors,
            "warnings": workflow_report.get("warnings", [])
                        + [text for item in results for text in item.get("details", {}).get("version_warnings", [])],
            "not_verified": ["CLI authentication and model access", "Actual model used by provider",
                             "Reviewer CLI checks in any directory other than reviewer_cwd",
                             f"Real {host_profile(engine.host).label} hook execution", "Native Windows/macOS behavior"],
            "note": ("Local reviewer CLI checks only: --version and --help in the runner's directory, and each CLI's "
                     "check of reviewer_cwd (Devin: whether it trusts the directory). The directory check fails only "
                     "when the CLI clearly refuses; ready is not proof of trust (a check that cannot decide passes). "
                     "Without a selected run reviewer_cwd is the project, and start checks the workspace it chooses. "
                     "Does not install tools or authenticate; normally no model calls (docs/security.md).")}


COMMANDS = (
    Command("status", "Inspect the bound or named implementation run", lambda e, a, run, s: e.status(run), needs_owner=False),
    Command("doctor", "Local reviewer CLI and capability diagnostics; normally no model calls", doctor,
            needs_run=False, needs_owner=False),
    Command("checkpoint", "Freeze the current code and an implementation summary",
            lambda e, a, run, s: e.checkpoint(run, s, a.summary_file), (Option("--summary-file", "draft"),)),
    Command("check", "Run the frozen, pre-approved project checks once", lambda e, a, run, s: e.check(run, s)),
    Command("review", "Dispatch the next reviewer and reserve one attempt; at the review limit, accept by the run's frozen setting or ask the human instead",
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
