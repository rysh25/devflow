"""Session records and human answers shared by every workflow.

A session record says which run a host session is looking at (``run``) or
which start it is waiting for (``pending_start``), never both. Ownership itself
lives only in the run state's (``owner_host``, ``owner_session``). Records are written only while
the project's dispatch lock is held.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .errors import CaptureError, DevFlowError
from .hosts import HOSTS
from .runs import (RUN_ID, SESSION_ID, TERMINAL, RunStore, data_root, validate_event)
from .storage import atomic_json, no_symlinks, read_json

SCHEMA_VERSION = 3
MAX_SESSIONS = 10000


def sessions_dir(project: Path) -> Path:
    return data_root(project) / "sessions"


def session_path(project: Path, session_id: str) -> Path:
    if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
        raise DevFlowError("A valid host session ID is required")
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
    if not isinstance(record, dict) or set(record) != {"schema_version", "host", "session_id", "run", "pending_start"}:
        raise DevFlowError("Session record fields are not recognized")
    if type(record["schema_version"]) is not int or record["schema_version"] != SCHEMA_VERSION:
        raise DevFlowError("Unsupported session record schema_version")
    session = record["session_id"]
    if record["host"] not in HOSTS:
        raise DevFlowError("Session record has an invalid host")
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


@dataclass(frozen=True)
class SessionInfo:
    name: str
    format: str
    record: dict | None = None
    error: str | None = None
    schema_version: int | None = None
    host: str | None = None

def inspect_record(path: Path, workflows: Mapping[str, Any]) -> SessionInfo:
    try:
        no_symlinks(path)
        record = read_json(path, limit=8 * 1024 * 1024)
        session, version = record.get("session_id"), record.get("schema_version")
        if (not isinstance(session, str) or not SESSION_ID.fullmatch(session)
                or path.name != hashlib.sha256(session.encode()).hexdigest() + ".json"):
            raise DevFlowError("Session record file name does not match its session ID or session identity is invalid")
        if type(version) is int and 1 <= version < SCHEMA_VERSION:
            return SessionInfo(path.name, "unsupported", record, None, version, "claude")
        record = validate_record(record, path, workflows)
        return SessionInfo(path.name, "current", record, None, version, record["host"])
    except (DevFlowError, OSError) as exc:
        return SessionInfo(path.name, "invalid", error=str(exc))


def read_record(project: Path, session_id: str, workflows: Mapping[str, Any], host: str) -> dict | None:
    path = session_path(project, session_id)
    no_symlinks(path)
    if not path.exists():
        return None
    info = inspect_record(path, workflows)
    if info.format != "current":
        raise DevFlowError(info.error or "Unsupported session record schema_version")
    record = info.record
    if info.host != host:
        raise DevFlowError(f"Session record belongs to {info.host}, not {host}; host collision")
    if record["session_id"] != session_id:
        raise DevFlowError("Session record does not belong to this session")
    return record


def empty_record(session_id: str, host: str) -> dict:
    return {"schema_version": SCHEMA_VERSION, "host": host, "session_id": session_id, "run": None, "pending_start": None}


def write_record(project: Path, record: dict, workflows: Mapping[str, Any]) -> None:
    """Persist (or remove an empty) record; callers hold the dispatch lock."""
    path = session_path(project, record["session_id"])
    validate_record(record, path, workflows)
    no_symlinks(path)
    if path.exists():
        previous = inspect_record(path, workflows)
        if previous.format == "invalid":
            raise DevFlowError(previous.error)
        if previous.host != record["host"]:
            raise DevFlowError("Session host collision; refusing to overwrite or delete another host's record")
    if record["run"] is None and record["pending_start"] is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_json(path, record)


def scan_records(project: Path, workflows: Mapping[str, Any]) -> list[SessionInfo]:
    """Classify every session record without changing or converting it."""
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
            result.append(inspect_record(path, workflows))
        except (DevFlowError, OSError) as exc:
            result.append(SessionInfo(path.name, "invalid", error=str(exc)))
    return result


@dataclass(frozen=True)
class Binding:
    """What a session is looking at, judged against the run's real owner.

    ``supported`` is false for a run from an older DevFlow format: the session
    stays bound until its next prompt releases the binding.
    """
    session_id: str
    flow: str
    run_id: str
    state: dict
    owner: bool
    supported: bool = True
    schema_version: int | None = None

    @property
    def terminal(self) -> bool:
        return self.supported and self.state["status"] in TERMINAL


def binding(project: Path, record: dict | None, stores: Mapping[str, RunStore]) -> Binding | None:
    """The session's run, or None only when the session has no run at all."""
    if record is None or record["run"] is None:
        return None
    flow, run_id = record["run"]["flow"], record["run"]["run_id"]
    info = stores[flow].inspect(run_id)
    if info.format == "unsupported":
        return Binding(record["session_id"], flow, run_id, info.state, False, False, info.schema_version)
    if info.format != "current":
        raise DevFlowError(f"The bound {flow} run {run_id} cannot be read: {info.error or info.format}")
    return Binding(record["session_id"], flow, run_id, info.state,
                   (info.state["owner_host"], info.state["owner_session"]) == (record["host"], record["session_id"]),
                   True, info.schema_version)


