"""Runner actions of the implement workflow (registered into the single DevFlow CLI)."""
from __future__ import annotations

from pathlib import Path

import shutil

from .. import __version__
from ..common.errors import DevFlowError
from ..common.reviewers import preflight
from .config import load_config
from .engine import Engine
from .workflow import WORKFLOW


def register(commands) -> None:
    commands.add_parser("status", help="Inspect the bound or named implementation run")
    commands.add_parser("doctor", help="Offline executable/capability diagnostics; never calls a model")
    checkpoint = commands.add_parser("checkpoint", help="Freeze the current code and an implementation summary")
    checkpoint.add_argument("--summary-file", type=Path, required=True)
    commands.add_parser("check", help="Run the frozen, pre-approved project checks once")
    commands.add_parser("review", help="Dispatch exactly the next reviewer and reserve one attempt")
    reflect = commands.add_parser("reflect", help="Record a disposition for every finding of the last review")
    reflect.add_argument("--decisions-file", type=Path, required=True)
    escalate = commands.add_parser("escalate", help="Ask the human: JSON {reason:string,questions:string[]}")
    escalate.add_argument("--questions-file", type=Path, required=True)
    resolve = commands.add_parser("resolve", help="Resolve with the event ID of a recorded human answer")
    resolve.add_argument("--event-id", required=True)
    resolve.add_argument("--decision", choices=["continue", "cancel"], required=True)
    resolve.add_argument("--reason", required=True)
    cancel = commands.add_parser("cancel", help="Keep all audit data and stop the run")
    cancel.add_argument("--reason", required=True)
    recover = commands.add_parser("recover", help="HUMAN ONLY: first verify no associated process remains alive")
    recover.add_argument("--confirm-process-stopped", action="store_true")


def doctor(project: Path, run_id: str | None) -> dict:
    """Offline diagnostics of the frozen (or project) configuration; never calls a model."""
    engine = Engine(project)
    config = engine.store.read(run_id)["config"] if run_id else load_config(project, WORKFLOW.resource_dir)
    results = []
    for provider in config["reviewers"]:
        try:
            results.append({"id": provider["id"], "ready": True, "details": preflight(provider)})
        except DevFlowError as exc:
            results.append({"id": provider["id"], "ready": False, "error": str(exc)})
    return {"schema_version": 1, "plugin": "devflow", "workflow": "implement", "version": __version__,
            "project": str(project), "run_id": run_id, "config": config, "reviewers": results,
            "executables": {name: shutil.which(name) for name in ("uv", "claude", "git")},
            "ready": all(item["ready"] for item in results),
            "warnings": WORKFLOW.doctor(project, config)["warnings"],
            "not_verified": ["CLI authentication and model access", "Actual model used by provider",
                             "Real Claude Code hook execution", "Native Windows/macOS behavior"],
            "note": "Offline help/version probes only. Does not install tools, authenticate, or incur model usage."}


def run(args, project: Path, run_id: str | None, session: str | None):
    engine = Engine(project)
    action = args.action
    if action == "doctor":
        return doctor(project, run_id)
    if not run_id:
        raise DevFlowError("No implementation run is bound; use /devflow:implement PLAN or /devflow:resume RUN_ID")
    if action == "status":
        return engine.status(run_id)
    if action == "checkpoint":
        return engine.checkpoint(run_id, session, args.summary_file)
    if action == "check":
        return engine.check(run_id, session)
    if action == "review":
        return engine.review(run_id, session)
    if action == "reflect":
        return engine.reflect(run_id, session, args.decisions_file)
    if action == "escalate":
        return engine.escalate(run_id, session, args.questions_file)
    if action == "resolve":
        return engine.resolve(run_id, session, args.event_id, args.decision, args.reason)
    if action == "cancel":
        return engine.cancel(run_id, session, args.reason)
    if action == "recover":
        return engine.recover(run_id, args.confirm_process_stopped)
    raise DevFlowError("Unknown implement action")
