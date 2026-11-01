"""Session-scoped Claude Code workflow guard and authentic event capture.

This is defense in depth, not an operating-system sandbox. In particular, an
approved project check executes project code, and other trusted hooks can alter
tool inputs. This module never grants permissions or emits an ``allow`` verdict.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
from typing import Any
from uuid import uuid4

from .storage import (
    BuildFlowError, atomic_write_json, file_lock, no_symlinks, now,
    project_root, read_json, session_path, valid_run_id, write_new,
)


class HookError(BuildFlowError):
    """Invalid active-workflow input; the hook entry point must fail closed."""


TERMINAL_STATUSES = frozenset({"completed", "exhausted", "cancelled"})
EDIT_STATUSES = frozenset({"implementing", "needs_revision"})
READ_TOOLS = frozenset({
    "Read", "Grep", "Glob", "LS", "List", "TodoWrite",
    "TaskCreate", "TaskUpdate", "TaskGet", "TaskList",
})
WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
PROTECTED_COMPONENTS = frozenset({
    ".git", ".devflow", ".buildflow", ".planflow", ".claude", ".codex", ".opencode", ".agents",
})
PROTECTED_NAMES = frozenset({
    "devflow.config.json", "buildflow.config.json", "planflow.config.json", "claude.md", "agents.md", ".mcp.json",
    "opencode.json", "opencode.jsonc", ".gitignore", ".gitattributes",
})
# Claude Code's question screen writes the human's choices and notes into these
# AskUserQuestion input fields before the tool runs.
SCREEN_FILLED_QUESTION_FIELDS = ("answers", "annotations")
START_COMMAND = "/devflow:implement"
RESUME_COMMAND = "/devflow:resume"
EVENT_ID = re.compile(r"evt-[a-f0-9]{32}\Z")
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,511}\Z")
MAX_EVENT_BYTES = 1024 * 1024
MAX_SESSION_BYTES = 8 * 1024 * 1024
MAX_EVENTS = 2000

# File-bearing arguments are drafts, never arbitrary scripts or audit paths.
COMMAND_OPTIONS = {
    "doctor": {}, "status": {}, "resume": {}, "check": {}, "review": {},
    "checkpoint": {"--summary-file": "draft"},
    "reflect": {"--decisions-file": "draft"},
    "escalate": {"--questions-file": "draft"},
    "resolve": {"--event-id": "event", "--decision": "decision", "--reason": "text"},
    "cancel": {"--reason": "text"},
}


def _deny(reason: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": "BuildFlow guard: " + reason,
    }}


def _context(event: str, message: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": message}}


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


def _path(value: str, cwd: Path) -> Path:
    if not isinstance(value, str) or not value or any(ord(ch) < 32 for ch in value):
        raise HookError("Expected a nonempty, single-line file path.")
    result = Path(value)
    return result if result.is_absolute() else cwd / result


def _safe_relative(value: str, cwd: Path, base: Path) -> tuple[Path, str]:
    candidate = _path(value, cwd)
    if ".." in candidate.parts:
        raise HookError("Parent traversal is not permitted for write paths.")
    candidate = Path(os.path.abspath(candidate))
    base = Path(os.path.abspath(base))
    try:
        relative = candidate.relative_to(base)
    except ValueError as exc:
        raise HookError("The write target is outside the permitted directory.") from exc
    if not relative.parts:
        raise HookError("Write a file, not the protected directory itself.")
    no_symlinks(candidate)
    cursor = base
    for component in relative.parts:
        cursor = cursor / component
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise HookError("Symbolic links and Windows reparse points are not writable.")
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            raise HookError("Hard-linked files are not writable through BuildFlow.")
    if candidate.exists() and not candidate.is_file():
        raise HookError("The write target must be a regular file.")
    candidate.resolve().relative_to(base.resolve())
    return candidate, relative.as_posix()


def _draft_path(value: str, cwd: Path, run_dir: Path) -> bool:
    try:
        _safe_relative(value, cwd, run_dir / "draft")
        return True
    except (BuildFlowError, OSError, RuntimeError, TypeError, ValueError):
        return False


def _file_paths(tool: str, data: dict) -> list[str]:
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    target = data.get(key)
    if not isinstance(target, str):
        raise HookError(f"{tool} has no supported {key} field.")
    result = [target]
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
                result.append(edit["file_path"])
    return result


def _source_path(value: str, cwd: Path, project: Path, state: dict, plugin_root: Path) -> None:
    candidate, relative = _safe_relative(value, cwd, project)
    parts = [part.casefold() for part in Path(relative).parts]
    if any(part in PROTECTED_COMPONENTS for part in parts) or parts[-1] in PROTECTED_NAMES:
        raise HookError("Workflow, Git, agent-instruction and permission configuration files are protected.")
    installed_root = plugin_root.parents[1] if plugin_root.parent.name == "components" else plugin_root
    if _same_path(candidate, installed_root) or candidate.resolve().is_relative_to(installed_root.resolve()):
        raise HookError("The installed DevFlow plugin is read-only.")
    for field in ("plan_source", "config_path"):
        protected = state.get(field)
        if isinstance(protected, str) and protected and _same_path(candidate, _path(protected, project)):
            raise HookError("The original plan and frozen configuration are read-only.")
    config = state.get("config")
    if not isinstance(config, dict):
        raise HookError("Active run configuration is missing.")
    from .snapshot import path_in_scope
    if not path_in_scope(relative, config):
        raise HookError("The target is outside the frozen snapshot include/exclude scope.")
    if config.get("snapshot", {}).get("mode", "git") == "git":
        env = dict(os.environ)
        env["GIT_OPTIONAL_LOCKS"] = "0"
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", str(project), "check-ignore", "-q", "--", relative],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            shell=False, timeout=10, check=False, env=env,
        )
        if result.returncode == 0:
            raise HookError("Git-ignored files are not in the reviewed source scope.")
        if result.returncode != 1:
            raise HookError("Cannot establish whether this write is in the Git snapshot scope.")


def _shell_tokens(command: str) -> list[str]:
    if not isinstance(command, str) or not command or len(command) > 32768:
        raise HookError("Expected a bounded runner command.")
    if any(ch in command for ch in "$`;&|<>(){}[]!\r\n\x00"):
        raise HookError("Shell operators, substitutions and multiline commands are forbidden.")
    if any(ord(ch) < 32 and ch != "\t" for ch in command):
        raise HookError("Control characters are forbidden in runner commands.")
    try:
        tokens = shlex.split(command, comments=False, posix=True)
    except ValueError as exc:
        raise HookError("Unsupported shell quoting.") from exc
    if not tokens:
        raise HookError("Empty runner command.")
    return tokens


def _parse_options(args: list[str], expected: dict[str, str]) -> dict[str, str]:
    values = {}
    while args:
        flag = args.pop(0)
        if flag not in expected or flag in values or not args:
            raise HookError("Unknown, duplicate or incomplete runner option.")
        values[flag] = args.pop(0)
    if values.keys() != expected.keys():
        raise HookError("Missing required runner option.")
    return values


def _runner_command(command: str, payload: dict, project: Path, session: dict, state: dict | None, plugin_root: Path) -> None:
    tokens = _shell_tokens(command)
    prefix = ["uv", "run", "--offline", "--no-project", "--no-config", "--no-python-downloads"]
    if os.name == "nt" and tokens[0].casefold() == "uv.exe":
        tokens[0] = "uv"
    if len(tokens) < 9 or tokens[:len(prefix)] != prefix:
        raise HookError("Use the documented uv invocation of the DevFlow runner.")
    cwd = Path(payload["cwd"])
    if not _same_path(_path(tokens[len(prefix)], cwd), plugin_root.parents[1] / "scripts" / "devflow.py"):
        raise HookError("The script is not this installed DevFlow runner.")
    args = tokens[len(prefix) + 1:]
    common = {}
    while args and args[0] in {"--project", "--session", "--run"}:
        flag = args.pop(0)
        if flag in common or not args:
            raise HookError("Duplicate or incomplete runner context option.")
        common[flag] = args.pop(0)
    if not {"--project", "--session"} <= common.keys():
        raise HookError("Identify the project and current Claude session explicitly.")
    if not _same_path(_path(common["--project"], cwd), project):
        raise HookError("The runner cannot target another project.")
    if common["--session"] != payload["session_id"]:
        raise HookError("The runner cannot impersonate another session.")
    if not args or args.pop(0) != "implement":
        raise HookError("Use the implement workflow of the DevFlow runner.")
    if not args:
        raise HookError("Missing implementation subcommand.")
    name, *args = args
    intent = session.get("intent") or {}
    if state is None:
        if name == "start" and intent.get("action") == "implement":
            if "--run" in common:
                raise HookError("A new start cannot target an existing run.")
            if args == ["--help"]:
                return
            values = _parse_options(args, {"--plan": "plan"})
            if not _same_path(_path(values["--plan"], cwd), _path(intent["args"], project)):
                raise HookError("Start must use the plan named by the actual user command.")
            return
        if name == "resume" and intent.get("action") == "resume":
            if common.get("--run") != intent.get("args") or args:
                raise HookError("Resume must target the user-selected run without extra options.")
            return
        if name in {"doctor", "status"} and not args and "--run" not in common:
            return
        raise HookError("Capture the baseline with start, or bind the requested run with resume, before other actions.")
    run_id = state["run_id"]
    if "--run" in common and common["--run"] != run_id:
        raise HookError("The runner cannot target another active run.")
    if name not in COMMAND_OPTIONS:
        raise HookError(f"Runner command {name!r} is not allowed; recover is human-only.")
    if name == "resume" and (intent.get("action") != "resume" or intent.get("args") != run_id):
        raise HookError("Resume requires the explicit user resume command.")
    if args == ["--help"]:
        return
    expected = COMMAND_OPTIONS[name]
    values = _parse_options(args, expected)
    run_dir = project / ".devflow" / "runs" / "implement" / run_id
    for flag, value in values.items():
        kind = expected[flag]
        if kind == "draft" and not _draft_path(value, cwd, run_dir):
            raise HookError("Runner input files must be in the current run's draft directory.")
        if kind == "event" and not EVENT_ID.fullmatch(value):
            raise HookError("Use the event ID captured from an actual user response.")
        if kind == "decision" and value not in {"continue", "cancel"}:
            raise HookError("Unsupported resolution decision.")
        if kind == "text" and (not value.strip() or len(value) > 10000):
            raise HookError("Provide a bounded, nonempty reason.")


def guard_tool(payload: dict, project: Path, session: dict, state: dict | None, plugin_root: Path) -> dict:
    """Deny unsafe tools, otherwise leave the normal permission checks intact."""
    tool, data = payload.get("tool_name"), payload.get("tool_input")
    if not isinstance(tool, str) or not isinstance(data, dict):
        return _deny("Malformed tool input.")
    if tool in READ_TOOLS:
        return {}
    if tool == "AskUserQuestion":
        # This is the only point that sees the model's own call: the question
        # screen adds the human's answers/notes afterwards.
        if any(data.get(field) for field in SCREEN_FILLED_QUESTION_FIELDS):
            return _deny("Human answers and notes must not be prefilled by the model.")
        return {}
    try:
        cwd = Path(payload["cwd"])
        if tool in WRITE_TOOLS:
            paths = _file_paths(tool, data)
            if state is None:
                raise HookError("No source or draft writes before the baseline is captured.")
            run_dir = project / ".devflow" / "runs" / "implement" / state["run_id"]
            for path in paths:
                if _draft_path(path, cwd, run_dir):
                    continue
                if state.get("status") not in EDIT_STATUSES:
                    raise HookError("Source edits are permitted only during implementing or needs_revision.")
                _source_path(path, cwd, project, state, plugin_root)
            return {}
        if tool in {"Bash", "PowerShell"}:
            if data.get("dangerouslyDisableSandbox") or data.get("run_in_background"):
                raise HookError("Sandbox bypass and detached runner execution are forbidden.")
            _runner_command(data.get("command"), payload, project, session, state, plugin_root)
            return {}
        # Nested agents, MCP tools, dynamic tool discovery, arbitrary skills,
        # web tools and undocumented tools cannot escape the workflow boundary.
        return _deny(f"Tool {tool!r} is outside this bounded implementation workflow.")
    except (BuildFlowError, OSError, RuntimeError, TypeError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        return _deny(str(exc))


def _command_arg(prompt: str, command: str) -> str | None:
    if prompt == command:
        return ""
    if prompt.startswith(command) and len(prompt) > len(command) and prompt[len(command)].isspace():
        return prompt[len(command):].strip()
    return None


def _plan_arg(argument: str) -> str:
    if not argument or any(ord(ch) < 32 for ch in argument):
        raise HookError("Use /devflow:implement followed by one plan-file path.")
    if argument[0] in {"\"", "'"}:
        if len(argument) < 3 or argument[-1] != argument[0]:
            raise HookError("The plan path has unmatched quotes.")
        argument = argument[1:-1]
    argument = argument.removeprefix("@")
    if not argument or argument.startswith("--"):
        raise HookError("Supply a plan file, not runner options or stacked skills.")
    return argument


def _validate_session(data: dict, session_id: str) -> None:
    if data.get("session_id") != session_id:
        raise HookError("Stored session identity does not match the active Claude session.")
    for field in ("seq", "prompt_seq"):
        if type(data.get(field)) is not int or data[field] < 0:
            raise HookError("Stored session counters are invalid.")
    if not isinstance(data.get("events"), list) or len(data["events"]) > MAX_EVENTS:
        raise HookError("Stored session event list is invalid or too large.")
    intent = data.get("intent")
    if intent is not None and (not isinstance(intent, dict) or intent.get("action") not in {"implement", "resume"}):
        raise HookError("Stored workflow intent is invalid.")


def _read_state(project: Path, session: dict) -> dict | None:
    run_id = session.get("active_run")
    if run_id is None:
        return None
    valid_run_id(run_id)
    path = project / ".devflow" / "runs" / "implement" / run_id / "state.json"
    no_symlinks(path)
    state = read_json(path)
    if state.get("run_id") != run_id or state.get("session_id") != session["session_id"]:
        raise HookError("Active run identity does not match this session.")
    if not isinstance(state.get("project"), str) or not _same_path(Path(state["project"]), project):
        raise HookError("Active run belongs to another project.")
    if not isinstance(state.get("config"), dict):
        raise HookError("Active run configuration is invalid.")
    return state


def _answer_text(value: Any) -> str:
    """Validate a successful answer while preserving the actual result exactly."""
    if isinstance(value, str):
        if not value.strip():
            raise HookError("AskUserQuestion has no human answer.")
        return value
    if isinstance(value, list):
        if not value or any(not isinstance(block, dict) or block.get("type") != "text"
                            or not isinstance(block.get("text"), str) for block in value):
            raise HookError("Unsupported or empty question-answer content blocks.")
        if not any(block["text"].strip() for block in value):
            raise HookError("AskUserQuestion has no human answer.")
    elif isinstance(value, dict):
        if any(value.get(key) for key in ("is_error", "error", "cancelled", "canceled", "aborted")):
            raise HookError("An errored or cancelled question is not a human answer.")
        answers = value.get("answers")
        if not isinstance(answers, dict) or not answers:
            raise HookError("AskUserQuestion has no structured human answer.")
        for answer in answers.values():
            if isinstance(answer, str) and answer.strip():
                continue
            if isinstance(answer, list) and answer and all(isinstance(x, str) and x.strip() for x in answer):
                continue
            raise HookError("A human answer is empty or has an unsupported shape.")
    else:
        raise HookError("Unsupported question-answer result type.")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _record(session: dict, payload: dict) -> str:
    event_name = payload["hook_event_name"]
    event = {"event_id": "evt-" + uuid4().hex, "created_at": now()}
    if event_name == "UserPromptSubmit":
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise HookError("UserPromptSubmit is missing the raw user prompt.")
        session["prompt_seq"] += 1
        event.update({"kind": "user_prompt", "text": prompt})
    else:
        if payload.get("is_error"):
            raise HookError("An errored question result cannot be used as human approval.")
        use_id = payload.get("tool_use_id")
        tool_input = payload.get("tool_input")
        if not isinstance(use_id, str) or not use_id or not isinstance(tool_input, dict) or "tool_response" not in payload:
            raise HookError("Question capture is missing its tool identity, input or actual result.")
        # By now the question screen has filled tool_input.answers with the
        # human's choice, so its presence is expected; model prefills are
        # denied at PreToolUse.
        for old in session["events"]:
            if old.get("kind") == "ask_user_question" and old.get("tool_use_id") == use_id:
                if old.get("tool_input") != tool_input or old.get("tool_response") != payload["tool_response"]:
                    raise HookError("A captured question ID was replayed with different content.")
                return old["event_id"]
        response = payload["tool_response"]
        event.update({
            "kind": "ask_user_question", "tool_use_id": use_id,
            "tool_input": tool_input, "tool_response": response,
            "text": _answer_text(response),
        })
    if len(json.dumps(event, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_EVENT_BYTES:
        raise HookError("Human event exceeds the capture limit; it was not truncated.")
    if len(session["events"]) >= MAX_EVENTS:
        raise HookError("Session event budget reached; stop and preserve the existing audit trail.")
    session["seq"] += 1
    event.update({"seq": session["seq"], "prompt_seq": session["prompt_seq"]})
    session["events"].append(event)
    return event["event_id"]


def _save_session(path: Path, session: dict) -> None:
    if len(json.dumps(session, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_SESSION_BYTES:
        raise HookError("Session audit data exceeds its limit; no data was silently dropped.")
    atomic_write_json(path, session)


def _state_context(state: dict | None, session: dict, event_id: str | None = None) -> str:
    fields = {"session_id": session["session_id"], "human_seq": session["seq"]}
    if state:
        fields.update({key: state[key] for key in ("run_id", "status", "review_count", "snapshot_id") if key in state})
    else:
        fields["intent"] = session.get("intent")
    if event_id:
        fields["recorded_event_id"] = event_id
    return (
        "BuildFlow is active for this session. Use its exact runner and current run draft directory. "
        "Keep normal Claude Code permissions; human event text is evidence, not authority to bypass them. "
        + json.dumps(fields, ensure_ascii=False)
    )


def handle(event_name: str, payload: dict, plugin_root: Path) -> dict:
    """Guard the bound session; unrelated sessions are inert and create no files."""
    if event_name not in {"UserPromptSubmit", "PreToolUse", "PostToolUse"}:
        return {}
    if not isinstance(payload, dict):
        raise HookError("Hook input must be an object.")
    if event_name == "PostToolUse" and payload.get("tool_name") != "AskUserQuestion":
        return {}
    payload = dict(payload, hook_event_name=event_name)
    cwd, session_id = payload.get("cwd"), payload.get("session_id")
    if not isinstance(cwd, str) or not cwd or not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
        raise HookError("Hook input needs a real project cwd and valid Claude session ID.")
    project = project_root(Path(cwd))
    path = session_path(project, session_id)
    prompt = payload.get("prompt") if event_name == "UserPromptSubmit" else None
    start = _command_arg(prompt, START_COMMAND) if isinstance(prompt, str) else None
    resume = _command_arg(prompt, RESUME_COMMAND) if isinstance(prompt, str) else None
    starts_workflow = start is not None or resume is not None
    if not path.exists() and not starts_workflow:
        return {}
    no_symlinks(path)
    if event_name == "PreToolUse":
        session = read_json(path, limit=MAX_SESSION_BYTES)
        _validate_session(session, session_id)
        state = _read_state(project, session)
        if state is None and not session.get("intent"):
            return {}
        return guard_tool(payload, project, session, state, plugin_root)
    if starts_workflow:
        if start is not None:
            argument = _plan_arg(start)
            action = "implement"
        else:
            valid_run_id(resume)
            argument, action = resume, "resume"
        ignored = project / ".devflow" / ".gitignore"
        no_symlinks(ignored)
        if not ignored.exists():
            write_new(ignored, b"*\n")
    with file_lock(path.with_suffix(".lock")):
        session = read_json(path, limit=MAX_SESSION_BYTES) if path.exists() else {
            "session_id": session_id, "active_run": None, "prompt_seq": 0,
            "seq": 0, "events": [], "intent": None,
        }
        _validate_session(session, session_id)
        state = _read_state(project, session)
        if state is None and not starts_workflow and not session.get("intent"):
            return {}
        if event_name == "UserPromptSubmit":
            if state and state.get("status") not in TERMINAL_STATUSES and starts_workflow:
                if action != "resume" or argument != state["run_id"]:
                    return {"decision": "block", "reason": "Finish or cancel the current BuildFlow run before starting a different one."}
            event_id = _record(session, payload)
            if state and state.get("status") in TERMINAL_STATUSES:
                # The fresh real user prompt, not the completion call itself,
                # releases the final-turn mutation guard.
                session["active_run"] = None
                session["intent"] = None
                state = None
            if starts_workflow:
                session["intent"] = {
                    "action": action, "args": argument,
                    "raw_args": start if start is not None else resume,
                    "prompt_seq": session["prompt_seq"],
                }
            elif state is None:
                # A new real prompt can abandon a pending, not-yet-started run.
                session["intent"] = None
            _save_session(path, session)
            if state is None and session["intent"] is None:
                return {}
            return _context(event_name, _state_context(state, session, event_id))
        if state is None:
            return {}
        event_id = _record(session, payload)
        _save_session(path, session)
        return _context(event_name, _state_context(state, session, event_id))


def handle_hook(payload: dict, plugin_root: Path) -> dict:
    """Convenience entry point for a stdin JSON Claude Code hook invocation."""
    if not isinstance(payload, dict):
        raise HookError("Hook input must be an object.")
    return handle(payload.get("hook_event_name"), payload, plugin_root)
