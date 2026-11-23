"""Project settings and the reviewer rules shared by every workflow."""
from __future__ import annotations

import copy
from pathlib import Path
import re
import stat
from typing import NamedTuple

from .errors import DevFlowError
from .hosts import HOSTS, validate_host
from .storage import read_json, writable, writable_text


REVIEWER_KINDS = frozenset({"codex", "opencode", "claude", "devin"})
REVIEWER_FIELDS = frozenset({"id", "kind", "command", "model", "effort", "timeout_seconds"})
REVIEWER_REQUIRED = REVIEWER_FIELDS
CONFIG_FILES = ("devflow.config.json", "devflow.config.local.json")


def _host_config_names(host: str) -> tuple[str, str]:
    return f"devflow.config.{host}.json", f"devflow.config.{host}.local.json"


HOST_CONFIG_FILES = frozenset(name for host in HOSTS for name in _host_config_names(host))


class SettingsDocument(NamedTuple):
    name: str
    data: dict
    dedicated: bool


def _check_writable(value) -> None:
    if isinstance(value, str):
        if not writable(value):
            raise DevFlowError("Settings must contain only strings that can be written as UTF-8")
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_writable(key)
            _check_writable(item)
    elif isinstance(value, list):
        for item in value:
            _check_writable(item)


def read_project_settings(project: Path, host: str, workflows) -> tuple[SettingsDocument, ...]:
    """Select and validate one document per layer, shared first. The caller supplies the workflow registry."""
    allowed = {"schema_version", "reviewers", "max_reviews", *workflows}
    documents = []
    for common, dedicated in zip(CONFIG_FILES, _host_config_names(validate_host(host))):
        for name in (dedicated, common):
            path = Path(project) / name
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise DevFlowError(f"{name}: Cannot inspect settings: {writable_text(str(exc))}") from exc
            try:
                if (not stat.S_ISREG(info.st_mode)
                        or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                    raise DevFlowError("Settings must be a regular file without symlinks or reparse points")
                document = read_json(path)
                if name == dedicated:
                    _check_writable(document)
                version = document.get("schema_version", 1)
                if type(version) is not int or version != 1:
                    raise DevFlowError("Unsupported schema_version; expected 1")
            except DevFlowError as exc:
                raise DevFlowError(f"{name}: {writable_text(str(exc))}") from exc
            except RecursionError as exc:
                raise DevFlowError(f"{name}: Settings are nested too deeply") from exc
            unknown = set(document) - allowed
            if unknown:
                keys = writable_text(", ".join(sorted(unknown)))
                raise DevFlowError(f"Unknown {name} keys: {keys}")
            for workflow in workflows:
                if workflow in document and not isinstance(document[workflow], dict):
                    raise DevFlowError(f"{name} {workflow} must contain an object")
            documents.append(SettingsDocument(name, document, name == dedicated))
            break
    return tuple(documents)


def _layers(document: dict, workflow: str):
    return ({key: document[key] for key in ("reviewers", "max_reviews") if key in document},
            document.get(workflow, {}))


def _merge(value: dict, overlay: dict) -> dict:
    value = copy.deepcopy(value)
    for key, item in overlay.items():
        # Only these public objects merge by member; arrays always replace.
        if key in {"publish", "snapshot"} and isinstance(item, dict) and isinstance(value.get(key), dict):
            value[key].update(copy.deepcopy(item))
        else:
            value[key] = copy.deepcopy(item)
    return value


def load_workflow_config(documents: tuple[SettingsDocument, ...], workflow, validate, *, validate_dedicated=None) -> dict:
    """Resolve the selected documents without allowing an overlay to hide an invalid setting."""
    defaults = read_json(workflow.resource("defaults.json"))
    result = defaults
    for name, document, dedicated in documents:
        try:
            standalone = defaults
            for layer in _layers(document, workflow.name):
                standalone = _merge(standalone, layer)
                validate(standalone)
                result = _merge(result, layer)
            if dedicated and validate_dedicated is not None:
                validate_dedicated(standalone)
            validate(result)
        except DevFlowError as exc:
            raise DevFlowError(f"{name}: {writable_text(str(exc))}") from exc
        except RecursionError as exc:
            raise DevFlowError(f"{name}: Settings are nested too deeply") from exc
    return validate(result)


def validate_reviewers(reviewers: object, max_reviews: int) -> list:
    """One rule set for the shared reviewer list, used by every workflow.

    Reviewer settings are trusted human configuration. DevFlow checks only what
    it needs to start each CLI and read its answer, never how the user runs it.
    """
    if not isinstance(reviewers, list) or not reviewers or len(reviewers) > max_reviews:
        raise DevFlowError("Configure from one reviewer up to max_reviews; every reviewer must be able to approve")
    seen = set()
    for reviewer in reviewers:
        validate_reviewer(reviewer)
        if reviewer["id"] in seen:
            raise DevFlowError("Reviewer IDs must be unique lowercase names")
        seen.add(reviewer["id"])
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
    if not isinstance(reviewer["kind"], str) or reviewer["kind"] not in REVIEWER_KINDS:
        raise DevFlowError("Unsupported reviewer kind; implement an explicit adapter first")
    command = reviewer["command"]
    if (not isinstance(command, list) or not command or len(command) > 128
            or any(not isinstance(x, str) or not x or "\x00" in x for x in command)):
        raise DevFlowError("Reviewer command must be a nonempty argv array of strings")
    # DevFlow writes these into its own arguments (``--model M``, Codex's
    # ``model_reasoning_effort="E"`` and OpenCode's ``M#E``), so they must not start
    # with "-" (an option), or hold spaces, double quotes, backslashes or "#" (the separator).
    for field in ("model", "effort"):
        if (not isinstance(reviewer[field], str) or any(ch.isspace() for ch in reviewer[field])
                or not re.fullmatch(r'(?!-)[^\x00-\x20\x7f"\\#]+', reviewer[field])):
            raise DevFlowError(f"Invalid reviewer {field}")
    timeout = reviewer["timeout_seconds"]
    if type(timeout) is not int or not 5 <= timeout <= 3600:
        raise DevFlowError("timeout_seconds must be an integer between 5 and 3600")
    return reviewer
