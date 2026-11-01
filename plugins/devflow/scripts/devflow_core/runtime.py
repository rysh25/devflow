"""Unified routing, not a dependency between the two workflow engines.

Hooks are session-scoped. Starts and resumes additionally enforce one unfinished
workflow per project. Ordinary hooks for unrelated sessions never create files.
This module never grants tool permissions or dispatches a model itself.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import shlex
import sys

from . import __version__


FLOWS = {"plan": "planflow", "implement": "buildflow"}
TERMINAL = frozenset({"completed", "exhausted", "cancelled"})
RUN_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[a-f0-9]{12}\Z")
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z")
RUNNER_PREFIX = ["uv", "run", "--offline", "--no-project", "--no-config", "--no-python-downloads"]
MAX_SCAN = 10000


class DevFlowError(ValueError):
    """A routing ambiguity or unsafe operation; never permission to continue."""


def component_root(plugin_root: Path, flow: str) -> Path:
    if flow not in FLOWS:
        raise DevFlowError("Unknown workflow; choose plan or implement")
    return plugin_root / "components" / FLOWS[flow]


def component(plugin_root: Path, flow: str, module: str):
    """Load only bundled component code, never a project's similarly named file."""
    scripts = component_root(plugin_root, flow) / "scripts"
    package = FLOWS[flow] + "_core"
    if package in sys.modules:
        origin = getattr(sys.modules[package], "__file__", None)
        if not origin or Path(origin).resolve() != (scripts / package / "__init__.py").resolve():
            raise DevFlowError("A conflicting workflow module is already loaded")
    source = str(scripts)
    if source not in sys.path:
        sys.path.insert(0, source)
    return importlib.import_module(package + "." + module)


def storage(plugin_root: Path):
    # Only the router imports both engines. The engines remain independent.
    return component(plugin_root, "plan", "storage")


def project_root(start: Path) -> Path:
    current = Path(start).resolve()
    if not current.is_dir():
        raise DevFlowError("Project directory does not exist")
    for directory in (current, *current.parents):
        if any((directory / marker).exists() for marker in (".devflow", "devflow.config.json", ".git")):
            return directory
    return current


def validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise DevFlowError("Use exactly the run ID shown by DevFlow")
    return run_id


def session_path(project: Path, flow: str, session_id: str) -> Path:
    if flow not in FLOWS or not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
        raise DevFlowError("A valid Claude session ID and workflow are required")
    return project / ".devflow" / "sessions" / flow / (hashlib.sha256(session_id.encode("utf-8")).hexdigest() + ".json")


def run_path(project: Path, flow: str, run_id: str) -> Path:
    if flow not in FLOWS:
        raise DevFlowError("Unknown workflow")
    return project / ".devflow" / "runs" / flow / validate_run_id(run_id)


def read_run(project: Path, flow: str, run_id: str, plugin_root: Path) -> dict:
    """Read routing metadata without recovering or mutating a transaction."""
    store = storage(plugin_root)
    directory = run_path(project, flow, run_id)
    store.no_symlinks(directory)
    state_path = directory / "state.json"
    transaction_path = directory / "transaction.json"
    if state_path.exists():
        state = store.read_json(state_path)
    elif transaction_path.exists():
        journal = store.read_json(transaction_path)
        state = journal.get("state")
    else:
        raise DevFlowError("Run has no committed state; preserve it for human recovery: " + run_id)
    if (not isinstance(state, dict) or state.get("run_id") != run_id
            or state.get("schema_version") != 1 or state.get("flow", flow) != flow
            or not isinstance(state.get("status"), str)
            or state.get("project") != str(project)):
        raise DevFlowError("Invalid or mismatched run routing metadata: " + run_id)
    return state


def resolve_run(project: Path, run_id: str, plugin_root: Path) -> str:
    validate_run_id(run_id)
    store = storage(plugin_root)
    candidates = []
    for flow in FLOWS:
        path = run_path(project, flow, run_id)
        store.no_symlinks(path)
        if path.exists():
            if not path.is_dir():
                raise DevFlowError("The selected run path is not a directory")
            read_run(project, flow, run_id, plugin_root)
            candidates.append(flow)
    if len(candidates) != 1:
        raise DevFlowError("Run ID is missing or ambiguous across workflows; do not guess a replacement")
    return candidates[0]


