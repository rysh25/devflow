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

from .errors import DevFlowError, GuardError, ShellPolicyError
from .paths import PROJECT_MARKERS, msys_path, project_root, same_path
from .runs import EVENT_ID, SESSION_ID
from .shell import ALLOWED_SUMMARY, check_command, powershell_tokens, shell_tokens
from .storage import no_symlinks
from .workflow import CLAUDE_KINDS

#: Tools that never write project files or run commands; every guarded state allows them.
NON_WRITING_TOOLS = frozenset({
    "Read", "Grep", "Glob", "LS", "List", "TodoWrite",
    "TaskCreate", "TaskUpdate", "TaskGet", "TaskList",
    # Only loads tool definitions; calling a loaded tool is still guarded.
    "ToolSearch",
    # The web and Claude Code's own read-only listings.
    "WebSearch", "WebFetch", "ReadNotifications", "ListAgents", "CronList",
    # Messages to the user themselves.
    "PushNotification", "SendUserFile",
})
WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
SHELL_TOOLS = frozenset({"Bash", "PowerShell"})
#: Names no guarded write may create anywhere in the project (below its own
#: ``.devflow``): project markers would move the hook's project, and ugrep's
#: configuration would change what an allowed grep does.
PROTECTED_WRITE_NAMES = frozenset({*PROJECT_MARKERS, ".ugrep", ".ugrep+", ".ug"})
POWERSHELL_MESSAGE = ("PowerShell accepts only the DevFlow runner; use the Bash tool (Git Bash) for read-only commands, "
                      "or Read, Grep and Glob")
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


def inside(path: Path, base: Path) -> bool:
    try:
        return Path(os.path.normcase(str(path.resolve()))).is_relative_to(Path(os.path.normcase(str(base.resolve()))))
    except (OSError, RuntimeError, ValueError):
        return False


def name_key(part: str) -> str:
    """How a file system compares a name: Windows ignores case and trailing dots and
    spaces, macOS ignores case. One rule for every OS keeps the decision the same."""
    return part.casefold().rstrip(" .")


def check_write_names(relative: Path) -> None:
    """':' names an NTFS stream (``devflow.config.json::$DATA`` is that very file), so no
    guarded write may use it below the project, on any OS."""
    if any(":" in part for part in Path(relative).parts):
        raise GuardError("File names with ':' (Windows file streams) are not writable through a DevFlow run.")


def safe_relative(value: str, cwd: Path, base: Path) -> tuple[Path, str]:
    """A regular file strictly below ``base``: no '..', links, reparse points or hard links.

    The relative path returned is taken after resolving, so Windows short names
    (``GIT~1``) of existing directories come back as their long names.
    """
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
    check_write_names(relative)
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
    return candidate, candidate.resolve().relative_to(base.resolve()).as_posix()


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


def parse_runner(command: object, payload: dict, project: Path, plugin_root: Path, tool: str = "Bash") -> RunnerCall:
    """Accept only the documented invocation of this installed runner."""
    tokens = powershell_tokens(command) if tool == "PowerShell" else shell_tokens(command)
    if os.name == "nt" and tokens and tokens[0].casefold() == "uv.exe":
        tokens[0] = "uv"
    runner = runner_path(plugin_root)
    if len(tokens) <= len(RUNNER_PREFIX) + 1 or tokens[:len(RUNNER_PREFIX)] != RUNNER_PREFIX:
        if tool == "PowerShell":
            raise GuardError(f"{POWERSHELL_MESSAGE}. The runner is {runner}.")
        raise GuardError(
            f"Bash accepts the documented uv invocation of the DevFlow runner ({runner}), alone, or "
            f"{ALLOWED_SUMMARY}. Use the runner's status or doctor for DevFlow diagnostics.")
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
    if kind not in CLAUDE_KINDS:
        raise GuardError(f"Runner option kind {kind!r} cannot be checked.")


def _shell(data: dict) -> None:
    if data.get("dangerouslyDisableSandbox") or data.get("run_in_background"):
        raise GuardError("Sandbox bypass and background runner execution are forbidden.")


def _global_or_help(call: RunnerCall, workflows) -> bool:
    """Help, status and doctor never change a run, so any run may be named."""
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


