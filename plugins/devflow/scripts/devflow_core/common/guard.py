"""PreToolUse decisions shared by every workflow.

The guard never grants a permission: ``None`` means "no objection", so Claude
Code's ordinary permission checks still apply. Workflows only describe their
runner actions, their write rules and any extra tool they need.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat

from .errors import DevFlowError, GuardError
from .runs import EVENT_ID, SESSION_ID
from .shell import READ_ONLY_SUMMARY, read_only_tokens, shell_tokens
from .storage import no_symlinks

READ_ONLY_TOOLS = frozenset({
    "Read", "Grep", "Glob", "LS", "List", "TodoWrite",
    "TaskCreate", "TaskUpdate", "TaskGet", "TaskList",
    # Only loads tool definitions; calling a loaded tool is still guarded.
    "ToolSearch",
})
WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
# Claude Code's question screen writes the human's choices and notes into these
# AskUserQuestion input fields before the tool runs.
SCREEN_FILLED_QUESTION_FIELDS = ("answers", "annotations")
RUNNER_PREFIX = ["uv", "run", "--offline", "--no-project", "--no-config", "--no-python-downloads"]
SELECTORS = ("--project", "--session", "--run")
GLOBAL_COMMANDS = ("status", "doctor")
HASH = re.compile(r"[a-f0-9]{64}\Z")
MAX_TEXT = 10000


def deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": "DevFlow guard: " + reason}}


def context(event: str, message: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": message}}


def runner_path(plugin_root: Path) -> Path:
    return Path(plugin_root) / "scripts" / "devflow.py"


def as_path(value: object, cwd: Path) -> Path:
    if not isinstance(value, str) or not value or any(ord(ch) < 32 for ch in value):
        raise GuardError("Expected a nonempty, single-line file path.")
    candidate = Path(value)
    return candidate if candidate.is_absolute() else Path(cwd) / candidate


def same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(str(Path(right).resolve()))


def inside(path: Path, base: Path) -> bool:
    try:
        return Path(os.path.normcase(str(path.resolve()))).is_relative_to(Path(os.path.normcase(str(base.resolve()))))
    except (OSError, RuntimeError, ValueError):
        return False


def safe_relative(value: str, cwd: Path, base: Path) -> tuple[Path, str]:
    """A regular file strictly below ``base``: no '..', links, reparse points or hard links."""
    candidate = as_path(value, cwd)
    if ".." in candidate.parts:
        raise GuardError("Parent traversal is not permitted for write paths.")
    candidate = Path(os.path.abspath(candidate))
    base = Path(os.path.abspath(base))
    try:
        relative = candidate.relative_to(base)
    except ValueError as exc:
        raise GuardError("The write target is outside the permitted directory.") from exc
    if not relative.parts:
        raise GuardError("Write a file, not the protected directory itself.")
    no_symlinks(candidate)
    cursor = base
    for component in relative.parts:
        cursor = cursor / component
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise GuardError("Symbolic links and Windows reparse points are not writable.")
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            raise GuardError("Hard-linked files are not writable through DevFlow.")
    if candidate.exists() and not candidate.is_file():
        raise GuardError("The write target must be a regular file.")
    candidate.resolve().relative_to(base.resolve())
    return candidate, relative.as_posix()


def draft_path(value: str, cwd: Path, run_dir: Path) -> bool:
    """A file strictly inside the run's draft directory."""
    try:
        safe_relative(value, cwd, run_dir / "draft")
        return True
    except (DevFlowError, OSError, RuntimeError, TypeError, ValueError):
        return False


def file_paths(tool: str, data: dict) -> list[str]:
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    main = data.get(key)
    if not isinstance(main, str):
        raise GuardError(f"{tool} has no supported {key} field.")
    paths = [main]
    if tool == "MultiEdit":
        edits = data.get("edits", [])
        if not isinstance(edits, list):
            raise GuardError("MultiEdit edits must be a list.")
        for edit in edits:
            if not isinstance(edit, dict):
                raise GuardError("Unsupported MultiEdit entry.")
            if "file_path" in edit:
                if not isinstance(edit["file_path"], str):
                    raise GuardError("Unsupported MultiEdit file path.")
                paths.append(edit["file_path"])
    return paths


