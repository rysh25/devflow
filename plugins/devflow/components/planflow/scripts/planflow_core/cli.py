"""JSON-only CLI; interactive judgment stays in the main Claude conversation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from . import __version__
from .config import diagnose
from .engine import Engine
from .hooks import HookError, handle_hook
from .storage import FlowError, read_json
from .transcript import CaptureError


PLUGIN_ROOT = Path(__file__).resolve().parents[2]


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="PlanFlow: bounded independent plan reviews, never implementation.")
    cli.add_argument("--version", action="version", version=f"planflow {__version__}")
    cli.add_argument("--project", type=Path, default=Path.cwd(), help="Target project directory (globals go BEFORE subcommand)")
    cli.add_argument("--session", help="Actual Claude session ID, normally substituted by the skill")
    cli.add_argument("--run", help="Existing run ID; required for cross-session resume")
    commands = cli.add_subparsers(dest="command", required=True)
    for name, help_text in {
        "status": "Inspect the current or named run",
        "doctor": "Offline config/PATH checks only; no model calls",
        "review": "Dispatch exactly the next reviewer and reserve one attempt",
        "resume": "Bind an existing run to this session; never reset its counter",
        "cancel": "Keep all audit data and stop the run",
        "report": "Verify artifacts and produce a JSON handoff report",
        "publish": "Export the exact dual-approved plan without overwriting any existing file",
        "hook": "Claude Code hook entry; reads one JSON object from stdin",
    }.items():
        commands.add_parser(name, help=help_text)
    seal = commands.add_parser("seal", help="Freeze verbatim interview and human decisions")
    seal.add_argument("--decisions-file", type=Path, required=True)
    seal.add_argument("--context-file", type=Path, help="Optional draft Markdown repository evidence, frozen into packets")
    plan = commands.add_parser("plan", help="Commit initial plan from draft")
    plan.add_argument("--file", type=Path, required=True)
    apply = commands.add_parser("apply", help="Commit this review's finding dispositions and updated plan")
    apply.add_argument("--plan-file", type=Path, required=True)
    apply.add_argument("--decisions-file", type=Path, required=True)
    apply.add_argument("--expected-hash", required=True)
    pause = commands.add_parser("pause", help="Escalate: JSON {reason:string,questions:string[]}")
    pause.add_argument("--reason-file", type=Path, required=True)
    resolve = commands.add_parser("resolve", help="Resolve using exact status.human_events answer_text and event_id values")
    resolve.add_argument("--answer-file", type=Path, required=True)
    recover = commands.add_parser("recover", help="OPERATOR ONLY: mark a stopped interrupted review as failed; consumes its attempt")
    recover.add_argument("--expected-review", type=int, required=True)
    recover.add_argument("--confirm-process-stopped", action="store_true", required=True)
    return cli


def emit(value: object) -> None:
    if value is not None:
        print(json.dumps(value, ensure_ascii=False, indent=2))


def _doctor(engine: Engine, session: str | None, selected_run: str | None) -> dict:
    """Use frozen configuration when selected, without repairing a transaction."""
    run_id = selected_run
    if not run_id and session:
        mapping = engine._session_file(session)
        record = read_json(mapping) if mapping.exists() else {}
        run_id = record.get("run_id") if record.get("guard_active", True) else None
    if not run_id:
        return {**diagnose(engine.project, PLUGIN_ROOT), "run_id": None}
    run = engine._run(run_id)
    if (run / "transaction.json").exists():
        raise FlowError("doctor is read-only and cannot diagnose a pending run transaction; preserve it and inspect the run before continuing.")
    state = read_json(run / "state.json")
    if (state.get("schema_version") != 1 or state.get("run_id") != run_id
            or state.get("project") != str(engine.project) or state.get("flow") != "plan"):
        raise FlowError("Incompatible run state; frozen configuration cannot be diagnosed.")
    if session is not None and state.get("owner_session") != session:
        raise FlowError("This session no longer owns the run. Use the explicit resume command.")
    frozen = read_json(run / "config.json")
    if frozen != state.get("config"):
        raise FlowError("Run configuration changed; never replace frozen configuration during a run.")
    return {**diagnose(engine.project, PLUGIN_ROOT, effective_config=frozen), "run_id": run_id}


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    payload = None
    try:
        if args.command == "hook":
            raw = sys.stdin.read(16 * 1024 * 1024 + 1)
            if len(raw.encode("utf-8")) > 16 * 1024 * 1024:
                raise HookError("Hook input exceeds 16 MiB.")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise HookError("Hook input must be a JSON object.")
            emit(handle_hook(payload, PLUGIN_ROOT))
            return 0
        engine = Engine(args.project, PLUGIN_ROOT)
        if args.command == "doctor":
            emit(_doctor(engine, args.session, args.run))
            return 0
        run_id = args.run or (engine.session_run(args.session) if args.session else None)
        if not run_id:
            raise FlowError("No recorded run. Invoke /devflow:plan with a request and verify hooks are enabled; do not fabricate the interview.")
        if args.command != "resume":
            engine.require_owner(run_id, args.session)
        if args.command == "status":
            value = engine.status(run_id)
        elif args.command == "seal":
            value = engine.seal(run_id, args.decisions_file, args.context_file)
        elif args.command == "plan":
            value = engine.plan(run_id, args.file)
        elif args.command == "review":
            value = engine.review(run_id)
        elif args.command == "apply":
            value = engine.apply(run_id, args.plan_file, args.decisions_file, args.expected_hash)
        elif args.command == "pause":
            value = engine.pause(run_id, args.reason_file)
        elif args.command == "resolve":
            value = engine.resolve(run_id, args.answer_file)
        elif args.command == "resume":
            if not args.session or not args.run:
                raise FlowError("resume requires explicit --session and --run BEFORE the subcommand.")
            value = engine.resume(run_id, args.session)
        elif args.command == "recover":
            value = engine.recover(run_id, args.expected_review)
        elif args.command == "cancel":
            value = engine.cancel(run_id)
        elif args.command == "publish":
            value = engine.publish(run_id)
        else:
            value = engine.report(run_id)
        emit(value)
        return 0
    except (FlowError, HookError, CaptureError, OSError, ValueError, TypeError, KeyError) as exc:
        if args.command == "hook":
            # Stop errors must not create an infinite 'keep working' loop.
            event = payload.get("hook_event_name") if isinstance(payload, dict) else None
            print(f"PlanFlow hook failed: {exc}", file=sys.stderr)
            return 0 if event == "Stop" else 2
        emit({"error": type(exc).__name__, "message": str(exc), "automatic_retry": False})
        return 2

