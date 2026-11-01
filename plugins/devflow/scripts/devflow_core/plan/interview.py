"""Bounded, read-only inspection of a configured local interview skill.

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


MAX_FRONTMATTER_BYTES = 64 * 1024
MAX_FRONTMATTER_LINES = 256
_KEY = re.compile(r"([A-Za-z][A-Za-z0-9_-]*):(?:[ \t]+(.*)|[ \t]*)$")
_TRUE = {"true", "yes", "on", "1"}
_FALSE = {"false", "no", "off", "0"}
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


def _metadata(data: bytes) -> dict:
    lines = data.splitlines(keepends=True)
    if not lines or not data.strip():
        raise _MetadataError("The skill file is empty.")
    if data.startswith(b"\xef\xbb\xbf"):
        raise _MetadataError("A byte-order mark makes frontmatter recognition uncertain.", "unverified")
    if lines[0].rstrip(b"\r\n") != b"---":
        # Claude Code requires the opening marker on the first line. Text later
        # in the body is not metadata, and is never executed by this checker.
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
        if key in {"disable-model-invocation", "user-invocable"} and raw.startswith(("'", '"')):
            raise _MetadataError("Quoted invocation booleans require runtime verification; use documented unquoted values.", "unverified")
        if re.fullmatch(r"[|>](?:[1-9][+-]?|[+-][1-9]?)?", raw):
            if key in {"name", "disable-model-invocation", "user-invocable", "context"}:
                raise _MetadataError("Invocation metadata must use single-line scalars.", "unverified")
            fields[key] = None
            block = True
        else:
            fields[key] = _scalar(raw)
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


def _inspect_candidate(path: Path, scope: str, kind: str) -> dict | None:
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
        metadata = _metadata(data)
    except _MetadataError as exc:
        return {**candidate, "status": exc.status, "reason": str(exc)}
    except (OSError, ValueError, RuntimeError) as exc:
        return {**candidate, "status": "unverified", "reason": "Cannot read skill metadata: " + type(exc).__name__}
    candidate["metadata"] = metadata
    if metadata["disable_model_invocation"]:
        return {**candidate, "status": "blocked", "reason": "disable-model-invocation is true; Claude must not invoke or reproduce this skill automatically."}
    if metadata["context"] == "fork":
        return {**candidate, "status": "blocked", "reason": "context: fork cannot conduct DevFlow's human interview in the main conversation."}
    return {**candidate, "status": "available", "reason": "Local metadata permits model invocation in the main conversation; runtime invocation remains unverified."}


def inspect_interview_skill(project: Path, configured_name: str, *, config_dir: Path | None = None) -> dict:
    """Inspect four exact paths at most; a positive result is not runtime proof."""
    report = {"configured_name": configured_name, "status": "unverified", "static_ready": False,
              "model_invocable": None, "runtime_verified": False, "source": None,
              "checked_locations": [], "candidates": [], "limitations": list(_LIMITATIONS)}
    if not isinstance(configured_name, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", configured_name):
        return {**report, "status": "invalid", "reason": "The configured skill name is not a bounded portable name."}
    if ":" in configured_name:
        return {**report, "reason": "Namespaced skills need Claude Code's runtime registry; this offline checker cannot resolve their installation or callability."}
    if configured_name in {".", ".."} or configured_name.casefold() in {"synced", "anthropic-skills"}:
        return {**report, "status": "invalid", "reason": "The configured name is reserved or not a skill directory name."}
    try:
        personal = (config_dir if config_dir is not None else
                    Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))).expanduser().absolute()
        project_config = Path(project).absolute() / ".claude"
        locations = []
        seen = set()
        for scope, directory in (("personal", personal), ("project", project_config)):
            for kind, path in (("skill", directory / "skills" / configured_name / "SKILL.md"),
                               ("command", directory / "commands" / (configured_name + ".md"))):
                identity = str(path.resolve(strict=False))
                if identity not in seen:
                    seen.add(identity)
                    locations.append((scope, kind, path))
    except (OSError, ValueError, RuntimeError) as exc:
        return {**report, "reason": "Cannot resolve configured search locations: " + type(exc).__name__}
    report["checked_locations"] = [str(path) for _, _, path in locations]
    candidates = [candidate for scope, kind, path in locations
                  if (candidate := _inspect_candidate(path, scope, kind)) is not None]
    report["candidates"] = candidates
    if not candidates:
        return {**report, "status": "missing", "reason": "No skill or command file was found at the checked personal/project paths. Other runtime sources and aliases are unverified, not proven absent."}
    if len(candidates) != 1:
        return {**report, "status": "ambiguous", "reason": "Multiple local definitions match the configured name. Verify Claude Code's selected source and resolve shadowing explicitly; this checker does not select a fallback."}
    candidate = candidates[0]
    metadata = candidate.get("metadata", {})
    return {**report, "status": candidate["status"], "source": candidate["path"],
            "static_ready": candidate["status"] == "available",
            "model_invocable": (not metadata["disable_model_invocation"] if metadata else None),
            "reason": candidate["reason"]}