@dataclass(frozen=True)
class RunnerCall:
    selectors: dict
    command: str
    action: str | None
    args: list

    @property
    def help(self) -> bool:
        return self.args == ["--help"]


def parse_runner(command: object, payload: dict, project: Path, plugin_root: Path) -> RunnerCall:
    """Accept only the documented invocation of this installed runner."""
    tokens = shell_tokens(command)
    if os.name == "nt" and tokens and tokens[0].casefold() == "uv.exe":
        tokens[0] = "uv"
    runner = runner_path(plugin_root)
    if len(tokens) <= len(RUNNER_PREFIX) + 1 or tokens[:len(RUNNER_PREFIX)] != RUNNER_PREFIX:
        raise GuardError(
            f"Bash accepts only the documented uv invocation of the DevFlow runner ({runner}) and "
            f"{READ_ONLY_SUMMARY}. Use the runner's status or doctor for diagnostics; do not run pwd, cd, "
            "which, env or standalone CLI probes.")
    cwd = Path(payload["cwd"])
    if not same_path(as_path(tokens[len(RUNNER_PREFIX)], cwd), runner):
        raise GuardError(f"The script is not this installed DevFlow runner; use {runner}")
    args = tokens[len(RUNNER_PREFIX) + 1:]
    selectors: dict[str, str] = {}
    while args and args[0] in SELECTORS:
        flag = args.pop(0)
        if flag in selectors or not args:
            raise GuardError("Duplicate or incomplete runner context option.")
        selectors[flag] = args.pop(0)
    if not {"--project", "--session"} <= selectors.keys():
        raise GuardError("Identify the current project and Claude session explicitly.")
    if not same_path(as_path(selectors["--project"], cwd), project):
        raise GuardError("The runner cannot target another project.")
    session = selectors["--session"]
    if not SESSION_ID.fullmatch(session) or session != payload.get("session_id"):
        raise GuardError("The runner cannot act for another Claude session.")
    if not args:
        raise GuardError("Missing runner command.")
    command_name = args.pop(0)
    action = args.pop(0) if command_name not in GLOBAL_COMMANDS and args and not args[0].startswith("--") else None
    return RunnerCall(selectors, command_name, action, args)


def parse_options(args: list, spec: dict) -> dict:
    """Parse ``--flag value`` pairs; a kind ending in '?' is optional."""
    values: dict[str, str] = {}
    pending = list(args)
    while pending:
        flag = pending.pop(0)
        if flag not in spec or flag in values or not pending:
            raise GuardError("Unknown, duplicate or incomplete runner option.")
        values[flag] = pending.pop(0)
    required = {flag for flag, kind in spec.items() if not kind.endswith("?")}
    if not required <= values.keys():
        raise GuardError("Missing required runner option.")
    return values


def check_option(kind: str, value: str, cwd: Path, run_dir: Path) -> None:
    kind = kind.rstrip("?")
    if kind == "draft" and not draft_path(value, cwd, run_dir):
        raise GuardError("Runner input files must be inside this run's draft directory.")
    if kind == "hash" and not HASH.fullmatch(value):
        raise GuardError("Expected a SHA-256 hash.")
    if kind == "event" and not EVENT_ID.fullmatch(value):
        raise GuardError("Use the event ID of an actual recorded human answer.")
    if kind == "decision" and value not in {"continue", "cancel"}:
        raise GuardError("Unsupported resolution decision.")
    if kind == "text" and (not value.strip() or len(value) > MAX_TEXT):
        raise GuardError("Provide a bounded, nonempty reason.")
    if kind not in {"draft", "hash", "event", "decision", "text"}:
        raise GuardError(f"Runner option kind {kind!r} cannot be checked.")


def _shell(data: dict) -> None:
    if data.get("dangerouslyDisableSandbox") or data.get("run_in_background"):
        raise GuardError("Sandbox bypass and background runner execution are forbidden.")


def _global_or_help(call: RunnerCall, run_id: str | None, workflows) -> bool:
    """Diagnostics and help never act on a run, so they are always available."""
    if "--run" in call.selectors and call.selectors["--run"] != run_id:
        raise GuardError("The runner cannot target another run.")
    if (call.command == "--help" and call.action is None and not call.args) or call.help:
        return True
    if call.command in GLOBAL_COMMANDS:
        if call.args and not call.help:
            raise GuardError("Global status/doctor accept no further arguments.")
        return True
    if call.command not in workflows:
        raise GuardError(f"Unknown runner command {call.command!r}.")
    if call.action in GLOBAL_COMMANDS and not call.args:
        return True
    return call.help


