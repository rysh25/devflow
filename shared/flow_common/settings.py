"""Shared project settings without runtime coupling between the workflows."""
from __future__ import annotations

import copy
from pathlib import Path

from .storage import FlowError, no_symlinks, read_json


def workflow_overlay(project: Path, workflow: str) -> dict:
    """Return only the requested workflow's settings and shared reviewer policy.

    Legacy planflow.config.json / buildflow.config.json are deliberately not
    imported: migration is an explicit operator decision, not a fallback.
    """
    if workflow not in {"plan", "implement"}:
        raise FlowError("Unknown DevFlow workflow")
    path = Path(project) / "devflow.config.json"
    no_symlinks(path)
    if not path.exists():
        return {}
    document = read_json(path)
    if not isinstance(document, dict):
        raise FlowError("devflow.config.json must contain an object")
    allowed = {"schema_version", "reviewers", "max_reviews", "plan", "implement"}
    if set(document) - allowed:
        raise FlowError("Unknown devflow.config.json keys: " + ", ".join(sorted(set(document) - allowed)))
    version = document.get("schema_version", 1)
    if type(version) is not int or version != 1:
        raise FlowError("Unsupported devflow.config.json schema_version; expected 1")
    for name in ("plan", "implement"):
        if name in document and not isinstance(document[name], dict):
            raise FlowError("devflow.config.json " + name + " must contain an object")
    result = {name: copy.deepcopy(document[name]) for name in ("reviewers", "max_reviews") if name in document}
    result.update(copy.deepcopy(document.get(workflow, {})))
    return result
