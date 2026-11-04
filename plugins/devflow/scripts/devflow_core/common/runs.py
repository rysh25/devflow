"""Run records for every workflow: one envelope and one transaction model.

The run state's ``owner_session`` is the only authority on which Claude session
owns a run. Runs appear by an atomic directory rename, so a run directory is
never half-created. Locked loads roll forward a committed transaction; reads
for display never change a file.
"""
from __future__ import annotations

import base64
import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
from uuid import uuid4

from .errors import DevFlowError
from .storage import (atomic_json, digest, file_lock, json_bytes, no_symlinks, now,
                      read_json, within, write_new)

SCHEMA_VERSION = 7
# Older run formats and the releases that wrote them; they are never continued.
# A format that was never released has no name and is shown generically.
RELEASES = {1: "0.1.x", 2: "0.2.x", 3: "0.3.x", 6: "0.4.x"}


def release_name(version: int) -> str:
    return RELEASES.get(version, f"an older version (run format {version})")
TERMINAL = frozenset({"completed", "exhausted", "cancelled"})
RUN_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[a-f0-9]{12}\Z")
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z")
EVENT_ID = re.compile(r"[a-f0-9]{32}\Z")
#: ``observation``: the AskUserQuestion input as Claude sent it, recorded at
#: PreToolUse (before the question screen adds answers); never an answer itself.
EVENT_KINDS = frozenset({"prompt", "question", "observation", "stop"})
MAX_EVENT_BYTES = 4 * 1024 * 1024
MAX_EVENTS = 5000
CREATING = ".creating-"
RUNNER_WAIT = 10.0
HOOK_WAIT = 5.0
ENVELOPE = frozenset({
    "schema_version", "flow", "run_id", "project", "status", "owner_session", "created_at",
    "updated_at", "state_revision", "artifacts", "event_ids", "inflight", "escalation",
})
ESCALATION = frozenset({"id", "reason", "questions", "boundary", "session_id", "return_status", "detail"})


def validate_run_id(run_id: object) -> str:
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise DevFlowError("Use exactly the run ID shown by DevFlow")
    return run_id


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:12]


def data_root(project: Path) -> Path:
    return Path(project) / ".devflow"


def ensure_data_root(project: Path) -> Path:
    """Create the private record directory; only starts and resumes call this."""
    root = data_root(project)
    no_symlinks(root)
    root.mkdir(mode=0o700, exist_ok=True)
    ignore = root / ".gitignore"
    no_symlinks(ignore)
    if not ignore.exists():
        # Runs can contain private requirements and conversation.
        write_new(ignore, b"# Private DevFlow records; never publish them.\n*\n")
    return root


def make_event(payload: dict, kind: str) -> dict:
    if kind not in EVENT_KINDS or not isinstance(payload, dict):
        raise DevFlowError("Unsupported human event")
    return {"event_id": uuid4().hex, "recorded_at": now(), "kind": kind, "payload": payload}


def event_bytes(event: dict) -> bytes:
    """Serialized event, refusing one larger than the per-event limit."""
    data = json_bytes(event)
    if len(data) > MAX_EVENT_BYTES:
        raise DevFlowError("A single human event exceeds 4 MiB; shorten the message")
    return data


def validate_event(value: object) -> dict:
    if (not isinstance(value, dict) or set(value) != {"event_id", "recorded_at", "kind", "payload"}
            or not isinstance(value["event_id"], str) or not EVENT_ID.fullmatch(value["event_id"])
            or not isinstance(value["recorded_at"], str) or value["kind"] not in EVENT_KINDS
            or not isinstance(value["payload"], dict)):
        raise DevFlowError("Invalid human event record")
    return value


def unsupported_message(run_id: str, version: int) -> str:
    """Why a run from an older format cannot continue (one wording everywhere)."""
    return f"Run {run_id} was started with DevFlow {release_name(version)}; this version cannot continue it"


def open_escalation(state: dict, identifier: str, reason: str, questions: list, return_status: str,
                    detail: dict | None = None) -> dict:
    """Pause for a human; only answers recorded after this point may resolve it."""
    value = {"id": identifier, "reason": reason, "questions": questions, "boundary": len(state["event_ids"]),
             "session_id": state["owner_session"], "return_status": return_status, "detail": detail or {}}
    state["escalation"] = value
    return value


