"""Session records and human answers shared by every workflow.

A session record says which run a Claude session is looking at (``run``) or
which start it is waiting for (``pending_start``), never both. Ownership itself
lives only in the run state's ``owner_session``. Records are written only while
the project's dispatch lock is held.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import json
from pathlib import Path
from typing import Any, Mapping

from .errors import CaptureError, DevFlowError
from .runs import (RUN_ID, SESSION_ID, TERMINAL, RunStore, data_root, validate_event)
from .storage import atomic_json, no_symlinks, read_json

SCHEMA_VERSION = 2
MAX_SESSIONS = 10000


def sessions_dir(project: Path) -> Path:
    return data_root(project) / "sessions"


def session_path(project: Path, session_id: str) -> Path:
    if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
        raise DevFlowError("A valid Claude session ID is required")
    return sessions_dir(project) / (hashlib.sha256(session_id.encode("utf-8")).hexdigest() + ".json")


def validate_record(record: object, path: Path, workflows: Mapping[str, Any]) -> dict:
    """Check every invariant; a record that fails is invalid as a whole."""
    try:
        return _validate_record(record, path, workflows)
    except (TypeError, KeyError, ValueError, AttributeError) as exc:
        raise DevFlowError(f"Malformed session record: {type(exc).__name__}: {exc}") from exc


def _known(flow: object, workflows: Mapping[str, Any]) -> bool:
    return isinstance(flow, str) and flow in workflows


def _validate_record(record: object, path: Path, workflows: Mapping[str, Any]) -> dict:
    if not isinstance(record, dict) or set(record) != {"schema_version", "session_id", "run", "pending_start"}:
        raise DevFlowError("Session record fields are not recognized")
    if type(record["schema_version"]) is not int or record["schema_version"] != SCHEMA_VERSION:
        raise DevFlowError("Unsupported session record schema_version")
    session = record["session_id"]
    if not isinstance(session, str) or not SESSION_ID.fullmatch(session):
        raise DevFlowError("Session record has an invalid session ID")
    if path.name != hashlib.sha256(session.encode("utf-8")).hexdigest() + ".json":
        raise DevFlowError("Session record file name does not match its session ID")
    run, pending = record["run"], record["pending_start"]
    if run is not None and pending is not None:
        raise DevFlowError("Session record cannot hold a run and a pending start together")
    if run is not None:
        if (not isinstance(run, dict) or set(run) != {"flow", "run_id"} or not _known(run["flow"], workflows)
                or not isinstance(run["run_id"], str) or not RUN_ID.fullmatch(run["run_id"])):
            raise DevFlowError("Session record has an invalid run binding")
    if pending is not None:
        if (not isinstance(pending, dict) or set(pending) != {"flow", "run_id", "args", "event", "created_at"}
                or not _known(pending["flow"], workflows)
                or not isinstance(pending["run_id"], str) or not RUN_ID.fullmatch(pending["run_id"])
                or not isinstance(pending["args"], str) or not isinstance(pending["created_at"], str)):
            raise DevFlowError("Session record has an invalid pending start")
        event = validate_event(pending["event"])
        if event["kind"] != "prompt":
            raise DevFlowError("A pending start must hold the start prompt")
    return record


def read_record(project: Path, session_id: str, workflows: Mapping[str, Any]) -> dict | None:
    path = session_path(project, session_id)
    no_symlinks(path)
    if not path.exists():
        return None
    record = validate_record(read_json(path, limit=8 * 1024 * 1024), path, workflows)
    if record["session_id"] != session_id:
        raise DevFlowError("Session record does not belong to this session")
    return record


def empty_record(session_id: str) -> dict:
    return {"schema_version": SCHEMA_VERSION, "session_id": session_id, "run": None, "pending_start": None}


def write_record(project: Path, record: dict, workflows: Mapping[str, Any]) -> None:
    """Persist (or remove an empty) record; callers hold the dispatch lock."""
    path = session_path(project, record["session_id"])
    validate_record(record, path, workflows)
    no_symlinks(path)
    if record["run"] is None and record["pending_start"] is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_json(path, record)


def scan_records(project: Path, workflows: Mapping[str, Any]) -> list[tuple[str, dict | None, str | None]]:
    """Every session record as (file name, record or None, error or None)."""
    directory = sessions_dir(project)
    no_symlinks(directory)
    if not directory.exists():
        return []
    result = []
    for index, path in enumerate(sorted(directory.iterdir())):
        if index >= MAX_SESSIONS:
            raise DevFlowError("Session history exceeds the bounded scan limit")
        if path.name.endswith(".lock") or path.name.startswith("."):
            continue
        if path.name in workflows and path.is_dir() and not path.is_symlink():
            continue  # The 0.1.x layout kept one directory per workflow; it is never read.
        try:
            no_symlinks(path)
            if not path.is_file():
                raise DevFlowError("Not a session record file")
            result.append((path.name, validate_record(read_json(path, limit=8 * 1024 * 1024), path, workflows), None))
        except (DevFlowError, OSError) as exc:
            result.append((path.name, None, str(exc)))
    return result


@dataclass(frozen=True)
class Binding:
    """What a session is looking at, judged against the run's real owner."""
    session_id: str
    flow: str
    run_id: str
    state: dict
    owner: bool

    @property
    def terminal(self) -> bool:
        return self.state["status"] in TERMINAL