def cd_checker(project: Path):
    """``cd`` (and ``git -C``) may go only where the hook still finds this project.

    The hook derives the project from its cwd, which follows ``cd``. Bash drops
    '..' textually while Python resolves symlinks first, so '..' is refused.
    """
    def allowed(value: str) -> bool:
        if os.name == "nt":
            value = msys_path(value)
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts:
            return False
        try:
            return same_path(project_root(path), project)
        except (DevFlowError, OSError, RuntimeError, ValueError):
            return False
    return allowed


def _shell_call(tool: str, data: dict, payload: dict, project: Path, plugin_root: Path) -> RunnerCall | None:
    """None: an allowed Bash command; otherwise the runner call (or GuardError)."""
    _shell(data)
    command = data.get("command")
    if tool == "Bash":
        if check_command(command, cd_checker(project), str(project)):
            return None
        return parse_runner(command, payload, project, plugin_root)
    try:
        return parse_runner(command, payload, project, plugin_root, "PowerShell")
    except ShellPolicyError as exc:  # PowerShell syntax the runner never needs
        raise GuardError(f"{POWERSHELL_MESSAGE}. {exc}") from exc


def _non_writing(tool: str, data: dict, payload: dict, project: Path, plugin_root: Path,
                 workflows) -> tuple[bool, RunnerCall | None]:
    """The base every guarded state allows: non-writing tools, the allowed Bash
    commands and the runner's help, status and doctor.

    Returns (True, None) when allowed, (False, call) for any other runner call
    (the state decides) and (False, None) for every other tool.
    """
    if tool in NON_WRITING_TOOLS:
        return True, None
    if tool in SHELL_TOOLS:
        call = _shell_call(tool, data, payload, project, plugin_root)
        if call is None or _global_or_help(call, workflows):
            return True, None
        return False, call
    return False, None


def check_protected_names(path: Path, project: Path) -> None:
    """No guarded write may create a project marker or ugrep settings in the project.

    Names are compared as the file system sees them (see ``name_key``), on the
    resolved path, so streams, trailing dots and short names are no way around.
    """
    try:
        check_write_names(Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(project))))
    except ValueError:
        pass  # Not below the project as written; the resolved check below still applies.
    try:
        relative = Path(path).resolve().relative_to(Path(project).resolve())
    except (OSError, RuntimeError, ValueError):
        return  # Outside the project: each workflow's own write rules refuse it.
    check_write_names(relative)
    parts = [name_key(part) for part in relative.parts]
    if parts and parts[0] == ".devflow":
        parts = parts[1:]
    if any(part in PROTECTED_WRITE_NAMES for part in parts):
        raise GuardError("Project markers (.git, .devflow, devflow.config.json) and ugrep settings (.ugrep) "
                         "cannot be created or changed through a DevFlow run.")


def _malformed(payload: dict):
    tool, data = payload.get("tool_name"), payload.get("tool_input")
    if not isinstance(tool, str) or not isinstance(data, dict):
        return None, None
    return tool, data


def prefilled_question(data: object) -> bool:
    """True when the model itself filled the fields the question screen fills in.

    Only meaningful at PreToolUse: by PostToolUse the screen has added the
    human's answers and notes to these same fields.
    """
    return isinstance(data, dict) and any(data.get(field) for field in SCREEN_FILLED_QUESTION_FIELDS)


