"""Runner actions of the plan workflow (registered into the single DevFlow CLI)."""
from __future__ import annotations

from pathlib import Path

from ..common.errors import DevFlowError
from ..common.storage import read_json
from .config import diagnose
from .engine import Engine

HELP = {
    "status": "Inspect the bound or named plan run",
    "doctor": "Offline config/PATH checks only; no model calls",
    "review": "Dispatch exactly the next reviewer and reserve one attempt",
    "cancel": "Keep all audit data and stop the run",
    "report": "Verify artifacts and produce a JSON handoff report",
    "publish": "Export the exact approved plan without overwriting any existing file",
}


def register(commands) -> None:
    for name, help_text in HELP.items():
        commands.add_parser(name, help=help_text)
    seal = commands.add_parser("seal", help="Freeze the verbatim interview and human decisions")
    seal.add_argument("--decisions-file", type=Path, required=True)
    seal.add_argument("--context-file", type=Path, help="Optional draft Markdown repository evidence, frozen into packets")
    plan = commands.add_parser("plan", help="Commit the initial plan from the draft directory")
    plan.add_argument("--file", type=Path, required=True)
    apply = commands.add_parser("apply", help="Commit this review's finding dispositions and the updated plan")
    apply.add_argument("--plan-file", type=Path, required=True)
    apply.add_argument("--decisions-file", type=Path, required=True)
    apply.add_argument("--expected-hash", required=True)
    pause = commands.add_parser("pause", help="Escalate: JSON {reason:string,questions:string[]}")
    pause.add_argument("--reason-file", type=Path, required=True)
    resolve = commands.add_parser("resolve", help="Resolve with exact status.human_events answer_text and event_id values")
    resolve.add_argument("--answer-file", type=Path, required=True)
    recover = commands.add_parser("recover", help="HUMAN ONLY: mark a stopped interrupted review as failed; consumes its attempt")
    recover.add_argument("--expected-review", type=int, required=True)
    recover.add_argument("--confirm-process-stopped", action="store_true", required=True)


def _doctor(engine: Engine, run_id: str | None) -> dict:
    """Use the frozen configuration of a selected run, without repairing anything."""
    if not run_id:
        return {**diagnose(engine.project, engine.resources), "run_id": None}
    run = engine._run(run_id)
    if (run / "transaction.json").exists():
        raise DevFlowError("doctor is read-only and cannot diagnose a pending run transaction; "
                           "preserve it and inspect the run before continuing.")
    state = engine.store.read(run_id)
    frozen = read_json(run / "config.json")
    if frozen != state["config"]:
        raise DevFlowError("Run configuration changed; never replace frozen configuration during a run.")
    return {**diagnose(engine.project, engine.resources, effective_config=frozen), "run_id": run_id}


def run(args, project: Path, run_id: str | None, session: str | None):
    engine = Engine(project, session=session)
    action = args.action
    if action == "doctor":
        if run_id and session is not None:
            engine.require_owner(run_id, session)
        return _doctor(engine, run_id)
    if not run_id:
        raise DevFlowError("No recorded plan run. Invoke /devflow:plan with a request and verify hooks are "
                           "enabled; do not fabricate the interview.")
    if session is not None:
        engine.require_owner(run_id, session)
    if action == "status":
        return engine.status(run_id)
    if action == "seal":
        return engine.seal(run_id, args.decisions_file, args.context_file)
    if action == "plan":
        return engine.plan(run_id, args.file)
    if action == "review":
        return engine.review(run_id)
    if action == "apply":
        return engine.apply(run_id, args.plan_file, args.decisions_file, args.expected_hash)
    if action == "pause":
        return engine.pause(run_id, args.reason_file)
    if action == "resolve":
        return engine.resolve(run_id, args.answer_file, session)
    if action == "recover":
        return engine.recover(run_id, args.expected_review)
    if action == "cancel":
        return engine.cancel(run_id)
    if action == "publish":
        return engine.publish(run_id)
    if action == "report":
        return engine.report(run_id)
    raise DevFlowError("Unknown plan action")
