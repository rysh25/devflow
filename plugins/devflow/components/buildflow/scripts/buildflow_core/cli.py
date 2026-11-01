"""CLI for session-bound workflow actions; no arbitrary command overrides."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import shutil
import sys

from . import __version__
from . import adapters
from .config import load_config
from .engine import Engine
from .storage import BuildFlowError

PLUGIN = Path(__file__).resolve().parents[2]


def parser():
    p = argparse.ArgumentParser(description="Implement an explicitly selected plan with bounded independent reviews.")
    p.add_argument("--version", action="version", version="BuildFlow " + __version__)
    p.add_argument("--project", type=Path, default=Path.cwd(), help="Exact project directory (common flags precede the action)")
    p.add_argument("--session", help="Current Claude Code session ID")
    p.add_argument("--run", help="Existing BuildFlow run ID")
    commands = p.add_subparsers(dest="action", required=True)
    commands.add_parser("hook", help="Receive an authentic Claude Code hook payload on stdin")
    commands.add_parser("doctor", help="Offline executable/capability diagnostics; never calls a model")
    commands.add_parser("status")
    start = commands.add_parser("start")
    start.add_argument("--plan", type=Path, required=True)
    commands.add_parser("resume")
    checkpoint = commands.add_parser("checkpoint")
    checkpoint.add_argument("--summary-file", type=Path, required=True)
    commands.add_parser("check")
    commands.add_parser("review")
    reflection = commands.add_parser("reflect")
    reflection.add_argument("--decisions-file",type=Path,required=True)
    escalation = commands.add_parser("escalate")
    escalation.add_argument("--questions-file",type=Path,required=True)
    resolution = commands.add_parser("resolve")
    resolution.add_argument("--event-id",required=True)
    resolution.add_argument("--decision",choices=["continue","cancel"],required=True)
    resolution.add_argument("--reason",required=True)
    cancel = commands.add_parser("cancel")
    cancel.add_argument("--reason",required=True)
    recovery = commands.add_parser("recover",help="Human-only: first verify no associated process remains alive")
    recovery.add_argument("--confirm-process-stopped",action="store_true")
    return p


def doctor(project):
    config = load_config(project,PLUGIN)
    results = []
    for provider in config["reviewers"]:
        try:
            results.append({"id":provider["id"],"ready":True,"details":adapters.preflight(provider)})
        except BuildFlowError as exc:
            results.append({"id":provider["id"],"ready":False,"error":str(exc)})
    return {"schema_version":1,"plugin":"buildflow","version":__version__,"project":str(project),
        "config":config,"reviewers":results,"executables":{x:shutil.which(x) for x in ("uv","claude","git")},
        "ready":all(r["ready"] for r in results),
        "warnings":([] if config["checks"] else ["No checks configured; every revised snapshot will require explicit human validation."]),
        "not_verified":["CLI authentication and model access","Actual model used by provider","Real Claude Code hook execution","Native Windows/macOS behavior"],
        "note":"Offline help/version probes only. Does not install tools, authenticate, or incur model usage."}


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.action == "hook":
            from .hooks import handle_hook
            raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise BuildFlowError("Hook payload too large")
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload,dict):
                raise BuildFlowError("Hook input must be an object")
            output = handle_hook(payload,PLUGIN)
        elif args.action == "doctor":
            output = doctor(args.project.resolve())
        else:
            engine = Engine(args.project,PLUGIN)
            if args.action == "start":
                output = engine.start(args.session,args.plan)
            else:
                if not args.run:
                    raise BuildFlowError("--run RUN_ID is required before the action")
                if args.action == "status":
                    output = engine.status(args.run)
                elif args.action == "resume":
                    output = engine.resume(args.run,args.session)
                elif args.action == "checkpoint":
                    output = engine.checkpoint(args.run,args.session,args.summary_file)
                elif args.action == "check":
                    output = engine.check(args.run,args.session)
                elif args.action == "review":
                    output = engine.review(args.run,args.session)
                elif args.action == "reflect":
                    output = engine.reflect(args.run,args.session,args.decisions_file)
                elif args.action == "escalate":
                    output = engine.escalate(args.run,args.session,args.questions_file)
                elif args.action == "resolve":
                    output = engine.resolve(args.run,args.session,args.event_id,args.decision,args.reason)
                elif args.action == "cancel":
                    output = engine.cancel(args.run,args.session,args.reason)
                else:
                    output = engine.recover(args.run,args.confirm_process_stopped)
        if output is not None and (args.action != "hook" or output):
            print(json.dumps(output,ensure_ascii=False,indent=2,allow_nan=False))
        return 0
    except (BuildFlowError,OSError,ValueError,TypeError,KeyError) as exc:
        # Hook errors must stop the protected action, never silently weaken it.
        print("BuildFlow: " + str(exc),file=sys.stderr)
        return 2