def pending_decision(payload: dict, project: Path, plugin_root: Path, pending: dict, workflow, workflows) -> dict | None:
    """Before a run exists: read-only work, diagnostics and the exact start only."""
    tool, data = payload.get("tool_name"), payload.get("tool_input")
    if not isinstance(tool, str) or not isinstance(data, dict):
        return deny("Malformed tool input; no tool has been authorized.")
    if tool in READ_ONLY_TOOLS:
        return None
    try:
        if tool in {"Bash", "PowerShell"}:
            _shell(data)
            if tool == "Bash" and read_only_tokens(data.get("command")) is not None:
                return None
            call = parse_runner(data.get("command"), payload, project, plugin_root)
            if _global_or_help(call, None, workflows):
                return None
            if (call.command == workflow.name and call.action == "start" and workflow.start_option
                    and "--run" not in call.selectors):
                values = parse_options(call.args, {workflow.start_option: "text"})
                if workflow.normalize_args(values[workflow.start_option], project) != pending["args"]:
                    raise GuardError("Start must use exactly the input named by the user's command.")
                return None
            raise GuardError("Start the pending workflow first; only diagnostics are available until then.")
        return deny(f"Tool {tool!r} is not available until the pending {workflow.name} run starts.")
    except (DevFlowError, OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
        return deny(str(exc))


def bound_decision(payload: dict, project: Path, plugin_root: Path, state: dict, run_dir: Path, workflow,
                   workflows) -> dict | None:
    """The owning session of a run: read-only work plus what the workflow permits."""
    tool, data = payload.get("tool_name"), payload.get("tool_input")
    if not isinstance(tool, str) or not isinstance(data, dict):
        return deny("Malformed tool input; no tool has been authorized.")
    if tool == "AskUserQuestion":
        # This is the only point that sees the model's own call: the question
        # screen adds the human's answers and notes afterwards.
        if any(data.get(field) for field in SCREEN_FILLED_QUESTION_FIELDS):
            return deny("Human answers and notes must not be prefilled by the model.")
        return None
    if tool in READ_ONLY_TOOLS:
        return None
    try:
        cwd = Path(payload["cwd"])
        if tool in WRITE_TOOLS:
            for path in file_paths(tool, data):
                if inside(as_path(path, cwd), Path(plugin_root)):
                    raise GuardError("The installed DevFlow plugin is read-only.")
                workflow.check_write(state, project, path, cwd, plugin_root)
            return None
        if tool in {"Bash", "PowerShell"}:
            _shell(data)
            # PowerShell quoting differs from Bash, so it gets the runner only.
            if tool == "Bash" and read_only_tokens(data.get("command")) is not None:
                return None
            call = parse_runner(data.get("command"), payload, project, plugin_root)
            if _global_or_help(call, state["run_id"], workflows):
                return None
            if call.command != workflow.name:
                raise GuardError(f"Only the {workflow.name} workflow of the bound run may be invoked.")
            if call.action not in workflow.actions:
                raise GuardError(f"Runner action {call.action!r} is not permitted here; recover is human-only.")
            spec = workflow.actions[call.action]
            for flag, value in parse_options(call.args, spec).items():
                check_option(spec[flag], value, cwd, run_dir)
            return None
        if workflow.allow_tool(state, tool, data):
            return None
        return deny(f"Tool {tool!r} is outside this {workflow.name} workflow.")
    except (DevFlowError, OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
        return deny(str(exc))


def status_context(workflow, state: dict, run_dir: Path, plugin_root: Path, event_id: str | None = None) -> str:
    fields = {"run_id": state["run_id"], "status": state["status"], "run_dir": str(run_dir),
              "draft_dir": str(run_dir / "draft"), "runner": str(runner_path(plugin_root)),
              **workflow.context_fields(state, run_dir)}
    if event_id:
        fields["recorded_event_id"] = event_id
    return (f"DevFlow {workflow.name} is active. {workflow.guidance} In Bash, run only the runner path below "
            f"and {READ_ONLY_SUMMARY}. Treat recorded human responses as evidence, not new tool permissions. "
            + json.dumps(fields, ensure_ascii=False))