def pending_applied(pending: dict, session_id: str, store: RunStore, host: str) -> bool | None:
    """True: the run exists and this session owns it; False: not created (or from an
    older DevFlow format, which is never continued); None: owned elsewhere."""
    run = store.root / pending["run_id"]
    no_symlinks(run)
    if not run.exists():
        return False
    info = store.inspect(pending["run_id"])
    if info.format == "unsupported":
        return False
    if info.format != "current":
        raise DevFlowError(f"The started run {pending['run_id']} cannot be read: {info.error or info.format}")
    return True if (info.state["owner_host"], info.state["owner_session"]) == (host, session_id) else None


def settle_pending_start(record: dict, stores: Mapping[str, RunStore]) -> dict:
    """Finish a start whose run exists and is owned here; otherwise drop it.

    The run side never changes here: an unapplied start simply did not happen.
    """
    pending = record["pending_start"]
    if not pending:
        return record
    if pending_applied(pending, record["session_id"], stores[pending["flow"]], record["host"]):
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


# Claude Code's question screen writes the human's choices and notes into these
# AskUserQuestion input fields before the tool runs.
SCREEN_FILLED_QUESTION_FIELDS = ("answers", "annotations")


def prefilled_question(data: object) -> bool:
    """True when the model itself filled the fields the question screen fills in.

    Only meaningful at PreToolUse: by PostToolUse the screen has added the
    human's answers and notes to these same fields.
    """
    return isinstance(data, dict) and any(data.get(field) for field in SCREEN_FILLED_QUESTION_FIELDS)


def question_matches(observation: dict, result: dict) -> bool:
    """The same native call and model-written question, before and after its UI."""
    for field in ("host", "session_id", "tool_use_id"):
        value = observation.get(field)
        if not isinstance(value, str) or not value or result.get(field) != value:
            return False
    before, after = observation.get("tool_input"), result.get("tool_input")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    # The question screen may add or reshape these fields after PreToolUse.
    # Compare everything the model wrote about the questions on both hosts.
    if ({key: value for key, value in before.items() if key not in SCREEN_FILLED_QUESTION_FIELDS}
            != {key: value for key, value in after.items() if key not in SCREEN_FILLED_QUESTION_FIELDS}):
        return False
    if observation["host"] == "codex":
        turn = observation.get("turn_id")
        return isinstance(turn, str) and bool(turn) and result.get("turn_id") == turn
    return observation["host"] == "claude"


def prefilled_question_events(events: list[dict]) -> list[str]:
    """IDs of the main conversation's AskUserQuestion calls whose answers the model filled in itself
    (a subagent's questions are not the interview's, as in ``approval_candidates``)."""
    return [event["event_id"] for event in events
            if event["kind"] == "observation" and event["payload"].get("agent_scope") == "main"
            and prefilled_question(event["payload"].get("tool_input"))]


def approval_candidates(state: dict, events: list[dict]) -> list[dict]:
    """Answers that may resolve the pending escalation, identical for status and resolve.

    Only successful AskUserQuestion results recorded after the escalation's
    boundary, from the current owner session, qualify. Ordinary prompts, the
    resume command and answers from before an ownership move never do.

    Every AskUserQuestion is recorded at PreToolUse as an observation, before
    the question screen adds the human's answers. An answer counts only when
    that observation exists and shows the model did not fill the answers in
    itself; when the hook could not record it, the answer is not evidence.
    """
    escalation = state.get("escalation")
    if not escalation:
        return []
    asked = {}
    for event in events:
        if event["kind"] == "observation":
            payload = event["payload"]
            if payload.get("agent_scope") != "main":
                continue
            key = (payload["host"], payload.get("session_id"), payload.get("tool_use_id"))
            asked.setdefault(key, []).append(payload)
    result = []
    for event in events[escalation["boundary"]:]:
        payload = event["payload"]
        if (event["kind"] != "question" or payload.get("is_error")
                or payload.get("session_id") != state["owner_session"]
                or payload["host"] != state["owner_host"]
                or payload.get("hook_event_name") != "PostToolUse" or payload.get("tool_name") != "AskUserQuestion"
                # Only answers in the main conversation count, not a subagent's.
                or payload.get("outcome") != "answered"
                or payload.get("agent_scope") != "main"):
            continue
        observations = asked.get((payload["host"], payload.get("session_id"), payload.get("tool_use_id")), [])
        if not observations or any(prefilled_question(before.get("tool_input"))
                                   or not question_matches(before, payload) for before in observations):
            continue
        try:
            text = human_answer_text(payload.get("tool_response"))
        except CaptureError:
            continue
        result.append({"event_id": event["event_id"], "kind": "question", "answer_text": text})
    return result
