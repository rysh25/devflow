"""The single DevFlow runner: one argparse tree built from the workflow registry.

Every non-hook command result, including an error, is one JSON object on
stdout whose top level always carries the reserved fields ``plugin`` and
``version`` (the CLI's values win over any same-named fields a command
returns). Every non-hook error, including a malformed invocation, exits
with status 2. ``--help`` and ``--version`` print text, and a ``None``
result prints nothing. Hook failures, including a malformed hook
invocation, go to stderr with exit status 1, which every supported host
shows or records without blocking the tool or
the prompt (exit status 2 blocks on Claude Code and Devin).
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil

import sys

from . import __version__, router
from .common import paths
from .common.hosts import HOSTS, executable_name, host_profile
from .common.arguments import Parser
from .common.hosts.diagnostics import MINIMUM_CLAUDE_CODE, claude_version, codex_version, devin_version
from .common.environment import python_errors, version_warnings
from .common.errors import DevFlowError
from .common.reviewers import readiness
from .common.settings import read_project_settings
from .workflows import WORKFLOWS

PLUGIN_ROOT = paths.PLUGIN_ROOT
MAX_HOOK_BYTES = 16 * 1024 * 1024


def parser() -> Parser:
    result = Parser(prog="devflow", description="DevFlow: planning and implementation workflows with bounded independent reviews.")
    result.add_argument("--version", action="version", version="devflow " + __version__)
    result.add_argument("--project", type=Path, default=Path.cwd(), help="Project/worktree; global options precede the command")
    result.add_argument("--session", help="Actual host session ID")
    result.add_argument("--host", choices=tuple(HOSTS), required=True,
                        help="The host whose session and records apply; always explicit")
    result.add_argument("--run", help="Existing run ID; required to inspect or act on a particular run")
    commands = result.add_subparsers(dest="command", required=True, parser_class=Parser)
    commands.add_parser("status", help="Overview, or the bound/selected run; changes nothing")
    commands.add_parser("doctor", help="Local prerequisites and reviewer CLI checks for every workflow; normally no model calls")
    commands.add_parser("hook", help="Host hook entry; reads one JSON object from stdin")
    for name, workflow in WORKFLOWS.items():
        actions = commands.add_parser(name, help=f"The {name} workflow").add_subparsers(
            dest="action", required=True, parser_class=Parser)
        workflow.register_commands(actions)
    return result


def emit(value) -> None:
    if value is not None:
        print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def emit_result(value: dict | None) -> None:
    """Every non-hook command result names the runner that produced it."""
    if value is not None:
        emit({**value, "plugin": "devflow", "version": __version__})




def host_version(host: str) -> dict:
    """The host's own offline version probe, chosen from one table."""
    return {"claude": claude_version, "codex": codex_version, "devin": devin_version}[host]()


COMMANDS = ("status", "doctor", "hook", *WORKFLOWS)
VALUE_OPTIONS = ("--project", "--session", "--host", "--run")  # The global options that take a value.


def command_name(arguments: list[str]) -> str | None:
    """The command word, read before parsing.

    Options and their values are passed over; the first positional token is the
    would-be command. It is returned only when it names a command, so a malformed
    hook invocation is still a hook while an invalid command word falls through
    to the versioned JSON error.
    """
    index = 0
    while index < len(arguments):
        token = arguments[index]
        # argparse also accepts a prefix of a long option; any value option it may name takes the next token.
        if token.startswith("--") and len(token) > 2 and any(option.startswith(token) for option in VALUE_OPTIONS):
            # An option-like next token is not a value: argparse reports the missing value, so
            # leave it for the scanner instead of skipping the command word it may contain.
            index += 1 if index + 1 < len(arguments) and arguments[index + 1].startswith("-") else 2
            continue
        if token.startswith("-") and len(token) > 1:
            index += 1
            continue
        return token if token in COMMANDS else None
    return None