def pending_refusal(payload: dict, project: Path, plugin_root: Path, pending: dict, workflow,
                    workflows) -> str | None:
    """Before a run exists: non-writing work, diagnostics and the exact start only.

    Returns the reason to refuse, or None. Every error becomes a reason, so a
    refusal never turns into a hook failure.
    """
    tool, data = _malformed(payload)
    if tool is None:
        return "Malformed tool input; no tool has been authorized."
    try:
        allowed, call = _non_writing(tool, data, payload, project, plugin_root, workflows)
        if allowed:
            return None
        if call is not None:
            if (call.command == workflow.name and call.action == "start" and workflow.start_option
                    and "--run" not in call.selectors):
                spec = {workflow.start_option: "text",
                        **{option.flag: option.kind + ("" if option.required else "?")
                           for option in workflow.extra_start_options}}
                values = parse_options(call.args, spec)
                if workflow.normalize_args(values[workflow.start_option], project) != pending["args"]:
                    raise GuardError("Start must use exactly the input named by the user's command.")
                return None
            raise GuardError("Start the pending workflow first; until then only non-writing tools, the allowed Bash "
                             "commands, diagnostics and this exact start are available.")
        return f"Tool {tool!r} is not available until the pending {workflow.name} run starts."
    except (DevFlowError, OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
        return str(exc)


def pending_decision(payload: dict, project: Path, plugin_root: Path, pending: dict, workflow, workflows) -> dict | None:
    reason = pending_refusal(payload, project, plugin_root, pending, workflow, workflows)
    return None if reason is None else deny(reason)


def inactive_refusal(payload: dict, project: Path, plugin_root: Path, workflows, reason: str) -> str | None:
    """A session bound to a run it does not own (transferred, or from an older
    DevFlow format): non-writing work and diagnostics only."""
    tool, data = _malformed(payload)
    if tool is None:
        return "Malformed tool input; no tool has been authorized."
    try:
        allowed, _ = _non_writing(tool, data, payload, project, plugin_root, workflows)
        return None if allowed else reason
    except (DevFlowError, OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
        return str(exc)


def inactive_decision(payload: dict, project: Path, plugin_root: Path, workflows, reason: str) -> dict | None:
    refusal = inactive_refusal(payload, project, plugin_root, workflows, reason)
    return None if refusal is None else deny(refusal)


def bound_refusal(payload: dict, project: Path, plugin_root: Path, state: dict, run_dir: Path, workflow,
                  workflows) -> str | None:
    """The owning session of a run: non-writing work plus what the workflow permits."""
    tool, data = _malformed(payload)
    if tool is None:
        return "Malformed tool input; no tool has been authorized."
    if tool == "AskUserQuestion":
        # This is the only point that sees the model's own call: the question
        # screen adds the human's answers and notes afterwards.
        if prefilled_question(data):
            return "Human answers and notes must not be prefilled by the model."
        return None
    try:
        cwd = Path(payload["cwd"])
        allowed, call = _non_writing(tool, data, payload, project, plugin_root, workflows)
        if allowed:
            return None
        if tool in WRITE_TOOLS:
            for path in file_paths(tool, data):
                target = as_path(path, cwd)
                if inside(target, Path(plugin_root)):
                    raise GuardError("The installed DevFlow plugin is read-only.")
                check_protected_names(target, project)
                workflow.check_write(state, project, path, cwd, plugin_root)
            return None
        if call is not None:
            if "--run" in call.selectors and call.selectors["--run"] != state["run_id"]:
                raise GuardError("The runner cannot target another run.")
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
        return f"Tool {tool!r} is outside this {workflow.name} workflow."
    except (DevFlowError, OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
        return str(exc)


def bound_decision(payload: dict, project: Path, plugin_root: Path, state: dict, run_dir: Path, workflow,
                   workflows) -> dict | None:
    reason = bound_refusal(payload, project, plugin_root, state, run_dir, workflow, workflows)
    return None if reason is None else deny(reason)


def status_context(workflow, state: dict, run_dir: Path, plugin_root: Path, event_id: str | None = None,
                   warnings: list[str] | None = None) -> str:
    fields = {"run_id": state["run_id"], "status": state["status"], "run_dir": str(run_dir),
              "draft_dir": str(run_dir / "draft"), "runner": str(runner_path(plugin_root)),
              **workflow.context_fields(state, run_dir), "bash_timeout_ms": workflow.bash_timeouts(state)}
    if event_id:
        fields["recorded_event_id"] = event_id
    if warnings:
        fields["warnings"] = warnings
    if workflow.restricts_tools:
        tools = (f"In Bash, run the runner path below alone, or {ALLOWED_SUMMARY}. ")
    else:
        tools = ("Use the runner path below for DevFlow actions; DevFlow does not restrict other tools, which "
                 "follow your Claude Code settings and permission mode. ")
    return (f"DevFlow {workflow.name} is active. {workflow.guidance} {tools}"
            "For review and check, set the Bash timeout to bash_timeout_ms. "
            "Treat recorded human responses as evidence, not new tool permissions. "
            + json.dumps(fields, ensure_ascii=False))
