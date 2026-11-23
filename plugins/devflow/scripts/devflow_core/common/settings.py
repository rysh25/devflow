"""Project settings and the reviewer rules shared by every workflow."""
from __future__ import annotations

import copy
from pathlib import Path
import re
import stat
from typing import NamedTuple

from .errors import DevFlowError
from .hosts import HOSTS, validate_host
from .review import TOTAL_REVIEW_CEILING
from .storage import read_json, writable, writable_text


REVIEWER_KINDS = frozenset({"codex", "opencode", "claude", "devin"})
REVIEWER_FIELDS = frozenset({"id", "kind", "command", "model", "effort", "timeout_seconds"})
REVIEWER_REQUIRED = REVIEWER_FIELDS
CONFIG_FILES = ("devflow.config.json", "devflow.config.local.json")
SHARED_SETTINGS = ("reviewers", "max_reviews", "auto_accept_at_review_limit")
HOSTS_KEY = "hosts"


def _host_config_names(host: str) -> tuple[str, str]:
    return f"devflow.config.{host}.json", f"devflow.config.{host}.local.json"


#: The former host-specific settings names, fixed to the three hosts that used
#: dedicated files. They are never read anymore, but the snapshot and the run
#: inspection keep protecting all of them and the distribution checks keep
#: protecting the local names.
HOST_CONFIG_FILES = frozenset(name for host in ("claude", "codex", "devin")
                            for name in _host_config_names(host))


class SettingsDocument(NamedTuple):
    name: str       # settings file name, for error locations
    data: dict      # the document's root content
    host: str       # the launch host whose ``hosts`` block applies


def _check_writable(document: dict) -> None:
    """Every string must be UTF-8 writable; reports the key path it knew, not the value."""
    def walk(value, path: tuple[str, ...]) -> None:
        if isinstance(value, str):
            if not writable(value):
                position = ".".join(path)
                raise DevFlowError((f"{position}: " if position else "")
                                   + "Settings must contain only strings that can be written as UTF-8")
        elif isinstance(value, dict):
            for key, item in value.items():
                walk(key, path)
                walk(item, (*path, key) if writable(key) else path)
        elif isinstance(value, list):
            for item in value:
                walk(item, path)
    walk(document, ())


def _error(name: str, position: str, message: str) -> DevFlowError:
    return DevFlowError(f"{name}: " + (f"{position}: " if position else "") + writable_text(message))


def _check_hosts(document: dict, name: str, workflows) -> None:
    """The structural rules of ``hosts``: checked for every host block, selected or not.

    Value errors of a host that did not launch DevFlow are reported when settings are
    resolved for that host; the shape (known hosts, objects, no ``schema_version``)
    is a property of the file itself and always applies.
    """
    if HOSTS_KEY not in document:
        return
    hosts = document[HOSTS_KEY]
    if not isinstance(hosts, dict):
        raise _error(name, HOSTS_KEY, "must contain an object")
    for host_name, block in hosts.items():
        position = f"{HOSTS_KEY}.{host_name}"
        if host_name not in HOSTS:
            raise _error(name, position, "unknown launch host (supported: " + ", ".join(HOSTS) + ")")
        if not isinstance(block, dict):
            raise _error(name, position, "must contain an object")
        if "schema_version" in block:
            raise _error(name, position, "schema_version is not allowed under hosts")
        unknown = set(block) - {*SHARED_SETTINGS, *workflows}
        if unknown:
            keys = writable_text(", ".join(sorted(unknown)))
            raise _error(name, position, f"unknown keys: {keys}")
        for workflow in workflows:
            if workflow not in block:
                continue
            flow = block[workflow]
            where = f"{position}.{workflow}"
            if not isinstance(flow, dict):
                raise _error(name, where, "must contain an object")
            if "schema_version" in flow:
                raise _error(name, where, "schema_version is not allowed under hosts")


