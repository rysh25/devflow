"""The single DevFlow runner: one argparse tree built from the workflow registry.

Every runner error, including a malformed invocation, is one JSON object on
stdout with exit status 2. ``--help`` and ``--version`` print text. Hook
failures go to stderr; a failed Stop hook exits 0 so it never loops.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

from . import __version__, router
from .common import paths
from .common.errors import DevFlowError, UsageError
from .common.reviewers import preflight
from .common.settings import validate_document
from .workflows import WORKFLOWS

PLUGIN_ROOT = paths.PLUGIN_ROOT
MINIMUM_CLAUDE_CODE = "2.1.295"
MAX_HOOK_BYTES = 16 * 1024 * 1024


class Parser(argparse.ArgumentParser):
    def error(self, message):  # argparse would print usage text and exit
        raise UsageError(f"{self.prog}: {message}")


def parser() -> Parser:
    result = Parser(prog="devflow", description="DevFlow: planning and implementation workflows with bounded independent reviews.")
    result.add_argument("--version", action="version", version="devflow " + __version__)
    result.add_argument("--project", type=Path, default=Path.cwd(), help="Project/worktree; global options precede the command")
    result.add_argument("--session", help="Actual Claude Code session ID")
    result.add_argument("--run", help="Existing run ID; required to inspect or act on a particular run")
    commands = result.add_subparsers(dest="command", required=True, parser_class=Parser)
    commands.add_parser("status", help="Read-only overview, or the bound/selected run")
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


def claude_version() -> dict:
    executable = shutil.which("claude")
    report = {"minimum_version": MINIMUM_CLAUDE_CODE, "path": executable,
              "version": None, "supported": False, "reason": "Claude Code executable not found"}
    if not executable:
        return report
    try:
        result = subprocess.run([executable, "--version"], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=10, shell=False, check=False)
        # A version probe is diagnostic only and does not authenticate or invoke a model.
        match = re.search(r"(?<![0-9])([0-9]+)\.([0-9]+)\.([0-9]+)(?![0-9])", result.stdout[:8192])
        if result.returncode != 0 or not match:
            report["reason"] = "Claude Code version could not be established"
        else:
            value = tuple(int(part) for part in match.groups())
            minimum = tuple(int(part) for part in MINIMUM_CLAUDE_CODE.split("."))
            report.update(version=".".join(match.groups()), supported=value >= minimum,
                          reason="Required fail-closed hook behavior is version-gated; real hook execution is not verified")
    except (OSError, subprocess.SubprocessError) as exc:
        report["reason"] = str(exc)
    return report


def doctor(project: Path) -> dict:
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
        extra = workflow.doctor(project, config)
        warnings.extend(extra.get("warnings", []))
        ready = all(item["ready"] for item in reviewers) and extra.get("ready", True)
        reports[name] = {"config": config, "configuration_source": "project_settings", "reviewers": reviewers,
                         "reviewers_ready": all(item["ready"] for item in reviewers), "ready": ready,
                         **{key: value for key, value in extra.items() if key not in {"warnings", "ready"}}}
    warnings.extend(router.blocking_warnings(project))
    version = claude_version()
    executables = {name: shutil.which(name) for name in ("uv", "claude", "git")}
    return {"plugin": "devflow", "version": __version__, "project": str(project),
            "minimum_claude_code": MINIMUM_CLAUDE_CODE, "claude_code": version,
            "python": {"version": sys.version.split()[0], "requires": ">=3.11", "fixed_minor": False},
            "executables": executables, "workflows": reports, "warnings": warnings,
            "ready": bool(executables["uv"]) and version["supported"] and all(item["ready"] for item in reports.values()),
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
        if command == "hook":
            if args.session or args.run:
                raise DevFlowError("Hook context comes only from the actual stdin hook payload")
            payload = _hook()
            emit(router.handle_hook(payload, PLUGIN_ROOT))
            return 0
        project = router.project_root(args.project)
        if command == "status":
            emit(router.status(project, args.session, args.run))
        elif command == "doctor":
            emit(doctor(project))
        elif args.action == "start":
            if args.run:
                raise DevFlowError("A new start cannot target an existing run")
            emit(router.runner_start(project, args.session, command, args.start_value))
        else:
            run_id = router.selected_run(project, args.session, command, args.run)
            if run_id is None:
                # Without a run, an action reads the current project settings.
                validate_document(project, WORKFLOWS)
            emit(WORKFLOWS[command].run_command(args, project, run_id, args.session))
        return 0
    except Exception as exc:  # noqa: BLE001 - every failure is reported, never retried
        if command == "hook":
            print("DevFlow hook failed: " + str(exc), file=sys.stderr)
            return 0 if isinstance(payload, dict) and payload.get("hook_event_name") == "Stop" else 2
        emit({"error": type(exc).__name__, "message": str(exc), "automatic_retry": False})
        return 2
