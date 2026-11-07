"""The single DevFlow runner: one argparse tree built from the workflow registry.

Every runner error, including a malformed invocation, is one JSON object on
stdout with exit status 2. ``--help`` and ``--version`` print text. Hook
failures go to stderr with exit status 1, which Claude Code shows without
blocking the tool or the prompt.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil

import sys

from . import __version__, router
from .common import paths
from .common.hosts import executable_name
from .common.arguments import Parser
from .common.hosts.diagnostics import MINIMUM_CLAUDE_CODE, claude_version, codex_version
from .common.environment import python_errors, version_warnings
from .common.errors import DevFlowError
from .common.reviewers import preflight
from .common.settings import validate_document
from .workflows import WORKFLOWS

PLUGIN_ROOT = paths.PLUGIN_ROOT
MAX_HOOK_BYTES = 16 * 1024 * 1024


def parser() -> Parser:
    result = Parser(prog="devflow", description="DevFlow: planning and implementation workflows with bounded independent reviews.")
    result.add_argument("--version", action="version", version="devflow " + __version__)
    result.add_argument("--project", type=Path, default=Path.cwd(), help="Project/worktree; global options precede the command")
    result.add_argument("--session", help="Actual host session ID")
    result.add_argument("--host", choices=("claude", "codex"), required=True,
                        help="The host whose session and records apply; always explicit")
    result.add_argument("--run", help="Existing run ID; required to inspect or act on a particular run")
    commands = result.add_subparsers(dest="command", required=True, parser_class=Parser)
    commands.add_parser("status", help="Overview, or the bound/selected run; changes nothing")
    commands.add_parser("doctor", help="Offline prerequisites for every workflow; never calls a model")
    commands.add_parser("hook", help="Claude Code hook entry; reads one JSON object from stdin")
    for name, workflow in WORKFLOWS.items():
        actions = commands.add_parser(name, help=f"The {name} workflow").add_subparsers(
            dest="action", required=True, parser_class=Parser)
        workflow.register_commands(actions)
    return result


def emit(value) -> None:
    if value is not None:
        print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))




def doctor(project: Path, host: str) -> dict:
    """Offline project-settings readiness for every workflow; changes nothing."""
    reports, cache, warnings = {}, {}, []
    validate_document(project, WORKFLOWS)
    for name, workflow in WORKFLOWS.items():
        config = workflow.load_config(project)
        reviewers = []
        for provider in config["reviewers"]:
            key = json.dumps({k: v for k, v in provider.items() if k != "timeout_seconds"}, sort_keys=True)
            if key not in cache:
                try:
                    cache[key] = {"ready": True, "details": preflight(provider)}
                except Exception as exc:  # noqa: BLE001 - diagnostics report every failure
                    cache[key] = {"ready": False, "error": str(exc)}
            reviewers.append({"id": provider["id"], **cache[key]})
        extra = workflow.doctor(project, config, host=host)
        warnings.extend(extra.get("warnings", []))
        ready = all(item["ready"] for item in reviewers) and extra.get("ready", True)
        reports[name] = {"config": config, "configuration_source": "project_settings", "reviewers": reviewers,
                         "reviewers_ready": all(item["ready"] for item in reviewers), "ready": ready,
                         **{key: value for key, value in extra.items() if key not in {"warnings", "ready"}}}
    warnings.extend(router.blocking_warnings(project, host))
    for item in cache.values():
        warnings.extend(item.get("details", {}).get("version_warnings", []))
    version = claude_version() if host == "claude" else codex_version()
    warnings.extend(version_warnings(host, version["version"]))
    executables = {name: shutil.which(name) for name in ("uv", executable_name(host), "git")}
    errors = python_errors()
    return {"plugin": "devflow", "version": __version__, "project": str(project),
            "minimum_claude_code": MINIMUM_CLAUDE_CODE, "claude_code": version if host == "claude" else None, "host": host, "host_diagnostics": version,
            "python": {"version": sys.version.split()[0], "requires": ">=3.11", "fixed_minor": False,
                       "platform": sys.platform},
            "executables": executables, "workflows": reports, "warnings": warnings, "errors": errors,
            "ready": (bool(executables["uv"]) and version["supported"] and not errors
                      and all(item["ready"] for item in reports.values())),
            "readiness_scope": "Offline project-settings prerequisites only; use a workflow's doctor with the active session/run to inspect frozen run configuration.",
            "not_verified": ["CLI authentication and model access", "Effective model and reasoning settings",
                             "Real Claude Code hook and interview skill execution", "Native Windows/macOS execution"],
            "note": "Offline help/version probes only; no model calls, authentication, installation, or implicit fallback"}


def _hook() -> dict | None:
    raw = sys.stdin.read(MAX_HOOK_BYTES + 1)
    if len(raw.encode("utf-8")) > MAX_HOOK_BYTES:
        raise DevFlowError("Hook payload exceeds 16 MiB")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise DevFlowError("Hook input must be a JSON object")
    return payload


def main(argv=None) -> int:
    command, payload = None, None
    try:
        args = parser().parse_args(argv)
        command = args.command
        host = args.host
        if command == "hook":
            if args.session or args.run:
                raise DevFlowError("Hook context comes only from the actual stdin hook payload")
            payload = _hook()
            emit(router.handle_hook(payload, PLUGIN_ROOT, host=host))
            return 0
        project = router.project_root(args.project)
        if command == "status":
            emit(router.status(project, args.session, args.run, host))
        elif command == "doctor":
            if args.run:
                flow, info = router.resolve_run(project, args.run, router.stores(project, host=host))
                if info.format != "current":
                    emit({"ready": False, **info.public()})
                else:
                    args.action = "doctor"
                    emit(WORKFLOWS[flow].run_command(args, project, args.run, args.session, host))
            else:
                emit(doctor(project, host))
        elif args.action == "start":
            if args.run:
                raise DevFlowError("A new start cannot target an existing run")
            options = WORKFLOWS[command].start_options(args)
            emit(router.runner_start(project, args.session, command, args.start_value, options, host=host))
        else:
            args.pending_start = None
            if args.action == "doctor" and args.session:
                from .common.sessions import inspect_record, session_path
                path = session_path(project, args.session)
                info = inspect_record(path, WORKFLOWS) if path.exists() else None
                if info is not None and (info.format != "current" or info.host != host):
                    emit({"ready": False, "configuration_source": "session_record", "format": info.format,
                          "session_host": info.host, "error": info.error or "Unsupported session record or host collision"})
                    return 0
                if info is not None:
                    args.pending_start = info.record["pending_start"]
            run_id = router.selected_run(project, args.session, command, args.run, host=host)
            if run_id is None:
                # Without a run, an action reads the current project settings.
                validate_document(project, WORKFLOWS)
            emit(WORKFLOWS[command].run_command(args, project, run_id, args.session, host))
        return 0
    except Exception as exc:  # noqa: BLE001 - every failure is reported, never retried
        if command == "hook":
            print("DevFlow hook failed: " + str(exc), file=sys.stderr)
            # Exit status 2 would make Claude Code block the tool or the prompt.
            return 1
        emit({"error": type(exc).__name__, "message": str(exc), "automatic_retry": False})
        return 2