def binding(project: Path, record: dict | None, stores: Mapping[str, RunStore]) -> Binding | None:
    if record is None or record["run"] is None:
        return None
    flow, run_id = record["run"]["flow"], record["run"]["run_id"]
    info = stores[flow].inspect(run_id)
    if info.format != "current":
        raise DevFlowError(f"The bound {flow} run {run_id} cannot be read: {info.error or info.format}")
    return Binding(record["session_id"], flow, run_id, info.state, info.state["owner_session"] == record["session_id"])


def pending_created(pending: dict, store: RunStore) -> bool:
    """Whether the pending start's run directory exists (its contents are judged elsewhere)."""
    return os.path.lexists(store.root / pending["run_id"])


def pending_applied(pending: dict, session_id: str, store: RunStore) -> bool | None:
    """True: the run exists and this session owns it; False: not created; None: owned elsewhere."""
    run = store.root / pending["run_id"]
    no_symlinks(run)
    if not run.exists():
        return False
    info = store.inspect(pending["run_id"])
    if info.format != "current":
        raise DevFlowError(f"The started run {pending['run_id']} cannot be read: {info.error or info.format}")
    return True if info.state["owner_session"] == session_id else None


def settle_pending_start(record: dict, stores: Mapping[str, RunStore]) -> dict:
    """Finish a start whose run exists and is owned here; otherwise drop it.

    The run side never changes here: an unapplied start simply did not happen.
    """
    pending = record["pending_start"]
    if not pending:
        return record
    if pending_applied(pending, record["session_id"], stores[pending["flow"]]):
        return dict(record, run={"flow": pending["flow"], "run_id": pending["run_id"]}, pending_start=None)
    return dict(record, pending_start=None)


# -- human answers --------------------------------------------------------------------------

def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)


def answer_content(value: Any) -> str:
    """Exact content of a successful AskUserQuestion result, or CaptureError."""
    # Preserve string values exactly. Structured values are encoded losslessly
    # as JSON, never paraphrased into an alleged verbatim quotation.
    if isinstance(value, str):
        if not value.strip():
            raise CaptureError("AskUserQuestion has no human answer")
        return value
    if isinstance(value, list):
        if not value:
            raise CaptureError("AskUserQuestion has no human answer")
        for block in value:
            if not isinstance(block, dict) or block.get("type") != "text" or not isinstance(block.get("text"), str):
                raise CaptureError("Unsupported non-text AskUserQuestion answer")
        if not any(block["text"].strip() for block in value):
            raise CaptureError("AskUserQuestion has no human answer")
        return _json(value)
    if isinstance(value, dict):
        if any(value.get(key) for key in ("is_error", "error", "cancelled", "canceled", "aborted")):
            raise CaptureError("AskUserQuestion failed or was cancelled")
        answers = value.get("answers")
        if not isinstance(answers, dict) or not answers:
            raise CaptureError("AskUserQuestion has no human answer")
        for answer in answers.values():
            if isinstance(answer, str) and answer.strip():
                continue
            if isinstance(answer, list) and answer and all(isinstance(x, str) and x.strip() for x in answer):
                continue
            raise CaptureError("AskUserQuestion has an empty or unsupported human answer")
        return _json(value)
    raise CaptureError("Unsupported AskUserQuestion answer type")


def human_answer_text(value: Any) -> str:
    """Validated exact answer representation used to resolve escalations."""
    answer_content(value)
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def approval_candidates(state: dict, events: list[dict]) -> list[dict]:
    """Answers that may resolve the pending escalation, identical for status and resolve.

    Only successful AskUserQuestion results recorded after the escalation's
    boundary, from the current owner session, qualify. Ordinary prompts, the
    resume command and answers from before an ownership move never do.
    """
    escalation = state.get("escalation")
    if not escalation:
        return []
    result = []
    for event in events[escalation["boundary"]:]:
        payload = event["payload"]
        if (event["kind"] != "question" or payload.get("is_error")
                or payload.get("session_id") != state["owner_session"]
                or payload.get("hook_event_name") != "PostToolUse" or payload.get("tool_name") != "AskUserQuestion"):
            continue
        try:
            text = human_answer_text(payload.get("tool_response"))
        except CaptureError:
            continue
        result.append({"event_id": event["event_id"], "kind": "question", "answer_text": text})
    return result
