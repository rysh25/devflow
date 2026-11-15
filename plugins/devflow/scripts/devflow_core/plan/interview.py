"""Bounded inspection of a configured local interview skill; it changes nothing.

This is intentionally not Claude Code's skill loader or a general YAML parser.
It never invokes a skill, executes frontmatter, reads settings/credentials, or
chooses another skill. A passing result describes static prerequisites only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat

from ..common.errors import DevFlowError
from ..common.hosts import host_profile
from ..common.hosts.skills import skill_locations


MAX_FRONTMATTER_BYTES = 64 * 1024
MAX_FRONTMATTER_LINES = 256
INTERVIEW_NAME_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_KEY = re.compile(r"([A-Za-z][A-Za-z0-9_-]*):(?:[ \t]+(.*)|[ \t]*)$")
_TRUE = {"true", "yes", "on", "1"}
_FALSE = {"false", "no", "off", "0"}
# YAML 1.2 core-schema booleans; Devin documents subagent as a boolean.
_YAML_BOOLEANS = {"true": True, "True": True, "TRUE": True, "false": False, "False": False, "FALSE": False}
_LIMITATIONS = [
    "Static metadata only: actual Skill invocation, permissions, trust, and session visibility are not verified.",
    "Only the configured name at personal/project skill and command paths is inspected; frontmatter aliases in other folders, parent/nested projects, enterprise, plugin, synced, and additional-directory skills are not resolved.",
    "Settings and credentials are not read; skillOverrides, setting-source switches, and managed policy may change availability.",
    "Skill bodies, nested skill dependencies, and dynamic commands are not executed or validated. Multiple local definitions require runtime source verification; no shadowed definition is selected automatically.",
]


class _MetadataError(DevFlowError):
    def __init__(self, message: str, status: str = "invalid"):
        super().__init__(message)
        self.status = status


def _scalar(value: str) -> str:
    """Read a small, explicit subset of YAML scalars; never guess extensions."""
    value = value.strip()
    if not value:
        return ""
    if value.startswith('"'):
        try:
            parsed, end = json.JSONDecoder().raw_decode(value)
        except ValueError as exc:
            raise _MetadataError("Unsupported or malformed quoted frontmatter scalar.", "unverified") from exc
        tail = value[end:].strip()
        if not isinstance(parsed, str) or (tail and not tail.startswith("#")):
            raise _MetadataError("Malformed quoted frontmatter scalar.")
        return parsed
    if value.startswith("'"):
        match = re.fullmatch(r"'((?:[^']|'')*)'\s*(?:#.*)?", value)
        if not match:
            raise _MetadataError("Malformed single-quoted frontmatter scalar.")
        return match[1].replace("''", "'")
    value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
    if value.startswith(("[", "{", "&", "*", "!", "@", "`")):
        raise _MetadataError("Complex YAML, aliases, anchors, or tags require runtime verification.", "unverified")
    if re.search(r":(?:\s|$)", value):
        raise _MetadataError("Unquoted mapping delimiter in a frontmatter scalar.")
    return value


#: Devin frontmatter keys that decide whether a skill can hold the interview.
_DEVIN_DECIDING = ("triggers", "subagent", "agent")


def _inline_list(raw: str) -> list[str]:
    """``[a, "b"]``: a flow list of single-line scalars (no nesting)."""
    inner = raw.strip()[1:-1]
    items, current, quote = [], "", None
    for char in inner:
        if quote:
            current += char
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
            current += char
        elif char == ",":
            items.append(current)
            current = ""
        elif char in "[]{}":
            raise _MetadataError("Nested YAML collections require runtime verification.", "unverified")
        else:
            current += char
    if quote:
        raise _MetadataError("Unterminated quoted list item.")
    if current.strip() or items:
        items.append(current)
    values = [_scalar(item) for item in items]
    if any(not value for value in values):
        raise _MetadataError("Empty list item in frontmatter.")
    return values


def devin_frontmatter(text: str) -> dict:
    """The part of a Devin SKILL.md frontmatter (the text between the --- lines) DevFlow reads.

    Not a YAML parser: top-level ``key: value`` scalars, flow lists ``[a, b]``,
    and block lists of scalars (``- value`` lines under a key). Indented lines
    under any other key (``permissions``, ``allowed-tools`` entries and the like)
    are skipped without being interpreted, but the key itself is kept with the
    value ``None``. A deciding key keeps its documented type or is unverified:
    ``triggers`` a list, ``subagent`` an unquoted YAML boolean (read as bool),
    ``agent`` a string (or no value). Shared by the interview-skill check and tools/package.py.
    """
    fields: dict[str, object] = {}
    current = None  # The key whose indented lines or block list follow.
    opened = set()  # Keys written without a value; a block list may follow.
    indents: dict[str, int] = {}  # The indentation of each block list's first item.
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "\t" in line or any(ord(char) < 32 for char in line):
            raise _MetadataError("Unsupported control characters in frontmatter.", "unverified")
        item = line.strip().startswith("- ") or line.strip() == "-"
        if line.startswith(" ") or (item and current is not None):
            if current is None:
                raise _MetadataError("Indented frontmatter line without a key.")
            value = fields[current]
            if item and (value == [] or isinstance(value, list)):
                indent = len(line) - len(line.lstrip(" "))
                if indents.setdefault(current, indent) != indent:
                    raise _MetadataError("Block list items must share one indentation.", "unverified")
                entry = _scalar(line.strip()[1:])
                if not entry or entry.startswith(("[", "{")):
                    raise _MetadataError("Block list items must be single-line scalars.", "unverified")
                value.append(entry)
            elif current in _DEVIN_DECIDING:
                raise _MetadataError(f"{current} is in a form this check does not read.", "unverified")
            else:
                fields[current] = None  # Nested content of a key DevFlow does not decide on.
            continue
        current = None
        match = _KEY.fullmatch(line)
        if not match:
            raise _MetadataError("Frontmatter is not a YAML mapping Devin reads.")
        key, raw = match[1], (match[2] or "").strip()
        if key in fields:
            raise _MetadataError("Duplicate frontmatter field: " + key)
        if not raw or raw.startswith("#"):
            fields[key] = []  # A block list or nested mapping follows (or the value is empty).
            current = key
            opened.add(key)
        elif raw.startswith("[") and re.split(r"\s+#", raw, maxsplit=1)[0].rstrip().endswith("]"):
            fields[key] = _inline_list(re.split(r"\s+#", raw, maxsplit=1)[0].rstrip())
        elif re.fullmatch(r"[|>](?:[1-9][+-]?|[+-][1-9]?)?", raw):
            if key in _DEVIN_DECIDING or key == "name":
                raise _MetadataError(f"{key} must be a single-line value.", "unverified")
            fields[key] = None
            current = key
        elif key == "subagent":
            value = re.split(r"\s+#", raw, maxsplit=1)[0].rstrip()
            if value not in _YAML_BOOLEANS:
                raise _MetadataError("subagent is not an unquoted YAML boolean.", "unverified")
            fields[key] = _YAML_BOOLEANS[value]
        elif key == "triggers":
            raise _MetadataError("triggers is not a list.", "unverified")
        else:
            fields[key] = _scalar(raw)
    for key in ("triggers", "subagent"):
        if key in opened and fields[key] in ([], None):
            raise _MetadataError(f"{key} has no value this check reads.", "unverified")
    if "agent" in fields:
        if "agent" in opened and fields["agent"] == []:
            fields["agent"] = None  # "agent:" with no value: no profile.
        elif not isinstance(fields["agent"], str):
            raise _MetadataError("agent is not a string.", "unverified")
    return fields


def _devin_metadata(header: str) -> dict:
    fields = devin_frontmatter(header)
    triggers = fields.get("triggers", ["user", "model"])
    if not isinstance(triggers, list):
        raise _MetadataError("triggers is in a form this check does not read.", "unverified")
    subagent = fields.get("subagent", False)
    if not isinstance(subagent, bool):
        raise _MetadataError("subagent is not a recognized boolean.", "unverified")
    agent = fields.get("agent")
    return {"frontmatter": True, "name": fields.get("name"), "triggers": triggers,
            "model_invocable": "model" in triggers, "subagent": subagent,
            "agent": agent if isinstance(agent, str) and agent else None}


def _metadata(data: bytes, host: str) -> dict:
    lines = data.splitlines(keepends=True)
    if not lines or not data.strip():
        raise _MetadataError("The skill file is empty.")
    if data.startswith(b"\xef\xbb\xbf"):
        raise _MetadataError("A byte-order mark makes frontmatter recognition uncertain.", "unverified")
    if lines[0].rstrip(b"\r\n") != b"---":
        # Claude Code requires the opening marker on the first line. Text later
        # in the body is not metadata, and is never executed by this checker.
        if host == "devin":
            return {"frontmatter": False, "name": None, "triggers": ["user", "model"], "model_invocable": True,
                    "subagent": False, "agent": None}
        return {"frontmatter": False, "disable_model_invocation": False, "context": None}
    closing = next((index for index, line in enumerate(lines[1:], 1)
                    if line.rstrip(b"\r\n") == b"---"), None)
    if closing is None:
        if len(data) > MAX_FRONTMATTER_BYTES or len(lines) > MAX_FRONTMATTER_LINES:
            raise _MetadataError("Frontmatter exceeds the bounded inspection limit.", "unverified")
        raise _MetadataError("Frontmatter has no closing --- marker.")
    header = b"".join(lines[1:closing])
    if len(header) > MAX_FRONTMATTER_BYTES or closing > MAX_FRONTMATTER_LINES:
        raise _MetadataError("Frontmatter exceeds the bounded inspection limit.", "unverified")
    try:
        decoded = header.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _MetadataError("Frontmatter must be valid UTF-8.") from exc
    if host == "devin":
        return _devin_metadata(decoded)
    fields = {}
    block = False
    for line in decoded.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "\t" in line or any(ord(char) < 32 for char in line):
            raise _MetadataError("Unsupported control characters in frontmatter.", "unverified")
        if line.startswith(" "):
            if block:
                continue
            raise _MetadataError("Nested or multiline YAML requires runtime verification.", "unverified")
        block = False
        match = _KEY.fullmatch(line)
        if not match:
            raise _MetadataError("Frontmatter is not a supported flat YAML mapping.", "unverified")
        key, raw = match[1], match[2] or ""
        if key in fields:
            raise _MetadataError("Duplicate frontmatter field: " + key)
        raw = re.split(r"\s+#", raw, maxsplit=1)[0].strip() if not raw.startswith(("'", '"')) else raw
        if host == "claude" and key in {"disable-model-invocation", "user-invocable"} and raw.startswith(("'", '"')):
            raise _MetadataError("Quoted invocation booleans require runtime verification; use documented unquoted values.", "unverified")
        if re.fullmatch(r"[|>](?:[1-9][+-]?|[+-][1-9]?)?", raw):
            if key in {"name", "disable-model-invocation", "user-invocable", "context"}:
                raise _MetadataError("Invocation metadata must use single-line scalars.", "unverified")
            fields[key] = None
            block = True
        else:
            fields[key] = _scalar(raw)
    if host == "codex":
        return {"frontmatter": True, "name": fields.get("name"), "native_policy_verified": False}
    disabled = fields.get("disable-model-invocation", "false").casefold()
    if disabled not in _TRUE | _FALSE:
        raise _MetadataError("disable-model-invocation is not a recognized boolean.")
    if "user-invocable" in fields and fields["user-invocable"].casefold() not in _TRUE | _FALSE:
        raise _MetadataError("user-invocable is not a recognized boolean.")
    context = fields.get("context")
    if context not in (None, "fork"):
        raise _MetadataError("Unknown context value requires runtime verification.", "unverified")
    return {"frontmatter": True, "name": fields.get("name"),
            "disable_model_invocation": disabled in _TRUE, "context": context}


def _inspect_candidate(path: Path, scope: str, kind: str, host: str) -> dict | None:
    candidate = {"path": str(path), "scope": scope, "kind": kind}
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        return {**candidate, "status": "unverified", "reason": "Cannot inspect skill path: " + type(exc).__name__}
    if not stat.S_ISREG(info.st_mode):
        return {**candidate, "status": "invalid", "reason": "The skill path is not a regular file."}
    try:
        resolved = path.resolve(strict=True)
        candidate["resolved_path"] = str(resolved)
        if resolved.name != path.name:
            return {**candidate, "status": "unverified", "reason": "A file symlink targets a differently named file; its contents were not read."}
        # Normal directory symlinks (for example shared skill installations) are
        # supported. Never open a device/FIFO, including after a path race.
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return {**candidate, "status": "invalid", "reason": "The skill path is not a regular file."}
            data = handle.read(MAX_FRONTMATTER_BYTES + 1)
        metadata = _metadata(data, host)
    except _MetadataError as exc:
        return {**candidate, "status": exc.status, "reason": str(exc)}
    except (OSError, ValueError, RuntimeError) as exc:
        return {**candidate, "status": "unverified", "reason": "Cannot read skill metadata: " + type(exc).__name__}
    candidate["metadata"] = metadata
    if host == "devin":
        if not metadata["model_invocable"]:
            return {**candidate, "status": "blocked", "reason": "triggers does not include model, so Devin's model "
                    "cannot invoke this skill for the interview."}
        if metadata["subagent"] or metadata["agent"]:
            return {**candidate, "status": "blocked", "reason": "The skill runs as a subagent (subagent: true or an "
                    "agent profile), and Devin gives subagents no ask_user_question, so it cannot hold the "
                    "interview in the main conversation."}
        return {**candidate, "status": "available", "reason": "Devin metadata permits model invocation in the main "
                "conversation; actual invocation is unverified (call the skill once in Devin to confirm)."}
    if host == "codex":
        return {**candidate, "status": "available", "reason": "Skill file is discoverable; native catalog policy and runtime use are unverified."}
    if metadata["disable_model_invocation"]:
        return {**candidate, "status": "blocked", "reason": "disable-model-invocation is true; Claude must not invoke or reproduce this skill automatically."}
    if metadata["context"] == "fork":
        return {**candidate, "status": "blocked", "reason": "context: fork cannot conduct DevFlow's human interview in the main conversation."}
    return {**candidate, "status": "available", "reason": "Local metadata permits model invocation in the main conversation; runtime invocation remains unverified."}


def inspect_interview_skill(project: Path, configured_name: str, *, config_dir: Path | None = None,
                            host: str) -> dict:
    """Inspect exact native locations; a positive result is not runtime proof."""
    report = {"configured_name": configured_name, "status": "unverified", "static_ready": False,
              "model_invocable": None, "runtime_verified": False, "source": None,
              "checked_locations": [], "candidates": [], "limitations": list(_LIMITATIONS)}
    if host == "codex":
        report["limitations"] = ["Offline file discovery only; the native catalog determines execution policy.",
                                 "Question tool availability and actual skill execution are unverified."]
    elif host == "devin":
        report["limitations"] = [
            "Static metadata only: actual skill invocation, permissions, workspace trust and ask_user_question are "
            "not verified.",
            "Only <directory>/<name>/SKILL.md in Devin's documented user and project skill directories is "
            "inspected; plugin skills, Devin Cloud skills, Windsurf's global skills (~/.codeium/<channel>/skills, "
            "which depend on Devin's release channel), nested imported folders and changes made by settings "
            "(such as turning off read_config_from imports) are not checked.",
            "Devin skips a skill whose frontmatter it cannot read without a warning; this check reads only the "
            "small part of the frontmatter it needs."]
    if not isinstance(configured_name, str) or not INTERVIEW_NAME_PATTERN.fullmatch(configured_name):
        return {**report, "status": "invalid", "reason": "The configured skill name is not a bounded portable name."}
    if configured_name in {".", ".."} or (host == "claude" and configured_name.casefold() in {"synced", "anthropic-skills"}):
        return {**report, "status": "invalid", "reason": "The configured name is reserved or not a skill directory name."}
    if ":" in configured_name:
        return {**report, "reason": "Namespaced skills require the host's native runtime catalog; installation and callability are unverified."}
    try:
        locations = skill_locations(project, configured_name, host=host, config_dir=config_dir)
    except (OSError, ValueError, RuntimeError) as exc:
        return {**report, "reason": "Cannot resolve configured search locations: " + type(exc).__name__}
    if host == "codex":
        paths = [str(path) for _, _, path in locations]
        candidates, seen = [], set()
        for value in paths:
            candidate = _inspect_candidate(Path(value), "native", "skill", host)
            if candidate is not None:
                identity = candidate.get("resolved_path", candidate["path"])
                if identity not in seen:
                    seen.add(identity)
                    candidates.append(candidate)
        if len(candidates) != 1:
            return {**report, "checked_locations": paths, "candidates": candidates,
                    "status": "ambiguous" if candidates else "missing",
                    "reason": "Select the configured skill from Codex's native catalog; no fallback is chosen."}
        candidate = candidates[0]
        available = candidate["status"] == "available"
        return {**report, "checked_locations": paths, "candidates": candidates,
                "source": candidate["path"], "status": candidate["status"], "static_ready": available,
                "reason": "Skill text is discoverable; actual native skill use and questions remain unverified.",
                "invocation": "Use the exact configured skill from the native catalog and read its instructions."}
    report["checked_locations"] = [str(path) for _, _, path in locations]
    candidates = [candidate for scope, kind, path in locations
                  if (candidate := _inspect_candidate(path, scope, kind, host)) is not None]
    report["candidates"] = candidates
    label = host_profile(host).label
    if not candidates:
        if host == "devin":
            return {**report, "status": "missing", "reason": "Not found at the checked locations "
                    "(checked_locations). Other runtime sources are unverified, not proven absent."}
        return {**report, "status": "missing", "reason": "No skill or command file was found at the checked personal/project paths. Other runtime sources and aliases are unverified, not proven absent."}
    if len(candidates) != 1:
        return {**report, "status": "ambiguous", "reason": f"Multiple local definitions match the configured name. Verify {label}'s selected source and resolve shadowing explicitly; this checker does not select a fallback."}
    candidate = candidates[0]
    metadata = candidate.get("metadata", {})
    invocable = (None if not metadata else metadata["model_invocable"] if host == "devin"
                 else not metadata["disable_model_invocation"])
    return {**report, "status": candidate["status"], "source": candidate["path"],
            "static_ready": candidate["status"] == "available", "model_invocable": invocable,
            "reason": candidate["reason"]}