def read_project_settings(project: Path, host: str, workflows) -> tuple[SettingsDocument, ...]:
    """Read the shared then the local settings file once each, shared first.

    Only the common file names are candidates; the former host-specific names are
    never searched. The caller supplies the workflow registry.
    """
    host = validate_host(host)
    allowed = {"schema_version", HOSTS_KEY, *SHARED_SETTINGS, *workflows}
    documents = []
    for name in CONFIG_FILES:
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
                raise _error(name, workflow, "must contain an object")
        _check_hosts(document, name, workflows)
        documents.append(SettingsDocument(name, document, host))
    return tuple(documents)


def _layers(document: SettingsDocument, workflow: str):
    """One document's overlays in precedence order, each with its location:
    root common, the workflow block, the launch host's common, the launch host's workflow block."""
    root = document.data
    yield "", {key: root[key] for key in SHARED_SETTINGS if key in root}
    yield workflow, root.get(workflow, {})
    block = (root.get(HOSTS_KEY) or {}).get(document.host) or {}
    position = f"{HOSTS_KEY}.{document.host}"
    yield position, {key: block[key] for key in SHARED_SETTINGS if key in block}
    yield f"{position}.{workflow}", block.get(workflow, {})


def _merge(value: dict, overlay: dict) -> dict:
    value = copy.deepcopy(value)
    for key, item in overlay.items():
        # Only these public objects merge by member; arrays always replace.
        if key in {"publish", "snapshot"} and isinstance(item, dict) and isinstance(value.get(key), dict):
            value[key].update(copy.deepcopy(item))
        else:
            value[key] = copy.deepcopy(item)
    return value


def load_workflow_config(documents: tuple[SettingsDocument, ...], workflow, validate, *,
                         validate_stage=None, validate_host_layer=None) -> dict:
    """Resolve the documents for the launch host without an overlay hiding an invalid setting.

    ``validate`` is the workflow's full validator. ``validate_stage`` applies the same
    checks while one document is being layered, except that the reviewer count is matched
    against ``max_reviews`` only on the document's effective value, once all its diffs are
    in — never on a mid-stage or defaults-supplemented combination. ``validate_host_layer``
    checks the launch host's own workflow layer (for example Implement's snapshot pattern
    syntax) before any later layer can replace it.
    """
    stage_validate = validate_stage or validate
    result = read_json(workflow.resource("defaults.json"))
    for document in documents:
        position = ""
        try:
            for position, layer in _layers(document, workflow.name):
                result = _merge(result, layer)
                stage_validate(result)
                if validate_host_layer is not None and position == f"{HOSTS_KEY}.{document.host}.{workflow.name}":
                    validate_host_layer(layer)
            position = ""
            validate(result)
        except DevFlowError as exc:
            raise _error(document.name, position, str(exc)) from exc
        except RecursionError as exc:
            raise DevFlowError(f"{document.name}: Settings are nested too deeply") from exc
    return validate(result)


def validate_reviewer_list(reviewers: object) -> list:
    """The reviewer array alone: a nonempty, ceiling-bounded list of valid, uniquely named entries.

    This holds even for an array a later layer replaces. Reviewer settings are trusted
    human configuration. DevFlow checks only what it needs to start each CLI and read its
    answer, never how the user runs it.
    """
    if not isinstance(reviewers, list) or not reviewers or len(reviewers) > TOTAL_REVIEW_CEILING:
        raise DevFlowError("Configure from one reviewer up to max_reviews; every reviewer must be able to approve")
    seen = set()
    for reviewer in reviewers:
        validate_reviewer(reviewer)
        if reviewer["id"] in seen:
            raise DevFlowError("Reviewer IDs must be unique lowercase names")
        seen.add(reviewer["id"])
    return copy.deepcopy(reviewers)


def validate_reviewers(reviewers: object, max_reviews: int) -> list:
    """One rule set for the shared reviewer list, used by every workflow: the array
    itself, then its count against the configured review limit."""
    result = validate_reviewer_list(reviewers)
    if len(result) > max_reviews:
        raise DevFlowError("Configure from one reviewer up to max_reviews; every reviewer must be able to approve")
    return result


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
