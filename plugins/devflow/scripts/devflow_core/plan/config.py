"""Versioned, immutable-per-run configuration. Secrets remain in provider CLIs."""

from __future__ import annotations

import copy
from pathlib import Path
import re
import shutil

from ..common.hosts import executable_name

from ..common.errors import DevFlowError
from .interview import INTERVIEW_NAME_PATTERN, inspect_interview_skill
from ..common.review import validate_limit
from ..common.settings import load_workflow_config, validate_reviewers


# The published plan's name: a template whose only values are {run_id} and {ticket}.
# The default gives the request's first ticket ID, or plan-<run-id> without one.
FILENAME_DEFAULT = "{ticket}"
FILENAME_MAX = 128
TICKET_ID = re.compile(r"(?<![A-Za-z0-9_])[A-Z][A-Z0-9]{0,31}-[0-9]{1,12}(?![A-Za-z0-9_])")
# The longest values (29 and 45 characters). A template that is safe and short enough with
# them is safe with every real value: both use only safe characters, start like these, never
# end with ".", and always contain "-", so they never form a reserved name.
_LONGEST = {"run_id": "00000000T000000Z-000000000000", "ticket": "A" + "0" * 31 + "-" + "0" * 12}
_RESERVED = {".git", ".devflow", ".claude", ".codex", ".opencode", ".agents"}
_WINDOWS = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_name(part: str) -> bool:
    """A portable path component: never a drive, shell expression, traversal, or a
    control/configuration directory."""
    return bool(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", part)
                and not part.endswith(".") and part.casefold() not in _RESERVED
                and part.split(".", 1)[0].upper() not in _WINDOWS)


def fill(template: str, values: dict) -> str:
    """Replace {run_id} and {ticket}; any other brace stays and fails safe_name."""
    return re.sub(r"\{(run_id|ticket)\}", lambda match: values[match.group(1)], template)


def publish_name(template: str, request: str, run_id: str) -> str:
    """The published plan's name, without an extension."""
    ticket = TICKET_ID.search(request)
    if "{ticket}" in template and not ticket:
        return "plan-" + run_id
    return fill(template, {"run_id": run_id, "ticket": ticket.group() if ticket else ""})


def validate_config(config: dict) -> dict:
    expected = {"schema_version", "grill_skill", "max_reviews", "max_packet_bytes", "reviewers", "publish"}
    if set(config) - expected:
        raise DevFlowError(f"Unknown configuration fields: {sorted(set(config) - expected)}")
    if type(config.get("schema_version")) is not int or config.get("schema_version") != 1:
        raise DevFlowError("Unsupported configuration schema_version (expected 1).")
    skill = config.get("grill_skill")
    if not isinstance(skill, str) or not INTERVIEW_NAME_PATTERN.fullmatch(skill):
        raise DevFlowError("grill_skill must be an installed skill name, with no leading slash.")
    count = validate_limit(config.get("max_reviews"))
    limit = config.get("max_packet_bytes")
    if type(limit) is not int or not 1024 <= limit <= 16 * 1024 * 1024:
        raise DevFlowError("max_packet_bytes must be between 1 KiB and 16 MiB.")
    publication = config.get("publish")
    if (not isinstance(publication, dict) or not {"enabled", "directory"} <= set(publication)
            or set(publication) - {"enabled", "directory", "filename"}):
        raise DevFlowError("publish must contain enabled and directory, and may contain filename.")
    if type(publication["enabled"]) is not bool:
        raise DevFlowError("publish.enabled must be boolean.")
    directory = publication["directory"]
    if not isinstance(directory, str) or not directory or len(directory) > 180:
        raise DevFlowError("publish.directory must be a bounded project-relative directory.")
    # Explicit configuration is frozen at start.
    if not all(safe_name(part) for part in directory.split("/")):
        raise DevFlowError("publish.directory must use safe relative directory components separated by '/'.")
    # A missing filename (a run started before 0.7.3) means the default.
    if "filename" in publication:
        template = publication["filename"]
        # An empty or non-string value gives an empty sample, which is not a safe name.
        sample = fill(template, _LONGEST) if isinstance(template, str) else ""
        if not safe_name(sample) or len(sample) > FILENAME_MAX:
            raise DevFlowError("publish.filename must be a file name without an extension that uses only "
                               "{run_id}, {ticket} and safe characters, at most 128 characters with the longest values.")
    validate_reviewers(config.get("reviewers"), count)
    return copy.deepcopy(config)


def load_config(project: Path, workflow) -> dict:
    return load_workflow_config(project, workflow, validate_config)


def diagnose(project: Path, workflow, effective_config: dict | None = None, *, host: str) -> dict:
    config = load_config(project, workflow) if effective_config is None else validate_config(effective_config)
    interview = inspect_interview_skill(project, config["grill_skill"], host=host)
    binaries = {}
    for name in ("uv", executable_name(host), *[p["command"][0] for p in config["reviewers"]]):
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
