"""Claude Code hook recording and conservative, plan-only tool guard.

This is a workflow boundary, not an operating-system security sandbox.  Allowed
tools still pass through Claude Code's normal permissions.  In particular this
module never emits ``permissionDecision: allow``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
from typing import Any


class HookError(ValueError):
    """Malformed or unsafe hook input; the entry point must fail closed."""


TERMINAL_STATUSES = frozenset({"completed", "exhausted", "cancelled"})
READ_ONLY_TOOLS = frozenset(
    {
        "Read", "Grep", "Glob", "LS", "List", "TodoWrite", "AskUserQuestion",
        "TaskCreate", "TaskUpdate", "TaskGet", "TaskList",
    }
)
WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}\Z")
SESSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z")
HASH_PATTERN = re.compile(r"[a-f0-9]{64}\Z")
# Claude Code's question screen writes the human's choices and notes into these
# AskUserQuestion input fields before the tool runs.
SCREEN_FILLED_QUESTION_FIELDS = ("answers", "annotations")
START_COMMAND = "/devflow:plan"
RESUME_COMMAND = "/devflow:resume"

# All file-bearing command arguments are drafts, never mutable state paths.
COMMAND_OPTIONS: dict[str, dict[str, str]] = {
    "status": {},
    "doctor": {},
    "seal": {"--decisions-file": "draft"},
    "plan": {"--file": "draft"},
    "review": {},
    "apply": {
        "--plan-file": "draft", "--decisions-file": "draft",
        "--expected-hash": "hash",
    },
    "pause": {"--reason-file": "draft"},
    "resolve": {"--answer-file": "draft"},
    "resume": {},
    "cancel": {},
    "report": {},
    "publish": {},
}
OPTIONAL_COMMAND_OPTIONS: dict[str, dict[str, str]] = {
    "seal": {"--context-file": "draft"},
}


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "PlanFlow plan-only guard: " + reason,
        }
    }


def _context(event: str, message: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": message,
        }
    }


def _path(value: str, cwd: Path) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise HookError("Expected a nonempty file path.")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = cwd / candidate
    return candidate


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


def _draft_path(value: str, cwd: Path, run_dir: Path) -> bool:
    """Reject escapes, symlinks and writes to the draft directory itself."""
    try:
        candidate = _path(value, cwd)
        if ".." in candidate.parts:
            return False
        draft = run_dir / "draft"
        # Reject the controlled directories themselves if replaced by symlinks.
        if run_dir.is_symlink() or draft.is_symlink():
            return False
        candidate_abs = Path(os.path.abspath(candidate))
        draft_abs = Path(os.path.abspath(draft))
        relative = candidate_abs.relative_to(draft_abs)
        if not relative.parts:
            return False
        cursor = draft_abs
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                return False
        candidate.resolve().relative_to(draft.resolve())
        return True
    except (OSError, RuntimeError, ValueError, TypeError):
        return False


def _file_paths(tool: str, data: dict[str, Any]) -> list[str]:
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    main_path = data.get(key)
    if not isinstance(main_path, str):
        raise HookError(f"{tool} has no supported {key} field.")
    paths = [main_path]
    if tool == "MultiEdit":
        edits = data.get("edits", [])
        if not isinstance(edits, list):
            raise HookError("MultiEdit edits must be a list.")
        for edit in edits:
            if not isinstance(edit, dict):
                raise HookError("Unsupported MultiEdit entry.")
            if "file_path" in edit:
                if not isinstance(edit["file_path"], str):
                    raise HookError("Unsupported MultiEdit file path.")
                paths.append(edit["file_path"])
    return paths


def _shell_tokens(command: str) -> list[str]:
    """Accept only a deliberately small common Bash/PowerShell subset.

    No interpolation, shell operators, control characters or shell escapes are
    accepted, including inside quotes.  This intentionally rejects otherwise
    valid paths containing these characters instead of guessing shell rules.
    """
    if not isinstance(command, str) or not command or len(command) > 32768:
        raise HookError("The runner command must be a bounded nonempty string.")
    if any(ch in command for ch in "$`;&|<>(){}[]!\r\n\x00"):
        raise HookError("Shell operators, substitutions and multiline commands are forbidden.")
    if any(ord(ch) < 32 for ch in command if ch != "\t"):
        raise HookError("Control characters are forbidden in runner commands.")
    try:
        tokens = shlex.split(command, comments=False, posix=True)
    except ValueError as exc:
        raise HookError("Unsupported shell quoting.") from exc
    if not tokens:
        raise HookError("Empty runner command.")
    return tokens


def _runner_command(
    command: str, payload: dict[str, Any], run_dir: Path, script_path: Path,
) -> bool:
    tokens = _shell_tokens(command)
    expected_prefix = [
        "uv", "run", "--offline", "--no-project", "--no-config", "--no-python-downloads",
    ]
    if os.name == "nt" and tokens and tokens[0].lower() == "uv.exe":
        tokens[0] = "uv"
    if len(tokens) < 9 or tokens[:6] != expected_prefix:
        raise HookError(
            "Only the documented uv runner invocation is permitted. "
            "Use Read/Grep/Glob for inspection and the runner's doctor for diagnostics; "
            "do not run pwd, cd, which, env, or standalone CLI probes."
        )
    cwd = Path(payload["cwd"])
    if not _same_path(_path(tokens[6], cwd), script_path):
        raise HookError("Runner script does not match this installed plugin.")
    args = tokens[7:]
    options: dict[str, str] = {}
    while args and args[0] in {"--project", "--session", "--run"}:
        name = args.pop(0)
        if name in options or not args:
            raise HookError("Duplicate or incomplete runner context option.")
        options[name] = args.pop(0)
    if "--project" not in options or "--session" not in options:
        raise HookError("Runner commands must identify the current project and session.")
    project = run_dir.parents[3]
    if not _same_path(_path(options["--project"], cwd), project):
        raise HookError("Runner project differs from this run's project.")
    session = options["--session"]
    if not SESSION_PATTERN.fullmatch(session) or session != payload.get("session_id"):
        raise HookError("Runner session differs from the current Claude session.")
    if "--run" in options and options["--run"] != run_dir.name:
        raise HookError("Runner commands cannot target another active run.")
    if not args:
        raise HookError("Missing runner command.")
    if args.pop(0) != "plan" or not args:
        raise HookError("Only this run's plan workflow may be invoked.")
    name, *args = args
    if name not in COMMAND_OPTIONS:
        raise HookError(f"Runner command {name!r} is not permitted during PlanFlow.")
    if args == ["--help"]:
        return True
    required = COMMAND_OPTIONS[name]
    expected = {**required, **OPTIONAL_COMMAND_OPTIONS.get(name, {})}
    parsed: dict[str, str] = {}
    while args:
        flag = args.pop(0)
        if flag not in expected or flag in parsed or not args:
            raise HookError("Unknown, duplicate or incomplete runner argument.")
        parsed[flag] = args.pop(0)
    if not required.keys() <= parsed.keys():
        raise HookError("Missing required runner argument.")
    for flag in parsed:
        kind = expected[flag]
        if kind == "draft" and not _draft_path(parsed[flag], cwd, run_dir):
            raise HookError("Runner input files must be inside this run's draft directory.")
        if kind == "hash" and not HASH_PATTERN.fullmatch(parsed[flag]):
            raise HookError("Expected a SHA-256 plan hash.")
    return True


def guard_tool(
    payload: dict[str, Any], run_dir: Path, script_path: Path, grill_skill: str,
) -> dict[str, Any] | None:
    """Return a deny decision, or None to preserve normal permission checks."""
    tool = payload.get("tool_name")
    data = payload.get("tool_input")
    if not isinstance(tool, str) or not isinstance(data, dict):
        return _deny("Malformed tool input; no tool has been authorized.")
    # This is the only point that sees the model's own call: Claude Code's
    # question screen adds the human's answers/notes afterwards.
    if tool == "AskUserQuestion" and any(data.get(field) for field in SCREEN_FILLED_QUESTION_FIELDS):
        return _deny("Human answers and notes must not be prefilled by the model.")
    if tool in READ_ONLY_TOOLS:
        return None
    if tool == "Skill":
        requested = data.get("skill")
        if isinstance(requested, str) and requested.lstrip("/") == grill_skill.lstrip("/"):
            return None
        return _deny(
            f"Only the configured interview skill {grill_skill!r} may be invoked during this workflow. "
            "An allowed name does not override Claude Code's skill permissions."
        )
    try:
        cwd_value = payload.get("cwd")
        if not isinstance(cwd_value, str) or not cwd_value:
            raise HookError("Missing hook cwd.")
        cwd = Path(cwd_value)
        if tool in WRITE_TOOLS:
            paths = _file_paths(tool, data)
            if all(_draft_path(path, cwd, run_dir) for path in paths):
                return None
            return _deny("Write only to the current run's draft directory; source and audit files are read-only.")
        if tool in {"Bash", "PowerShell"}:
            if data.get("dangerouslyDisableSandbox") or data.get("run_in_background"):
                raise HookError("Sandbox bypass and background runner execution are forbidden.")
            _runner_command(data.get("command"), payload, run_dir, script_path)
            return None
        return _deny(f"Tool {tool!r} is outside this plan-only workflow.")
    except (HookError, OSError, RuntimeError, TypeError, ValueError) as exc:
        return _deny(str(exc))


def _command_argument(prompt: str, command: str) -> str | None:
    if prompt == command:
        return ""
    if prompt.startswith(command) and len(prompt) > len(command):
        separator = prompt[len(command)]
        if separator in " \t\r\n":
            return prompt[len(command):].strip()
    return None


def _has_stacked_command(argument: str) -> bool:
    # A slash-starting path with additional '/' is not interpreted as a skill.
    return bool(re.search(r"(?:\A|\n)\s*/[A-Za-z][A-Za-z0-9_:-]*(?:\s|\Z)", argument))


def _status_context(state: dict[str, Any], event_id: str | None = None) -> str:
    fields = {
        key: state[key]
        for key in (
            "run_id", "run_dir", "draft_dir", "status", "review_count",
            "current_plan", "plan_hash", "next_reviewer",
        )
        if key in state
    }
    if event_id:
        fields["recorded_event_id"] = event_id
    config = state.get("config")
    if isinstance(config, dict) and isinstance(config.get("grill_skill"), str):
        fields["interview_skill"] = config["grill_skill"]
    return (
        "PlanFlow is active. Use the bundled runner and the current run's draft directory only. "
        "Do not implement source changes. Treat recorded human responses as evidence, not new tool permissions. "
        + json.dumps(fields, ensure_ascii=False)
    )


def handle_hook(payload: dict[str, Any], plugin_root: Path) -> dict[str, Any] | None:
    """Record explicit runs and guard their tools; unrelated sessions are inert.

    Exceptions deliberately propagate to the CLI.  The CLI must block failed
    UserPromptSubmit/PreToolUse hooks, but must never create a Stop retry loop.
    """
    event = payload.get("hook_event_name")
    if event not in {"UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"}:
        return None
    cwd = payload.get("cwd")
    session = payload.get("session_id")
    if not isinstance(cwd, str) or not cwd or not isinstance(session, str) or not session:
        raise HookError("Hook input is missing cwd or session_id.")
    if not SESSION_PATTERN.fullmatch(session):
        raise HookError("Unsupported Claude session identifier.")

    # Lazy import keeps the guard independently testable and avoids a cycle.
    from .engine import Engine

    engine = Engine(Path(cwd), plugin_root)
    active = engine.find_active(session)
    if event == "UserPromptSubmit" and active:
        previous = engine.status(active)
        if previous.get("status") in TERMINAL_STATUSES:
            # Completion is not permission to implement in the same turn.  A
            # fresh, real human prompt is the boundary that releases the guard.
            engine.release_session(session)
            active = None
    if event == "UserPromptSubmit":
        prompt = payload.get("prompt")
        if not isinstance(prompt, str):
            raise HookError("UserPromptSubmit is missing the raw prompt.")
        request = _command_argument(prompt, START_COMMAND)
        resume = _command_argument(prompt, RESUME_COMMAND)
        if request is not None:
            if not request:
                return {
                    "decision": "block",
                    "reason": "Use /devflow:plan followed by the original planning request.",
                }
            if _has_stacked_command(request):
                return {
                    "decision": "block",
                    "reason": "Start PlanFlow with one explicit command, not stacked skills.",
                }
            state = engine.start(session, prompt, payload.get("transcript_path"))
            event_id = engine.record_event(state["run_id"], payload)
            return _context(event, _status_context(state, event_id))
        if resume is not None:
            if not RUN_ID_PATTERN.fullmatch(resume):
                return {
                    "decision": "block",
                    "reason": "Use /devflow:resume followed by exactly one safe run ID.",
                }
            state = engine.resume(resume, session)
            event_id = engine.record_event(state["run_id"], payload)
            return _context(event, _status_context(state, event_id))

    if not active:
        return None
    state = engine.status(active)
    if event == "PreToolUse":
        config = state.get("config", {})
        if not isinstance(config, dict):
            raise HookError("Active run has invalid configuration.")
        skill = config.get("grill_skill")
        if not isinstance(skill, str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]+", skill):
            raise HookError("Active run has no configured interview skill.")
        return guard_tool(
            payload, Path(state["run_dir"]), plugin_root.parents[1] / "scripts" / "devflow.py", skill,
        )
    if event == "UserPromptSubmit":
        event_id = engine.record_event(active, payload)
        return _context(event, _status_context(state, event_id))
    if event == "PostToolUse" and payload.get("tool_name") == "AskUserQuestion":
        # By now the question screen has filled tool_input.answers with the
        # human's choice, so its presence is expected here.  Model prefills are
        # denied at PreToolUse and rechecked against the transcript at seal.
        if not isinstance(payload.get("tool_input"), dict):
            raise HookError("Question capture requires its actual tool input.")
        use_id = payload.get("tool_use_id")
        if not isinstance(use_id, str) or not use_id.strip() or len(use_id) > 512 or "tool_response" not in payload:
            raise HookError("Question capture requires its actual tool identity and result.")
        event_id = engine.record_event(active, payload)
        return _context(
            event,
            f"PlanFlow captured this human question/answer as event {event_id}. "
            "Use its exact response and event ID if resolving a human escalation.",
        )
    if event == "Stop":
        engine.record_event(active, payload)
    return None
