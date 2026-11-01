"""Versioned, immutable-per-run configuration. Secrets remain in provider CLIs."""

from __future__ import annotations

import copy
from pathlib import Path
import re
import shutil

from .storage import FlowError, read_json
from .interview import inspect_interview_skill
from ._vendor.flow_common.review import validate_limit
from ._vendor.flow_common.settings import workflow_overlay


def validate_config(config: dict) -> dict:
    expected = {"schema_version", "grill_skill", "max_reviews", "max_packet_bytes", "reviewers", "publish"}
    if set(config) - expected:
        raise FlowError(f"Unknown configuration fields: {sorted(set(config) - expected)}")
    if type(config.get("schema_version")) is not int or config.get("schema_version") != 1:
        raise FlowError("Unsupported configuration schema_version (expected 1).")
    skill = config.get("grill_skill")
    if not isinstance(skill, str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]+", skill):
        raise FlowError("grill_skill must be an installed skill name, with no leading slash.")
    count = validate_limit(config.get("max_reviews"))
    limit = config.get("max_packet_bytes")
    if type(limit) is not int or not 1024 <= limit <= 16 * 1024 * 1024:
        raise FlowError("max_packet_bytes must be between 1 KiB and 16 MiB.")
    publication = config.get("publish")
    if not isinstance(publication, dict) or set(publication) != {"enabled", "directory"}:
        raise FlowError("publish must contain enabled and directory.")
    if type(publication["enabled"]) is not bool:
        raise FlowError("publish.enabled must be boolean.")
    directory = publication["directory"]
    if not isinstance(directory, str) or not directory or len(directory) > 180:
        raise FlowError("publish.directory must be a bounded project-relative directory.")
    # A portable directory, never a drive, shell expression, traversal, or a
    # control/configuration directory. Explicit configuration is frozen at start.
    reserved = {".git", ".devflow", ".planflow", ".buildflow", ".claude", ".codex", ".opencode", ".agents"}
    windows = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    for part in directory.split("/"):
        if (not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", part)
                or part.endswith(".") or part.casefold() in reserved
                or part.split(".", 1)[0].upper() in windows):
            raise FlowError("publish.directory must use safe relative directory components separated by '/'.")
    reviewers = config.get("reviewers")
    if not isinstance(reviewers, list) or len(reviewers) < 2 or len(reviewers) > count:
        raise FlowError("Configure at least two distinct reviewers and no more than max_reviews.")
    ids = set()
    fields = {"id", "kind", "command", "model", "effort", "timeout_seconds", "protocol_version"}
    for provider in reviewers:
        if not isinstance(provider, dict) or set(provider) - fields:
            raise FlowError("Reviewer configuration contains unknown fields.")
        identifier = provider.get("id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", identifier) or identifier in ids:
            raise FlowError("Each reviewer needs a unique lowercase id.")
        ids.add(identifier)
        if provider.get("kind") not in {"codex", "opencode"}:
            raise FlowError("Unsupported reviewer kind. Add an explicit adapter before enabling it.")
        command = provider.get("command")
        if not isinstance(command, list) or not command or any(not isinstance(item, str) or not item or "\x00" in item for item in command):
            raise FlowError("command must be a nonempty argv array, not a shell command string.")
        for name in ("model", "effort"):
            if not isinstance(provider.get(name), str) or not provider[name].strip() or "\x00" in provider[name]:
                raise FlowError(f"Reviewer {identifier} requires {name}.")
        timeout = provider.get("timeout_seconds")
        if type(timeout) is not int or not 5 <= timeout <= 3600:
            raise FlowError("timeout_seconds must be an integer between 5 and 3600.")
        if "protocol_version" in provider and (type(provider["protocol_version"]) is not int or provider["protocol_version"] not in (1, 2)):
            raise FlowError("protocol_version must explicitly select a supported major version (1 or 2).")
    return copy.deepcopy(config)


def load_config(project: Path, plugin_root: Path) -> dict:
    value = read_json(plugin_root / "config" / "defaults.json")
    overlay = workflow_overlay(project, "plan")
    if "publish" in overlay and isinstance(overlay["publish"], dict):
        overlay["publish"] = {**value["publish"], **overlay["publish"]}
    # Lists replace in full. Review order is an explicit, immutable contract.
    value.update(overlay)
    return validate_config(value)


def diagnose(project: Path, plugin_root: Path, effective_config: dict | None = None) -> dict:
    config = load_config(project, plugin_root) if effective_config is None else validate_config(effective_config)
    interview = inspect_interview_skill(project, config["grill_skill"])
    binaries = {}
    for name in ("uv", "claude", *[p["command"][0] for p in config["reviewers"]]):
        found = shutil.which(name)
        binaries[name] = {"found": bool(found), "path": found}
    return {
        "schema_version": 1,
        "config": config,
        "configuration_source": "project_settings" if effective_config is None else "frozen_run",
        "binaries": binaries,
        "interview_skill": interview,
        "ready": interview["static_ready"] and all(item["found"] for item in binaries.values()),
        "readiness_scope": "Offline prerequisites only; no runtime Skill or provider calls are verified.",
        "checks_not_performed": [
            "CLI authentication, model availability, supported reasoning variants, and billing",
            "Claude Code hooks, effective skill permissions/overrides, and actual interview skill execution",
            "Real Windows/macOS CLI execution",
        ],
        "note": "Offline discovery only. Finding a binary does not verify provider readiness.",
    }