def bindings(project: Path, session_id: str, plugin_root: Path) -> list[dict]:
    store = storage(plugin_root)
    result = []
    for flow in FLOWS:
        path = session_path(project, flow, session_id)
        store.no_symlinks(path)
        if not path.exists():
            continue
        record = store.read_json(path, limit=8 * 1024 * 1024)
        if flow == "plan":
            if not record.get("guard_active", True):
                continue
            identifier = record.get("run_id")
            state = read_run(project, flow, identifier, plugin_root)
            owner = state.get("owner_session")
        else:
            component(plugin_root, flow, "hooks")._validate_session(record, session_id)
            identifier = record.get("active_run")
            if identifier is None:
                if record.get("intent"):
                    result.append({"flow": flow, "run_id": None, "state": None,
                                   "record": record, "path": path, "pending": True,
                                   "owner_matches": True})
                continue
            state = read_run(project, flow, identifier, plugin_root)
            owner = state.get("session_id")
        result.append({"flow": flow, "run_id": identifier, "state": state,
                       "record": record, "path": path, "pending": False,
                       "owner_matches": owner == session_id})
    return result


def active_binding(project: Path, session_id: str, plugin_root: Path) -> dict | None:
    current = bindings(project, session_id, plugin_root)
    if len(current) > 1:
        raise DevFlowError("This session is bound to multiple workflows; preserve records and resolve the conflict")
    return current[0] if current else None


def _release(binding: dict, plugin_root: Path) -> None:
    """Release only a terminal binding or not-yet-started intent on a new prompt."""
    store = storage(plugin_root)
    path = binding["path"]
    with store.file_lock(path.with_suffix(".lock")):
        record = store.read_json(path, limit=8 * 1024 * 1024)
        if binding["flow"] == "plan":
            if record.get("run_id") != binding["run_id"]:
                raise DevFlowError("Session binding changed during release")
            record["guard_active"] = False
        else:
            if record.get("active_run") != binding["run_id"]:
                raise DevFlowError("Session binding changed during release")
            record.update(active_run=None, intent=None)
        store.atomic_json(path, record)


def _scan_runs(project: Path, plugin_root: Path):
    store = storage(plugin_root)
    count = 0
    for flow in FLOWS:
        directory = project / ".devflow" / "runs" / flow
        store.no_symlinks(directory)
        if not directory.exists():
            continue
        if not directory.is_dir():
            raise DevFlowError("Workflow runs path is not a directory")
        for path in sorted(directory.iterdir()):
            count += 1
            if count > MAX_SCAN:
                raise DevFlowError("Run history exceeds the bounded scan limit; archive it explicitly")
            store.no_symlinks(path)
            if path.is_dir():
                yield flow, path.name, read_run(project, flow, path.name, plugin_root)


def assert_available(project: Path, session_id: str, plugin_root: Path,
                     selected: tuple[str, str] | None = None) -> None:
    """Require no other unfinished run or another session's pending start."""
    for flow, identifier, state in _scan_runs(project, plugin_root):
        if selected == (flow, identifier):
            continue
        if state["status"] not in TERMINAL:
            raise DevFlowError(f"Another unfinished {flow} run exists ({identifier}); resume or cancel it first")
    store = storage(plugin_root)
    directory = project / ".devflow" / "sessions" / "implement"
    store.no_symlinks(directory)
    if directory.exists():
        for index, path in enumerate(sorted(directory.glob("*.json"))):
            if index >= MAX_SCAN:
                raise DevFlowError("Session history exceeds the bounded scan limit")
            record = store.read_json(path, limit=8 * 1024 * 1024)
            owner = record.get("session_id")
            if not isinstance(owner, str) or path != session_path(project, "implement", owner):
                raise DevFlowError("Mismatched implementation session record")
            if not record.get("active_run") and record.get("intent") and owner != session_id:
                raise DevFlowError("Another session has a pending implementation request; resolve it in that session first")