def doctor(project: Path, host: str) -> dict:
    """Local project-settings readiness for every workflow, with each reviewer CLI checked in the
    project; changes nothing."""
    reports, cache, warnings = {}, {}, []
    documents = read_project_settings(project, host, WORKFLOWS)
    for name, workflow in WORKFLOWS.items():
        config = workflow.load_config(documents)
        # Settings-level diagnostics run before any reviewer CLI probe.
        extra = workflow.doctor(project, config, host=host)
        reviewers = []
        for provider in config["reviewers"]:
            key = json.dumps({k: v for k, v in provider.items() if k != "timeout_seconds"}, sort_keys=True)
            if key not in cache:
                # Every workflow checks the same directory, the project, so the key needs no cwd.
                cache[key] = {k: v for k, v in readiness(provider, project).items() if k != "id"}
            reviewers.append({"id": provider["id"], **cache[key]})
        warnings.extend(extra.get("warnings", []))
        ready = all(item["ready"] for item in reviewers) and extra.get("ready", True)
        reports[name] = {"config": config, "configuration_source": "project_settings", "reviewers": reviewers,
                         "reviewers_ready": all(item["ready"] for item in reviewers), "ready": ready,
                         **{key: value for key, value in extra.items() if key not in {"warnings", "ready"}}}
    warnings.extend(router.record_warnings(project, host))
    for item in cache.values():
        warnings.extend(item.get("details", {}).get("version_warnings", []))
    version = host_version(host)
    warnings.extend(version_warnings(host, version["version"]))
    executables = {name: shutil.which(name) for name in ("uv", executable_name(host), "git")}
    errors = python_errors()
    return {"project": str(project),
            "minimum_claude_code": MINIMUM_CLAUDE_CODE, "claude_code": version if host == "claude" else None, "host": host, "host_diagnostics": version,
            "python": {"version": sys.version.split()[0], "requires": ">=3.11", "fixed_minor": False,
                       "platform": sys.platform},
            "executables": executables, "workflows": reports, "warnings": warnings, "errors": errors,
            "ready": (bool(executables["uv"]) and version["supported"] and not errors
                      and all(item["ready"] for item in reports.values())),
            "reviewer_cwd": str(project),
            "readiness_scope": ("Local project-settings prerequisites and reviewer CLI checks in reviewer_cwd only; use a "
                                "workflow's doctor with the active session/run to inspect frozen run configuration."),
            "not_verified": ["CLI authentication and model access", "Effective model and reasoning settings",
                             "Reviewer CLI checks in any directory other than reviewer_cwd (an implementation worktree)",
                             f"Real {host_profile(host).label} hook and interview skill execution",
                             "Native Windows/macOS execution"],
            "note": ("Reviewer CLIs: --version and --help in the runner's directory, and each CLI's check of reviewer_cwd "
                     "(Devin: whether it trusts the directory); the directory check fails only when the CLI clearly "
                     "refuses, so ready is not proof of trust. Normally no model calls (docs/security.md); no authentication, "
                     "installation, or implicit fallback")}


def _with_flow_warnings(result, project: Path, flow: str, run_id: str | None, host: str):
    """The doctor of a Plan run started by the flow command also gives the flow's start warnings."""
    if flow != "plan" or not run_id or not isinstance(result, dict):
        return result
    return {**result, **router.flow_doctor(project, run_id, host=host)}


def _hook() -> dict | None:
    raw = sys.stdin.read(MAX_HOOK_BYTES + 1)
    if len(raw.encode("utf-8")) > MAX_HOOK_BYTES:
        raise DevFlowError("Hook payload exceeds 16 MiB")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise DevFlowError("Hook input must be a JSON object")
    return payload


def main(argv=None) -> int:
    arguments = sys.argv[1:] if argv is None else list(argv)
    # Known before parsing, so a malformed hook invocation (for example a host this
    # runner does not know) also ends with exit status 1 and never blocks the host.
    command = "hook" if command_name(arguments) == "hook" else None
    payload = None
    try:
        args = parser().parse_args(arguments)
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
            emit_result(router.status(project, args.session, args.run, host))
        elif command == "doctor":
            if args.run:
                flow, info = router.resolve_run(project, args.run, router.stores(project, host=host))
                if info.format != "current":
                    emit_result({"ready": False, **info.public()})
                else:
                    args.action = "doctor"
                    result = WORKFLOWS[flow].run_command(args, project, args.run, args.session, host)
                    emit_result(_with_flow_warnings(result, project, flow, args.run, host))
            else:
                emit_result(doctor(project, host))
        elif args.action == "start":
            if args.run:
                raise DevFlowError("A new start cannot target an existing run")
            options = WORKFLOWS[command].start_options(args)
            if getattr(args, "from_plan", None) is not None:
                emit_result(router.runner_start_from_plan(project, args.session, args.from_plan, options, host=host))
            else:
                emit_result(router.runner_start(project, args.session, command, args.start_value, options, host=host))
        else:
            args.pending_start = None
            if args.action == "doctor" and args.session:
                from .common.sessions import inspect_record, session_path
                path = session_path(project, args.session)
                info = inspect_record(path, WORKFLOWS) if path.exists() else None
                if info is not None and (info.format != "current" or info.host != host):
                    emit_result({"ready": False, "configuration_source": "session_record", "format": info.format,
                                 "session_host": info.host,
                                 "error": info.error or "Unsupported session record or host collision"})
                    return 0
                if info is not None:
                    args.pending_start = info.record["pending_start"]
            run_id = router.selected_run(project, args.session, command, args.run, host=host)
            if run_id is None:
                # Without a run, an action reads the current project settings.
                args.settings_documents = read_project_settings(project, host, WORKFLOWS)
            result = WORKFLOWS[command].run_command(args, project, run_id, args.session, host)
            if args.action == "doctor":
                result = _with_flow_warnings(result, project, command, run_id, host)
            emit_result(result)
        return 0
    except Exception as exc:  # noqa: BLE001 - every failure is reported, never retried
        if command == "hook":
            print("DevFlow hook failed: " + str(exc), file=sys.stderr)
            # Exit status 2 would make Claude Code or Devin block the tool or the prompt.
            return 1
        emit_result({"error": type(exc).__name__, "message": str(exc), "automatic_retry": False})
        return 2
