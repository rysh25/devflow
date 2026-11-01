"""Strict project configuration, frozen for the lifetime of a run."""
from __future__ import annotations
import copy
from pathlib import Path
import re
from ..common.errors import DevFlowError
from ..common.storage import read_json
from ..common.review import validate_limit
from ..common.settings import validate_reviewers, workflow_overlay
from .checks import validate_checks


def _integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise DevFlowError(f"{name} must be an integer between {low} and {high}")


def validate_config(value: dict) -> dict:
    fields = {"schema_version", "max_reviews", "max_check_attempts", "max_plan_bytes", "allow_dirty", "snapshot", "reviewers", "checks"}
    if not isinstance(value, dict) or set(value) != fields or type(value.get("schema_version")) is not int or value.get("schema_version") != 1:
        raise DevFlowError("Invalid configuration fields or schema_version; compare config/defaults.json")
    validate_limit(value["max_reviews"])
    _integer(value["max_check_attempts"], 1, 10, "max_check_attempts")
    _integer(value["max_plan_bytes"], 1024, 8 * 1024 * 1024, "max_plan_bytes")
    if type(value["allow_dirty"]) is not bool:
        raise DevFlowError("allow_dirty must be a boolean")
    snapshot = value["snapshot"]
    sf = {"mode", "include", "exclude", "context_paths", "max_files", "max_file_bytes", "max_total_bytes", "max_packet_bytes"}
    if not isinstance(snapshot, dict) or set(snapshot) != sf or snapshot["mode"] not in {"git", "files"}:
        raise DevFlowError("Invalid snapshot config; mode must explicitly be git or files")
    for field in ("include", "exclude", "context_paths"):
        if not isinstance(snapshot[field], list) or any(not isinstance(x, str) or not x or "\x00" in x for x in snapshot[field]):
            raise DevFlowError(f"snapshot.{field} must be a string array")
    if not snapshot["include"]:
        raise DevFlowError("At least one snapshot include pattern is required")
    for field, low, high in (("max_files", 1, 100000), ("max_file_bytes", 1024, 32*1024*1024), ("max_total_bytes", 1024, 64*1024*1024), ("max_packet_bytes", 1024, 16*1024*1024)):
        _integer(snapshot[field], low, high, "snapshot." + field)
    validate_reviewers(value["reviewers"], value["max_reviews"])
    validate_checks(value["checks"])
    if value["checks"] and not any(check.get("required", True) for check in value["checks"]):
        raise DevFlowError("Configure at least one required check; optional-only diagnostics are not a validation gate")
    return copy.deepcopy(value)


def load_config(project: Path, workflow) -> dict:
    value = read_json(workflow.resource("defaults.json"))
    overlay = workflow_overlay(project, workflow.name)
    if overlay:
        # Lists replace completely. Only the snapshot object merges named fields.
        if "snapshot" in overlay and isinstance(overlay["snapshot"], dict):
            overlay["snapshot"] = {**value["snapshot"], **overlay["snapshot"]}
        value.update(overlay)
    return validate_config(value)