def assert_no_legacy_active(project: Path, session_id: str, plugin_root: Path) -> None:
    store = storage(plugin_root)
    filename = hashlib.sha256(session_id.encode("utf-8")).hexdigest() + ".json"
    for legacy in ("planflow", "buildflow"):
        path = project / ("." + legacy) / "sessions" / filename
        store.no_symlinks(path)
        if not path.exists():
            continue
        record = store.read_json(path, limit=8 * 1024 * 1024)
        identifier = record.get("run_id") if legacy == "planflow" else record.get("active_run")
        if legacy == "planflow" and not record.get("guard_active", True):
            continue
        if not identifier:
            if legacy == "buildflow" and record.get("intent"):
                raise DevFlowError("A legacy BuildFlow request is pending; finish it with the old plugin first")
            continue
        validate_run_id(identifier)
        state = store.read_json(project / ("." + legacy) / "runs" / identifier / "state.json")
        if state.get("status") not in TERMINAL:
            raise DevFlowError("A legacy " + legacy + " run is active in this session; no automatic migration is performed")


@contextmanager
def dispatch_lock(project: Path, plugin_root: Path):
    store = storage(plugin_root)
    root = project / ".devflow"
    store.no_symlinks(root)
    root.mkdir(mode=0o700, exist_ok=True)
    ignore = root / ".gitignore"
    store.no_symlinks(ignore)
    if not ignore.exists():
        store.write_new(ignore, b"# Private DevFlow run data.\n*\n")
    with store.file_lock(root / "dispatch.lock"):
        yield


def public_command(prompt: str) -> tuple[str | None, str]:
    for name in ("plan", "implement", "resume"):
        command = "/devflow:" + name
        if prompt == command:
            return name, ""
        if prompt.startswith(command) and prompt[len(command):len(command)+1].isspace():
            return name, prompt[len(command):].strip()
    return None, ""


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": "DevFlow guard: " + reason}}


def _same_path(value: str, base: Path, expected: Path) -> bool:
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    return os.path.normcase(str(path.resolve())) == os.path.normcase(str(expected.resolve()))


def common_runner_allowed(payload: dict, project: Path, binding: dict, plugin_root: Path) -> bool:
    """Recognize only exact global status/doctor commands; everything else defers."""
    data = payload.get("tool_input")
    if payload.get("tool_name") not in {"Bash", "PowerShell"} or not isinstance(data, dict):
        return False
    command = data.get("command")
    if not isinstance(command, str) or not command or len(command) > 32768:
        return False
    if any(char in command for char in "$`;&|<>(){}[]!\r\n\x00") or any(ord(c) < 32 and c != "\t" for c in command):
        return False
    if data.get("dangerouslyDisableSandbox") or data.get("run_in_background"):
        return False
    try:
        tokens = shlex.split(command, posix=True, comments=False)
    except ValueError:
        return False
    if os.name == "nt" and tokens and tokens[0].casefold() == "uv.exe":
        tokens[0] = "uv"
    if len(tokens) <= len(RUNNER_PREFIX) + 1 or tokens[:len(RUNNER_PREFIX)] != RUNNER_PREFIX:
        return False
    cwd = Path(payload["cwd"])
    if not _same_path(tokens[len(RUNNER_PREFIX)], cwd, plugin_root / "scripts" / "devflow.py"):
        return False
    args = tokens[len(RUNNER_PREFIX) + 1:]
    selectors = {}
    while args and args[0] in {"--project", "--session", "--run"}:
        key = args.pop(0)
        if key in selectors or not args:
            return False
        selectors[key] = args.pop(0)
    if set(selectors) - {"--project", "--session", "--run"} or not {"--project", "--session"} <= selectors.keys():
        return False
    if not _same_path(selectors["--project"], cwd, project) or selectors["--session"] != payload["session_id"]:
        return False
    authorized_run = binding["run_id"]
    if authorized_run is None and binding.get("pending") and binding["flow"] == "implement":
        # /devflow:resume captures an intent before the implementation engine
        # binds the run. The public resume skill first inspects that exact run.
        # A pending new start must not grant access to an arbitrary run ID.
        intent = binding["record"].get("intent") or {}
        if intent.get("action") == "resume":
            try:
                selected = validate_run_id(intent.get("args"))
                if resolve_run(project, selected, plugin_root) != "implement":
                    return False
                authorized_run = selected
            except (ValueError, OSError):
                return False
    if "--run" in selectors and selectors["--run"] != authorized_run:
        return False
    return args in (["status"], ["doctor"])