@dataclass(frozen=True)
class RunInfo:
    flow: str
    run_id: str
    format: str  # current | unsupported | invalid
    state: dict | None
    error: str | None
    recovery_required: bool
    schema_version: int | None = None

    def public(self) -> dict:
        value = {"flow": self.flow, "run_id": self.run_id, "format": self.format,
                 "recovery_required": self.recovery_required}
        if self.error:
            value["error"] = self.error
        if self.format == "unsupported":
            value["status"] = self.state.get("status")
            value["project"] = self.state.get("project")
            value["message"] = (f"Started with DevFlow {release_name(self.schema_version)}; "
                                "this version cannot continue it")
        return value


class RunStore:
    """All file access for one workflow's runs in one project."""

    def __init__(self, project: Path, workflow, lock_wait: float = RUNNER_WAIT):
        self.project = Path(project)
        self.workflow = workflow
        self.flow = workflow.name
        self.lock_wait = lock_wait
        self.root = data_root(self.project) / "runs" / self.flow
        self._held: set[str] = set()

    # -- paths and locks ------------------------------------------------------------------
    def run_dir(self, run_id: str) -> Path:
        path = self.root / validate_run_id(run_id)
        no_symlinks(path)
        return path

    @contextmanager
    def lock(self, run_id: str, wait: float | None = None):
        if run_id in self._held:
            raise DevFlowError("Run lock is not reentrant")
        with file_lock(self.run_dir(run_id) / "state.lock", self.lock_wait if wait is None else wait):
            self._held.add(run_id)
            try:
                yield
            finally:
                self._held.discard(run_id)

    def holds(self, run_id: str) -> bool:
        return run_id in self._held

    def _require_lock(self, run_id: str) -> None:
        if run_id not in self._held:
            raise DevFlowError("Changing a run requires its lock")

    # -- validation -------------------------------------------------------------------------
    def validate(self, state: object, run_id: str) -> dict:
        """Check the envelope and the workflow's fields; malformed values become DevFlowError."""
        try:
            return self._validate(state, run_id)
        except (TypeError, KeyError, ValueError, AttributeError, IndexError) as exc:
            raise DevFlowError(f"Malformed run state: {type(exc).__name__}: {exc}") from exc

    def _validate(self, state: object, run_id: str) -> dict:
        workflow = self.workflow
        if not isinstance(state, dict):
            raise DevFlowError("Run state must be an object")
        version = state.get("schema_version")
        if type(version) is not int or version != SCHEMA_VERSION:
            raise DevFlowError("Unsupported run state schema_version")
        unknown = set(state) - ENVELOPE - workflow.state_fields
        missing = (ENVELOPE | workflow.state_fields) - set(state)
        if unknown or missing:
            raise DevFlowError("Run state fields do not match the workflow: " + ", ".join(sorted(unknown | missing)))
        if (state["flow"] != self.flow or state["run_id"] != run_id or state["project"] != str(self.project)
                or not isinstance(state["status"], str) or state["status"] not in workflow.statuses
                or not isinstance(state["owner_session"], str) or not SESSION_ID.fullmatch(state["owner_session"])
                or not isinstance(state["created_at"], str) or not isinstance(state["updated_at"], str)
                or type(state["state_revision"]) is not int or state["state_revision"] < 0
                or not isinstance(state["artifacts"], dict)
                or any(not isinstance(k, str) or not isinstance(v, str) for k, v in state["artifacts"].items())
                or not isinstance(state["event_ids"], list)
                or any(not isinstance(i, str) or not EVENT_ID.fullmatch(i) for i in state["event_ids"])
                or len(set(state["event_ids"])) != len(state["event_ids"])
                or len(state["event_ids"]) > MAX_EVENTS):
            raise DevFlowError("Invalid or mismatched run state: " + run_id)
        workflow.check_state(state)
        if (state["status"] in workflow.inflight_statuses) != (state["inflight"] is not None):
            raise DevFlowError("Run state has an in-flight status without its reservation (or the reverse)")
        if state["inflight"] is not None and not isinstance(state["inflight"], dict):
            raise DevFlowError("Invalid in-flight reservation")
        escalation = state["escalation"]
        if escalation is not None and (
                not isinstance(escalation, dict) or set(escalation) != ESCALATION
                or type(escalation["boundary"]) is not int
                or not 0 <= escalation["boundary"] <= len(state["event_ids"])
                or not isinstance(escalation["questions"], list)):
            raise DevFlowError("Invalid pending human escalation")
        return state

    # -- reading ----------------------------------------------------------------------------
    def _transaction(self, run_id: str) -> dict:
        journal = read_json(self.run_dir(run_id) / "transaction.json", limit=256 * 1024 * 1024)
        desired = journal.get("state")
        if (journal.get("schema_version") != SCHEMA_VERSION or not isinstance(desired, dict)
                or type(journal.get("base_revision")) is not int
                or desired.get("state_revision") != journal["base_revision"] + 1
                or not isinstance(journal.get("writes"), dict)):
            raise DevFlowError("Invalid pending transaction; preserve it for human recovery")
        self.validate(desired, run_id)
        return journal

    def _finish_transaction(self, run_id: str) -> None:
        """Replay a committed journal: only file and state writes, never a dispatch."""
        run = self.run_dir(run_id)
        path = run / "transaction.json"
        if not path.exists():
            return
        journal = self._transaction(run_id)
        desired = journal["state"]
        current = read_json(run / "state.json") if (run / "state.json").exists() else None
        if current is not None:
            revision = current.get("state_revision")
            if revision not in {journal["base_revision"], desired["state_revision"]}:
                raise DevFlowError("Pending transaction is stale; refusing to replace newer state")
            if revision == desired["state_revision"] and current != desired:
                raise DevFlowError("Conflicting committed transaction state")
        for relative, encoded in journal["writes"].items():
            target = within(run / relative, run)
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError) as exc:
                raise DevFlowError("Corrupt pending artifact bytes") from exc
            if digest(data) != desired["artifacts"].get(relative):
                raise DevFlowError("Pending artifact does not match its committed hash")
            if target.exists():
                if not target.is_file() or target.read_bytes() != data:
                    raise DevFlowError(f"Divergent artifact blocks recovery: {relative}; never overwrite it")
            else:
                write_new(target, data)
        atomic_json(run / "state.json", desired)
        path.unlink()

    def load(self, run_id: str) -> dict:
        """Locked read for a change: completes a leftover transaction first."""
        self._require_lock(run_id)
        self._finish_transaction(run_id)
        return self.validate(read_json(self.run_dir(run_id) / "state.json"), run_id)

    def read(self, run_id: str) -> dict:
        """Display read: never writes; a pending transaction is reported, not replayed."""
        run = self.run_dir(run_id)
        if (run / "state.json").exists():
            return self.validate(read_json(run / "state.json"), run_id)
        if (run / "transaction.json").exists():
            return self._transaction(run_id)["state"]
        raise DevFlowError("Run has no committed state; preserve it for human recovery: " + run_id)

    def inspect(self, run_id: str) -> RunInfo:
        """Classify a run without raising or changing anything."""
        recovery = False
        try:
            run = self.run_dir(run_id)
            if not run.is_dir():
                raise DevFlowError("Run path is not a directory")
            recovery = (run / "transaction.json").exists()
            if (run / "state.json").exists():
                raw = read_json(run / "state.json")
            elif recovery:
                raw = read_json(run / "transaction.json", limit=256 * 1024 * 1024).get("state")
            else:
                raise DevFlowError("Run has no committed state")
            if not isinstance(raw, dict):
                raise DevFlowError("Run state must be an object")
            version = raw.get("schema_version")
            if type(version) is int and 1 <= version < SCHEMA_VERSION:
                if (raw.get("run_id") == run_id and raw.get("flow") == self.flow
                        and raw.get("project") == str(self.project) and isinstance(raw.get("status"), str)):
                    return RunInfo(self.flow, run_id, "unsupported", raw, None, recovery, version)
                raise DevFlowError("Run state from an older DevFlow format has mismatched basic information")
            state = self.read(run_id)
            return RunInfo(self.flow, run_id, "current", state, None, recovery, SCHEMA_VERSION)
        except (DevFlowError, OSError, TypeError, KeyError, ValueError, AttributeError) as exc:
            return RunInfo(self.flow, run_id, "invalid", None, str(exc), recovery)

    def scan(self) -> tuple[list[RunInfo], list[str]]:
        """Every run directory (bounded) plus leftover ``.creating-*`` directories."""
        no_symlinks(self.root)
        if not self.root.exists():
            return [], []
        if not self.root.is_dir():
            raise DevFlowError("Workflow runs path is not a directory")
        runs, creating = [], []
        for index, path in enumerate(sorted(self.root.iterdir())):
            if index >= 10000:
                raise DevFlowError("Run history exceeds the bounded scan limit; archive it explicitly")
            if path.name.startswith(CREATING):
                creating.append(path.name)
            elif RUN_ID.fullmatch(path.name):
                runs.append(self.inspect(path.name))
            else:
                runs.append(RunInfo(self.flow, path.name, "invalid", None, "Not a run directory name", False))
        return runs, creating

    # -- writing ----------------------------------------------------------------------------
    def stage(self, state: dict, relative: str, data: bytes) -> Path:
        """Stage an immutable artifact for the next save; never overwrite one."""
        run = self.run_dir(state["run_id"])
        target = within(run / relative, run)
        if target.exists() and (not target.is_file() or target.read_bytes() != data):
            raise DevFlowError(f"Refusing to overwrite a different immutable artifact: {relative}")
        state.setdefault("_pending_writes", {})[relative] = base64.b64encode(data).decode("ascii")
        state["artifacts"][relative] = digest(data)
        return target

    def save(self, state: dict) -> None:
        run_id = state["run_id"]
        self._require_lock(run_id)
        self.workflow.before_save(self, state)
        writes = state.pop("_pending_writes", {})
        base_revision = state["state_revision"]
        desired = dict(state, updated_at=now(), state_revision=base_revision + 1)
        self.validate(desired, run_id)
        path = self.run_dir(run_id) / "transaction.json"
        if path.exists():
            raise DevFlowError("A prior transaction must recover before another change")
        atomic_json(path, {"schema_version": SCHEMA_VERSION, "base_revision": base_revision,
                           "state": desired, "writes": writes})
        self._finish_transaction(run_id)
        state.clear()
        state.update(desired)

    def verify_artifacts(self, state: dict) -> None:
        run = self.run_dir(state["run_id"])
        for relative, expected in state["artifacts"].items():
            path = within(run / relative, run)
            if not path.is_file() or digest(path.read_bytes()) != expected:
                raise DevFlowError(f"Immutable artifact changed or is missing: {relative}; stop and investigate")

    def create(self, fields: dict, artifacts: dict[str, bytes], *, run_id: str, owner: str, first_event: dict,
               directories: tuple[str, ...] = ()) -> dict:
        """Build a complete run in a private directory, then publish it by rename."""
        ensure_data_root(self.project)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        final = self.run_dir(run_id)
        if final.exists():
            raise DevFlowError("Run ID already exists: " + run_id)
        validate_event(first_event)
        event_bytes(first_event)
        staging = self.root / (CREATING + run_id)
        no_symlinks(staging)
        staging.mkdir(mode=0o700)
        try:
            for name in ("draft", "events", *directories):
                (staging / name).mkdir(mode=0o700, exist_ok=True)
            files = dict(artifacts)
            files[f"events/{first_event['event_id']}.json"] = event_bytes(first_event)
            hashes = {}
            for relative, data in files.items():
                target = within(staging / relative, staging)
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                write_new(target, data)
                hashes[relative] = digest(data)
            created = now()
            state = {**fields, "schema_version": SCHEMA_VERSION, "flow": self.flow, "run_id": run_id,
                     "project": str(self.project), "owner_session": owner, "created_at": created,
                     "updated_at": created, "state_revision": 0, "artifacts": hashes,
                     "event_ids": [first_event["event_id"]], "inflight": None,
                     "escalation": fields.get("escalation")}
            if state["escalation"] is not None:
                state["escalation"] = dict(state["escalation"], boundary=1, session_id=owner)
            self.validate(state, run_id)
            atomic_json(staging / "state.json", state)
            os.rename(staging, final)
        except BaseException:
            if staging.exists() and not final.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise
        return state

    # -- external processes -----------------------------------------------------------------
    def reserve(self, state: dict, status: str, kind: str, number: int, target: str | None,
                reviewer_id: str | None = None) -> dict:
        """Record an in-flight external process before it starts (lock held).

        The attempt is consumed even if the process never reports back.
        """
        if status not in self.workflow.inflight_statuses:
            raise DevFlowError("Not an in-flight status: " + status)
        reservation = {"kind": kind, "number": number, "reviewer_id": reviewer_id, "target": target,
                       "started_at": now()}
        state.update(status=status, inflight=reservation)
        self.save(state)
        return reservation

    def claim(self, run_id: str, reservation: dict, status: str, outcome: dict) -> dict:
        """Reload after the process and take the result only for a still-current reservation.

        Returns the state with ``inflight`` cleared (the caller finalizes and
        saves). A result for an outdated reservation, after cancel or recover,
        is kept under ``late/`` and the run is left unchanged.
        """
        state = self.load(run_id)
        if state["status"] != status or state["inflight"] != reservation:
            late = self.run_dir(run_id) / "late" / f"{reservation['kind']}-{reservation['number']:03}-{uuid4().hex}.json"
            write_new(late, json_bytes({"reservation": reservation, **outcome}))
            raise DevFlowError(f"A stale {reservation['kind']} finished after cancel or recover; "
                               "the current run state was left unchanged")
        state["inflight"] = None
        return state

    def recover(self, run_id: str, expect=None) -> dict:
        """Human-confirmed end of an interrupted process; the attempt stays consumed.

        ``expect(state)`` may refuse (raise) after the state is read under the lock.
        """
        with self.lock(run_id):
            state = self.load(run_id)
            if state["status"] not in self.workflow.inflight_statuses:
                raise DevFlowError("Recovery applies only to an interrupted check or review")
            if expect is not None:
                expect(state)
            self.workflow.on_recover(self, state)
            state["inflight"] = None
            self.save(state)
            return state

    # -- human events -----------------------------------------------------------------------
    def events(self, state: dict) -> list[dict]:
        """Every event of the run, including ones staged in the current transaction."""
        run = self.run_dir(state["run_id"])
        pending = state.get("_pending_writes", {})
        result = []
        for identifier in state["event_ids"]:
            relative = f"events/{identifier}.json"
            if relative in pending:
                value = validate_event(json.loads(base64.b64decode(pending[relative])))
            else:
                value = validate_event(read_json(run / relative, limit=MAX_EVENT_BYTES + 4096))
            if value["event_id"] != identifier:
                raise DevFlowError("Human event record does not match its ID")
            result.append(value)
        return result

    def append_event(self, run_id: str, payload: dict, kind: str) -> str | None:
        """Record one hook payload from the owning session; terminal runs are frozen.

        ``observation`` is every AskUserQuestion as Claude sent it at PreToolUse
        (whether or not the model filled in the answers); ``question`` is its
        PostToolUse result with the human's answers.
        """
        with self.lock(run_id):
            state = self.load(run_id)
            if state["status"] in TERMINAL:
                return None
            if payload.get("session_id") != state["owner_session"]:
                raise DevFlowError("This run is owned by another session; stale-session events cannot become human evidence")
            if kind in ("question", "observation"):
                use_id = payload.get("tool_use_id")
                if (not isinstance(use_id, str) or not use_id.strip() or len(use_id) > 512
                        or not isinstance(payload.get("tool_input"), dict)
                        or (kind == "question" and "tool_response" not in payload)):
                    raise DevFlowError("Question capture requires its actual tool identity, input and result"
                                       if kind == "question" else
                                       "Observation capture requires its actual tool identity and input")
                for old in self.events(state):
                    earlier = old["payload"]
                    if (old["kind"] == kind and earlier.get("session_id") == payload["session_id"]
                            and earlier.get("tool_use_id") == use_id):
                        if (earlier.get("tool_input") != payload["tool_input"]
                                or earlier.get("tool_response") != payload.get("tool_response")
                                or bool(earlier.get("is_error")) != bool(payload.get("is_error"))):
                            raise DevFlowError(f"A captured {kind} ID was replayed with different content")
                        return old["event_id"]  # A hook retry, not a new event.
            if len(state["event_ids"]) >= MAX_EVENTS:
                raise DevFlowError("This run reached its human event limit; start a new run")
            event = make_event(payload, kind)
            data = event_bytes(event)
            self.stage(state, f"events/{event['event_id']}.json", data)
            state["event_ids"].append(event["event_id"])
            self.save(state)
            return event["event_id"]

    def transfer_refusal(self, state: dict, session: str) -> str | None:
        """Why ownership cannot move now (checked on a freshly loaded state)."""
        if state["status"] in self.workflow.inflight_statuses or state["inflight"] is not None:
            return "A process may still be running; a human must confirm it stopped and use recover"
        return self.workflow.can_transfer(state, session)

    def transfer(self, state: dict, session: str, payload: dict) -> dict:
        """Move ownership and record the resume request in one commit (lock held)."""
        self._require_lock(state["run_id"])
        reason = self.transfer_refusal(state, session)
        if reason:
            raise DevFlowError(reason)
        previous = state["owner_session"]
        state["owner_session"] = session
        event = make_event(payload, "prompt")
        data = event_bytes(event)
        if len(state["event_ids"]) >= MAX_EVENTS:
            raise DevFlowError("The resume request cannot be recorded within the event limits")
        self.stage(state, f"events/{event['event_id']}.json", data)
        state["event_ids"].append(event["event_id"])
        if previous != session and state["escalation"] is not None:
            # Answers given before the move, and the resume command itself,
            # can no longer resolve the pending decision.
            state["escalation"] = dict(state["escalation"], boundary=len(state["event_ids"]), session_id=session)
        self.workflow.on_transfer(state)
        self.save(state)
        return state
