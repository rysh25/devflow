"""Project settings and the reviewer rules shared by every workflow."""
from __future__ import annotations

import copy
from pathlib import Path
import re

from .errors import DevFlowError
from .paths import workflow_names
from .storage import no_symlinks, read_json


SHELLS = frozenset({"sh", "bash", "zsh", "fish", "dash", "ksh", "csh", "tcsh", "cmd", "powershell", "pwsh"})
REVIEWER_KINDS = frozenset({"codex", "opencode"})
REVIEWER_FIELDS = frozenset({"id", "kind", "command", "model", "effort", "timeout_seconds", "protocol_version", "max_output_bytes"})
REVIEWER_REQUIRED = frozenset({"id", "kind", "command", "model", "effort", "timeout_seconds"})
DEFAULT_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024


def workflow_overlay(project: Path, workflow: str) -> dict:
    """Return only the requested workflow's settings and shared reviewer policy."""
    names = workflow_names()
    if workflow not in names:
        raise DevFlowError("Unknown DevFlow workflow")
    path = Path(project) / "devflow.config.json"
    no_symlinks(path)
    if not path.exists():
        return {}
    document = read_json(path)
    allowed = {"schema_version", "reviewers", "max_reviews", *names}
    if set(document) - allowed:
        raise DevFlowError("Unknown devflow.config.json keys: " + ", ".join(sorted(set(document) - allowed)))
    version = document.get("schema_version", 1)
    if type(version) is not int or version != 1:
        raise DevFlowError("Unsupported devflow.config.json schema_version; expected 1")
    for name in names:
        if name in document and not isinstance(document[name], dict):
            raise DevFlowError("devflow.config.json " + name + " must contain an object")
    result = {name: copy.deepcopy(document[name]) for name in ("reviewers", "max_reviews") if name in document}
    result.update(copy.deepcopy(document.get(workflow, {})))
    return result


def _program(value: str) -> str:
    name = re.split(r"[\\/]", value)[-1].casefold()
    for suffix in (".exe", ".cmd", ".bat", ".ps1"):
        name = name.removesuffix(suffix)
    return name


def validate_reviewers(reviewers: object, max_reviews: int) -> list:
    """One rule set for the shared reviewer list, used by every workflow.

    Reviewer settings are trusted human configuration; the command checks are
    defense in depth against accidents, not a sandbox.
    """
    if not isinstance(reviewers, list) or len(reviewers) < 2 or len(reviewers) > max_reviews:
        raise DevFlowError("Configure from two reviewers up to max_reviews; every reviewer must be able to approve")
    seen = set()
    for reviewer in reviewers:
        validate_reviewer(reviewer)
        if reviewer["id"] in seen:
            raise DevFlowError("Reviewer IDs must be unique lowercase names")
        seen.add(reviewer["id"])
    if len({reviewer["kind"] for reviewer in reviewers}) < 2:
        raise DevFlowError("Reviews need at least two different reviewer kinds for independence")
    return copy.deepcopy(reviewers)


def validate_reviewer(reviewer: object) -> dict:
    """The rules for one reviewer entry (shared by settings and the CLI adapters)."""
    if not isinstance(reviewer, dict) or set(reviewer) - REVIEWER_FIELDS or not REVIEWER_REQUIRED <= set(reviewer):
        raise DevFlowError("Invalid reviewer fields")
    identifier = reviewer["id"]
    if not isinstance(identifier, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", identifier):
        raise DevFlowError("Reviewer IDs must be unique lowercase names")
    if reviewer["kind"] not in REVIEWER_KINDS:
        raise DevFlowError("Unsupported reviewer kind; implement an explicit adapter first")
    command = reviewer["command"]
    if (not isinstance(command, list) or not command or len(command) > 128
            or any(not isinstance(x, str) or not x or "\x00" in x for x in command)):
        raise DevFlowError("Reviewer command must be a nonempty argv array, never a shell string")
    if any(_program(item) in SHELLS for item in command):
        raise DevFlowError("Reviewer commands must invoke the reviewer CLI directly, never through a shell")
    if any(item == "--yolo" or item.startswith("--dangerously") for item in command):
        raise DevFlowError("Reviewer commands must not disable the CLI's own safety controls")
    for field in ("model", "effort"):
        if not isinstance(reviewer[field], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:-]*", reviewer[field]):
            raise DevFlowError(f"Invalid reviewer {field}")
    timeout = reviewer["timeout_seconds"]
    if type(timeout) is not int or not 5 <= timeout <= 3600:
        raise DevFlowError("timeout_seconds must be an integer between 5 and 3600")
    if "protocol_version" in reviewer:
        if reviewer["kind"] != "opencode" or type(reviewer["protocol_version"]) is not int or reviewer["protocol_version"] not in (1, 2):
            raise DevFlowError("protocol_version is an OpenCode setting and must be 1 or 2")
    if "max_output_bytes" in reviewer:
        limit = reviewer["max_output_bytes"]
        if type(limit) is not int or not 1024 <= limit <= MAX_OUTPUT_BYTES:
            raise DevFlowError("max_output_bytes must be an integer between 1024 and 16 MiB")
    return reviewer
