"""Project settings and the reviewer rules shared by every workflow."""
from __future__ import annotations

import copy
from pathlib import Path
import re

from .errors import DevFlowError
from .storage import no_symlinks, read_json


REVIEWER_KINDS = frozenset({"codex", "opencode"})
REVIEWER_FIELDS = frozenset({"id", "kind", "command", "model", "effort", "timeout_seconds", "max_output_bytes"})
REVIEWER_REQUIRED = frozenset({"id", "kind", "command", "model", "effort", "timeout_seconds"})
#: The largest reviewer output DevFlow reads, and the default.
MAX_OUTPUT_BYTES = 16 * 1024 * 1024


def _document(project: Path) -> dict:
    path = Path(project) / "devflow.config.json"
    no_symlinks(path)
    if not path.exists():
        return {}
    document = read_json(path)
    version = document.get("schema_version", 1)
    if type(version) is not int or version != 1:
        raise DevFlowError("Unsupported devflow.config.json schema_version; expected 1")
    return document


def validate_document(project: Path, workflows) -> None:
    """Reject unknown top-level keys; the caller passes the registered workflow names."""
    document = _document(project)
    allowed = {"schema_version", "reviewers", "max_reviews", *workflows}
    if set(document) - allowed:
        raise DevFlowError("Unknown devflow.config.json keys: " + ", ".join(sorted(set(document) - allowed)))


def workflow_overlay(project: Path, workflow: str) -> dict:
    """Only the requested workflow's section plus the shared reviewer policy."""
    document = _document(project)
    if workflow in document and not isinstance(document[workflow], dict):
        raise DevFlowError("devflow.config.json " + workflow + " must contain an object")
    result = {name: copy.deepcopy(document[name]) for name in ("reviewers", "max_reviews") if name in document}
    result.update(copy.deepcopy(document.get(workflow, {})))
    return result


def validate_reviewers(reviewers: object, max_reviews: int) -> list:
    """One rule set for the shared reviewer list, used by every workflow.

    Reviewer settings are trusted human configuration. DevFlow checks only what
    it needs to start each CLI and read its answer, never how the user runs it.
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
    if not isinstance(reviewer, dict):
        raise DevFlowError("Each reviewer must be an object")
    unknown, missing = set(reviewer) - REVIEWER_FIELDS, REVIEWER_REQUIRED - set(reviewer)
    if unknown or missing:
        raise DevFlowError("Invalid reviewer fields"
                           + (" (unknown: " + ", ".join(sorted(unknown)) + ")" if unknown else "")
                           + (" (missing: " + ", ".join(sorted(missing)) + ")" if missing else "")
                           + "; see the reviewer settings in the DevFlow README")
    identifier = reviewer["id"]
    if not isinstance(identifier, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", identifier):
        raise DevFlowError("Reviewer IDs must be unique lowercase names")
    if reviewer["kind"] not in REVIEWER_KINDS:
        raise DevFlowError("Unsupported reviewer kind; implement an explicit adapter first")
    command = reviewer["command"]
    if (not isinstance(command, list) or not command or len(command) > 128
            or any(not isinstance(x, str) or not x or "\x00" in x for x in command)):
        raise DevFlowError("Reviewer command must be a nonempty argv array of strings")
    # DevFlow writes these into its own arguments (``--model M``, Codex's
    # ``model_reasoning_effort="E"`` and OpenCode's ``M#E``), so they must not start
    # with "-" (an option), or hold spaces, double quotes, backslashes or "#" (the separator).
    for field in ("model", "effort"):
        if not isinstance(reviewer[field], str) or not re.fullmatch(r'(?!-)[^\x00-\x20\x7f"\\#]+', reviewer[field]):
            raise DevFlowError(f"Invalid reviewer {field}")
    timeout = reviewer["timeout_seconds"]
    if type(timeout) is not int or not 5 <= timeout <= 3600:
        raise DevFlowError("timeout_seconds must be an integer between 5 and 3600")
    if "max_output_bytes" in reviewer:
        limit = reviewer["max_output_bytes"]
        if type(limit) is not int or not 1024 <= limit <= MAX_OUTPUT_BYTES:
            raise DevFlowError("max_output_bytes must be an integer between 1024 and 16 MiB")
    return reviewer
