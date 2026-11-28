"""Strict project configuration, frozen for the lifetime of a run."""
from __future__ import annotations
import copy
from ..common.errors import DevFlowError
from ..common.review import check_limit_auto, validate_limit
from ..common.settings import SettingsDocument, load_workflow_config, validate_reviewer_list, validate_reviewers
from .checks import validate_checks
from .snapshot import validate_patterns


#: The snapshot settings; the scope's size is never limited.
SNAPSHOT_FIELDS = frozenset({"mode", "include", "exclude", "context_paths", "max_packet_bytes"})


def _integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise DevFlowError(f"{name} must be an integer between {low} and {high}")


def validate_config(value: dict) -> dict:
    return _validate(value, match_reviewer_limit=True)


def validate_stage(value: dict) -> dict:
    """Every ``validate_config`` check but the reviewer-count/review-limit match,
    which applies to a document's effective value, not mid-layer combinations."""
    return _validate(value, match_reviewer_limit=False)


def _validate(value: dict, *, match_reviewer_limit: bool) -> dict:
    fields = {"schema_version", "max_reviews", "max_check_attempts", "max_plan_bytes", "snapshot",
              "reviewers", "checks", "review_limit_auto", "review_limit_auto_extra_reviews",
              "review_limit_auto_final"}
    if (not isinstance(value, dict) or set(value) != fields
            or type(value.get("schema_version")) is not int or value.get("schema_version") != 1):
        raise DevFlowError("Invalid configuration fields or schema_version; compare config/defaults.json")
    check_limit_auto(value, combination=match_reviewer_limit)
    validate_limit(value["max_reviews"])
    _integer(value["max_check_attempts"], 1, 10, "max_check_attempts")
    _integer(value["max_plan_bytes"], 1024, 8 * 1024 * 1024, "max_plan_bytes")
    snapshot = value["snapshot"]
    if not isinstance(snapshot, dict):
        raise DevFlowError("snapshot must be an object")
    unknown, missing = set(snapshot) - SNAPSHOT_FIELDS, SNAPSHOT_FIELDS - set(snapshot)
    if unknown or missing:
        raise DevFlowError("Invalid snapshot fields"
                           + (" (unknown: " + ", ".join(sorted(unknown)) + ")" if unknown else "")
                           + (" (missing: " + ", ".join(sorted(missing)) + ")" if missing else "")
                           + "; see the project settings in the DevFlow README")
    if not isinstance(snapshot["mode"], str) or snapshot["mode"] not in {"git", "files"}:
        raise DevFlowError("snapshot.mode must explicitly be git or files")
    for field in ("include", "exclude", "context_paths"):
        if not isinstance(snapshot[field], list) or any(not isinstance(x, str) or not x or "\x00" in x for x in snapshot[field]):
            raise DevFlowError(f"snapshot.{field} must be a string array")
    if not snapshot["include"]:
        raise DevFlowError("At least one snapshot include pattern is required")
    _integer(snapshot["max_packet_bytes"], 1024, 16 * 1024 * 1024, "snapshot.max_packet_bytes")
    if match_reviewer_limit:
        validate_reviewers(value["reviewers"], value["max_reviews"])
    else:
        validate_reviewer_list(value["reviewers"])
    validate_checks(value["checks"])
    if value["checks"] and not any(check.get("required", True) for check in value["checks"]):
        raise DevFlowError("Configure at least one required check; optional-only diagnostics are not a validation gate")
    return copy.deepcopy(value)


def load_config(documents: tuple[SettingsDocument, ...], workflow) -> dict:
    return load_workflow_config(documents, workflow, validate_config, validate_stage=validate_stage,
                                validate_host_layer=lambda layer: validate_patterns(layer.get("snapshot", {})))