def handle_hook(payload: dict, plugin_root: Path) -> dict | None:
    if not isinstance(payload, dict):
        raise DevFlowError("Hook input must be a JSON object")
    event = payload.get("hook_event_name")
    if event not in {"UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"}:
        return None
    cwd, session_id = payload.get("cwd"), payload.get("session_id")
    if not isinstance(cwd, str) or not cwd or not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
        raise DevFlowError("Hook requires project cwd and a valid Claude session ID")
    project = project_root(Path(cwd))
    if event == "UserPromptSubmit":
        prompt = payload.get("prompt")
        if not isinstance(prompt, str):
            raise DevFlowError("UserPromptSubmit requires the original prompt")
        command, argument = public_command(prompt)
        if command in {"plan", "implement"}:
            if not argument:
                raise DevFlowError("Provide a request after /devflow:plan or a file after /devflow:implement")
            if command == "implement":
                component(plugin_root, "implement", "hooks")._plan_arg(argument)
            elif component(plugin_root, "plan", "hooks")._has_stacked_command(argument):
                raise DevFlowError("Start one workflow, not stacked skills")
        if command == "resume":
            validate_run_id(argument)
        current = bindings(project, session_id, plugin_root)
        if not current and command is None:
            return None
        # Every release here is caused by this actual next prompt, never a
        # synthetic event inserted by a runner. The terminal turn stays guarded.
        with dispatch_lock(project, plugin_root):
            for binding in bindings(project, session_id, plugin_root):
                if binding["pending"] or binding["state"]["status"] in TERMINAL:
                    _release(binding, plugin_root)
            active = active_binding(project, session_id, plugin_root)
            flow = command if command in FLOWS else None
            selected = None
            if command == "resume":
                flow = resolve_run(project, argument, plugin_root)
                selected = (flow, argument)
            if flow:
                assert_no_legacy_active(project, session_id, plugin_root)
                assert_available(project, session_id, plugin_root, selected)
                if active and (active["flow"], active["run_id"]) != selected:
                    raise DevFlowError("Finish or cancel this session's active workflow before starting another")
                return component(plugin_root, flow, "hooks").handle_hook(payload, component_root(plugin_root, flow))
            if active:
                if not active["owner_matches"]:
                    raise DevFlowError("This run was resumed in another session; explicitly resume it to regain ownership")
                return component(plugin_root, active["flow"], "hooks").handle_hook(payload, component_root(plugin_root, active["flow"]))
            return None
    active = active_binding(project, session_id, plugin_root)
    if active is None:
        return None
    if not active["owner_matches"]:
        if event == "PreToolUse":
            return _deny("This session no longer owns its run; explicitly resume before using workflow tools")
        raise DevFlowError("Stale session cannot contribute human events to a transferred run")
    if event == "PreToolUse" and common_runner_allowed(payload, project, active, plugin_root):
        return None  # No permissionDecision: allow; ordinary permissions remain.
    return component(plugin_root, active["flow"], "hooks").handle_hook(payload, component_root(plugin_root, active["flow"]))


def status(project: Path, session_id: str | None, run_id: str | None, plugin_root: Path) -> dict:
    if run_id:
        flow = resolve_run(project, run_id, plugin_root)
        engine = component(plugin_root, flow, "engine").Engine(project, component_root(plugin_root, flow))
        return {"plugin": "devflow", "flow": flow, **engine.status(run_id)}
    active = active_binding(project, session_id, plugin_root) if session_id else None
    if active and active["run_id"]:
        return status(project, session_id, active["run_id"], plugin_root)
    runs = []
    for flow, identifier, state in _scan_runs(project, plugin_root):
        runs.append({"flow": flow, "run_id": identifier, "status": state["status"],
                     "review_count": state.get("review_count", len(state.get("attempts", []))),
                     "updated_at": state.get("updated_at"), "publication": state.get("publication")})
    return {"plugin": "devflow", "version": __version__, "project": str(project),
            "status": "pending_start" if active and active["pending"] else "idle",
            "pending_intent": active["record"].get("intent") if active and active["pending"] else None,
            "runs": runs}
