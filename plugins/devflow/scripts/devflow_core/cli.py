"""One public runner for both independent workflows; standard library only."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

from . import __version__, runtime


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
MINIMUM_CLAUDE_CODE = "2.1.295"
ACTIONS = {
    "plan": {"status", "doctor", "seal", "plan", "review", "apply", "pause", "resolve",
             "resume", "cancel", "report", "recover", "publish"},
    "implement": {"doctor", "status", "start", "resume", "checkpoint", "check", "review",
                  "reflect", "escalate", "resolve", "cancel", "recover"},
}


def parser():
    result = argparse.ArgumentParser(description="DevFlow: independent planning and implementation with bounded reviews.")
    result.add_argument("--version", action="version", version="devflow " + __version__)
    result.add_argument("--project", type=Path, default=Path.cwd(), help="Project/worktree; global options precede the workflow")
    result.add_argument("--session", help="Actual Claude Code session ID")
    result.add_argument("--run", help="Existing run ID; required to resume or inspect a particular run")
    result.add_argument("command", choices=["plan", "implement", "status", "doctor", "hook"])
    result.add_argument("arguments", nargs=argparse.REMAINDER, help="Workflow-specific action and its options")
    return result


def emit(value):
    if value is not None:
        print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def claude_version():
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
            report.update(version=".".join(match.groups()), supported=value >= (2, 1, 295),
                          reason="Required fail-closed hook behavior is version-gated; real hook execution is not verified")
    except (OSError, subprocess.SubprocessError) as exc:
        report["reason"] = str(exc)
    return report


def doctor(project: Path, plugin_root: Path = PLUGIN_ROOT):
    probe = runtime.component(plugin_root, "implement", "adapters").preflight
    reports = {}
    cache = {}
    for flow in runtime.FLOWS:
        root = runtime.component_root(plugin_root, flow)
        config = runtime.component(plugin_root, flow, "config").load_config(project, root)
        reviewers = []
        for provider in config["reviewers"]:
            key = json.dumps({k: v for k, v in provider.items() if k != "timeout_seconds"}, sort_keys=True)
            if key not in cache:
                try:
                    cache[key] = {"ready": True, "details": probe(provider)}
                except Exception as exc:
                    cache[key] = {"ready": False, "error": str(exc)}
            reviewers.append({"id": provider["id"], **cache[key]})
        reviewers_ready = all(item["ready"] for item in reviewers)
        reports[flow] = {"config": config, "configuration_source": "project_settings",
                         "reviewers": reviewers, "reviewers_ready": reviewers_ready,
                         "ready": reviewers_ready}
        if flow == "plan":
            inspection = runtime.component(plugin_root, "plan", "interview").inspect_interview_skill(project, config["grill_skill"])
            reports[flow]["interview_skill"] = inspection
            reports[flow]["ready"] = reviewers_ready and inspection["static_ready"]
    version = claude_version()
    executables = {name: shutil.which(name) for name in ("uv", "claude", "git")}
    warnings = []
    if not reports["implement"]["config"]["checks"]:
        warnings.append("No implementation checks configured; every revised snapshot requires actual human validation")
    inspection = reports["plan"]["interview_skill"]
    if not inspection["static_ready"]:
        warnings.append("Planning interview skill " + inspection["configured_name"] + ": " + inspection["reason"])
    warnings.append("Interview skill callability is not runtime-verified; static metadata cannot establish effective permissions or skillOverrides")
    return {"plugin": "devflow", "version": __version__, "project": str(project),
            "minimum_claude_code": MINIMUM_CLAUDE_CODE, "claude_code": version,
            "python": {"version": sys.version.split()[0], "requires": ">=3.11", "fixed_minor": False},
            "executables": executables,
            "workflows": reports, "warnings": warnings,
            "ready": bool(executables["uv"]) and version["supported"] and all(item["ready"] for item in reports.values()),
            "readiness_scope": "Offline project-settings prerequisites only; use plan doctor with the active session/run to inspect frozen run configuration.",
            "not_verified": ["CLI authentication and model access", "Effective model and reasoning settings",
                             "Real Claude Code hook and interview skill execution", "Native Windows/macOS execution"],
            "note": "Offline help/version probes only; no model calls, authentication, installation, or implicit fallback"}


def _forward(args, project: Path):
    forwarded = ["--project", str(project)]
    if args.session:
        forwarded += ["--session", args.session]
    if args.run:
        forwarded += ["--run", args.run]
    return forwarded + args.arguments


def _workflow(args, project: Path, plugin_root: Path):
    flow = args.command
    if not args.arguments:
        raise runtime.DevFlowError("Select an action after the workflow, such as plan status or implement start")
    action = args.arguments[0]
    if action in {"--help", "--version"} and len(args.arguments) == 1:
        return runtime.component(plugin_root, flow, "cli").main(args.arguments)
    if action not in ACTIONS[flow]:
        raise runtime.DevFlowError("Unknown or unsafe workflow action; component hook entry points are not exposed")
    if action in {"start", "resume"}:
        if not args.session or not runtime.SESSION_ID.fullmatch(args.session):
            raise runtime.DevFlowError("Start/resume requires the actual current Claude session ID")
        selected = None
        if action == "resume":
            selected_flow = runtime.resolve_run(project, args.run, plugin_root)
            if selected_flow != flow:
                raise runtime.DevFlowError("The selected run belongs to another workflow")
            selected = (flow, args.run)
        with runtime.dispatch_lock(project, plugin_root):
            runtime.assert_no_legacy_active(project, args.session, plugin_root)
            runtime.assert_available(project, args.session, plugin_root, selected)
            return runtime.component(plugin_root, flow, "cli").main(_forward(args, project))
    return runtime.component(plugin_root, flow, "cli").main(_forward(args, project))


def main(argv=None):
    args = parser().parse_args(argv)
    payload = None
    try:
        if args.command == "hook":
            if args.arguments or args.session or args.run:
                raise runtime.DevFlowError("Hook context comes only from the actual stdin hook payload")
            raw = sys.stdin.read(16 * 1024 * 1024 + 1)
            if len(raw.encode("utf-8")) > 16 * 1024 * 1024:
                raise runtime.DevFlowError("Hook payload exceeds 16 MiB")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise runtime.DevFlowError("Hook input must be a JSON object")
            emit(runtime.handle_hook(payload, PLUGIN_ROOT))
            return 0
        project = runtime.project_root(args.project)
        if args.command in runtime.FLOWS:
            return _workflow(args, project, PLUGIN_ROOT)
        if args.arguments:
            raise runtime.DevFlowError("Global status/doctor accept no action arguments; use global --run before the command")
        if args.command == "status":
            emit(runtime.status(project, args.session, args.run, PLUGIN_ROOT))
        else:
            emit(doctor(project))
        return 0
    except Exception as exc:
        if args.command == "hook":
            print("DevFlow hook failed: " + str(exc), file=sys.stderr)
            return 0 if isinstance(payload, dict) and payload.get("hook_event_name") == "Stop" else 2
        emit({"error": type(exc).__name__, "message": str(exc), "automatic_retry": False})
        return 2
