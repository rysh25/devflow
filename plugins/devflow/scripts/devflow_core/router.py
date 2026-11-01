"""Session routing for every workflow, without per-workflow branches.

Hooks are session-scoped: a session without a record is inert and never
creates files. Starts and resumes additionally enforce one unfinished run per
project. Ownership is decided only by a run's ``owner_session``. This module
never grants tool permissions or dispatches a model itself.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from . import __version__
from .common import guard, sessions
from .common.errors import DevFlowError
from .common.runs import (HOOK_WAIT, RUNNER_WAIT, TERMINAL, RunStore, data_root, ensure_data_root,
                          event_bytes, make_event, new_run_id, validate_run_id)
from .common.settings import validate_document
from .common.storage import file_lock, now
from .workflows import WORKFLOWS

RESUME = "resume"


def project_root(start: Path) -> Path:
    current = Path(start).resolve()
    if not current.is_dir():
        raise DevFlowError("Project directory does not exist")
    for directory in (current, *current.parents):
        if any((directory / marker).exists() for marker in (".devflow", "devflow.config.json", ".git")):
            return directory
    return current


def stores(project: Path, wait: float = RUNNER_WAIT) -> dict[str, RunStore]:
    return {name: RunStore(project, workflow, wait) for name, workflow in WORKFLOWS.items()}


def public_command(prompt: str) -> tuple[str | None, str]:
    for name in (*WORKFLOWS, RESUME):
        command = "/devflow:" + name
        if prompt == command:
            return name, ""
        if prompt.startswith(command) and prompt[len(command):len(command) + 1].isspace():
            return name, prompt[len(command):].strip()
    return None, ""


@contextmanager
def dispatch_lock(project: Path, wait: float):
    """Serializes every session-record change and every start/resume decision."""
    root = ensure_data_root(project)
    with file_lock(root / "dispatch.lock", wait):
        yield


def resolve_run(project: Path, run_id: str, all_stores: dict[str, RunStore]):
    """The single workflow that has this run directory, with its classification."""
    validate_run_id(run_id)
    found = []
    for flow, store in all_stores.items():
        path = store.root / run_id
        if path.exists() or path.is_symlink():
            found.append((flow, store.inspect(run_id)))
    if len(found) != 1:
        raise DevFlowError("Run ID is missing or ambiguous across workflows; do not guess a replacement")
    return found[0]


def assert_available(project: Path, session: str, all_stores: dict[str, RunStore], selected=None) -> None:
    """One unfinished run per project; damaged or interrupted records stop starts."""
    for flow, store in all_stores.items():
        runs, _ = store.scan()
        for info in runs:
            if selected == (flow, info.run_id):
                continue
            if info.format == "invalid":
                raise DevFlowError(f"Run {flow}/{info.run_id} cannot be read ({info.error}); "
                                   "preserve it for human recovery before starting another")
            if info.recovery_required:
                raise DevFlowError(f"Run {flow}/{info.run_id} has an interrupted transaction; complete it with "
                                   f"/devflow:resume {info.run_id} or cancel before starting another")
            if info.format == "current" and info.state["status"] not in TERMINAL:
                raise DevFlowError(f"Another unfinished {flow} run exists ({info.run_id}); resume or cancel it first")
    for name, record, error in sessions.scan_records(project, WORKFLOWS):
        if error:
            raise DevFlowError(f"Session record {name} is invalid ({error}); a human must move it out of "
                               ".devflow/sessions/ before another start")
        pending = record["pending_start"]
        if record["session_id"] == session or not pending:
            continue
        if not sessions.pending_created(pending, all_stores[pending["flow"]]):
            raise DevFlowError(f"Another session has a pending {pending['flow']} start (session record {name}); "
                               "finish it in that session, or have a human move the record away")


def _create(project: Path, session: str, pending: dict, all_stores: dict[str, RunStore]) -> dict:
    workflow = WORKFLOWS[pending["flow"]]
    fields, artifacts = workflow.build_start(project, pending)
    return all_stores[pending["flow"]].create(fields, artifacts, run_id=pending["run_id"], owner=session,
                                              first_event=pending["event"])


def _bound(record: dict, flow: str, run_id: str) -> dict:
    return dict(record, run={"flow": flow, "run_id": run_id}, pending_start=None)


def _settle(project: Path, record: dict, all_stores: dict[str, RunStore]) -> dict:
    """Finish an applied start, drop an unapplied one, and release a finished run.

    Only an actual next prompt reaches this point, so a finished run stays
    guarded for the rest of the turn that finished it.
    """
    record = sessions.settle_pending_start(record, all_stores)
    if record["run"]:
        info = all_stores[record["run"]["flow"]].inspect(record["run"]["run_id"])
        if info.format == "current" and info.state["status"] in TERMINAL:
            record = dict(record, run=None)
    return record


def _context(event: str, flow: str, state: dict, store: RunStore, plugin_root: Path, event_id: str | None) -> dict:
    run_dir = store.run_dir(state["run_id"])
    return guard.context(event, guard.status_context(WORKFLOWS[flow], state, run_dir, plugin_root, event_id))


def _start(project: Path, session: str, flow: str, argument: str, payload: dict, record: dict,
           all_stores: dict[str, RunStore], plugin_root: Path) -> dict:
    workflow = WORKFLOWS[flow]
    current = sessions.binding(project, record, all_stores)
    if current is not None and not current.terminal:
        raise DevFlowError("Finish or cancel this session's active workflow before starting another")
    args = workflow.normalize_args(argument, project)
    validate_document(project, WORKFLOWS)
    assert_available(project, session, all_stores)
    pending = {"flow": flow, "run_id": new_run_id(), "args": args, "event": make_event(payload, "prompt"),
               "created_at": now()}
    record = dict(record, run=None, pending_start=pending)
    sessions.write_record(project, record, WORKFLOWS)
    if workflow.start_option is not None:
        return guard.context("UserPromptSubmit", (
            f"DevFlow {flow} start is pending. Run the {flow} start action with {workflow.start_option} naming "
            f"exactly this input; until it starts, only diagnostics and read-only tools are available. "
            f"Runner: {guard.runner_path(plugin_root)}. Pending input: {args}"))
    try:
        state = _create(project, session, pending, all_stores)
    except Exception:
        # No run exists after a failed creation; drop the pending start so it
        # cannot block other sessions. A killed process is settled next prompt.
        sessions.write_record(project, dict(record, pending_start=None), WORKFLOWS)
        raise
    sessions.write_record(project, _bound(record, flow, state["run_id"]), WORKFLOWS)
    return _context("UserPromptSubmit", flow, state, all_stores[flow], plugin_root, pending["event"]["event_id"])


def _resume(project: Path, session: str, argument: str, payload: dict, record: dict,
            all_stores: dict[str, RunStore], plugin_root: Path) -> dict:
    run_id = validate_run_id(argument)
    flow, info = resolve_run(project, run_id, all_stores)
    if info.format == "unsupported":
        raise DevFlowError(f"Run {run_id} was started with DevFlow 0.1.x; this version cannot continue it")
    current = sessions.binding(project, record, all_stores)
    if current is not None and (current.flow, current.run_id) != (flow, run_id) and not current.terminal:
        raise DevFlowError("Finish or cancel this session's active workflow before resuming another")
    store = all_stores[flow]
    with store.lock(run_id):
        state = store.load(run_id)  # Completes an interrupted transaction first.
        reason = store.transfer_refusal(state, session)
        if reason:
            raise DevFlowError(reason)
        assert_available(project, session, all_stores, selected=(flow, run_id))
        # The binding is written first: a stop before the ownership change
        # leaves only a non-owning binding, which asks for /devflow:resume again.
        sessions.write_record(project, _bound(record, flow, run_id), WORKFLOWS)
        state = store.transfer(state, session, payload)
    return _context("UserPromptSubmit", flow, state, store, plugin_root, state["event_ids"][-1])


def runner_start(project: Path, session: str | None, flow: str, raw: str) -> dict:
    """``FLOW start`` from the runner: creates exactly the run the user asked for."""
    if not session:
        raise DevFlowError("start requires the actual current Claude session ID")
    workflow = WORKFLOWS[flow]
    all_stores = stores(project)
    with dispatch_lock(project, RUNNER_WAIT):
        record = sessions.read_record(project, session, WORKFLOWS)
        pending = record and record["pending_start"]
        if not pending or pending["flow"] != flow:
            raise DevFlowError(f"Start only from an explicit /devflow:{flow} command in this session")
        validate_document(project, WORKFLOWS)
        if workflow.normalize_args(raw, project) != pending["args"]:
            raise DevFlowError(f"The {workflow.start_option} value does not match the user's explicit start command")
        assert_available(project, session, all_stores)
        state = _create(project, session, pending, all_stores)
        sessions.write_record(project, _bound(record, flow, state["run_id"]), WORKFLOWS)
    return workflow.engine(project).status(state["run_id"])


def selected_run(project: Path, session: str | None, flow: str, explicit: str | None) -> str | None:
    """The run a workflow command acts on: an explicit ``--run`` or the session's binding."""
    if explicit:
        selected_flow, info = resolve_run(project, explicit, stores(project))
        if selected_flow != flow:
            raise DevFlowError("The selected run belongs to another workflow")
        if info.format == "unsupported":
            raise DevFlowError(f"Run {explicit} was started with DevFlow 0.1.x; this version cannot continue it")
        return explicit
    if not session:
        return None
    record = sessions.read_record(project, session, WORKFLOWS)
    if record and record["run"] and record["run"]["flow"] == flow:
        return record["run"]["run_id"]
    return None


def handle_hook(payload: dict, plugin_root: Path) -> dict | None:
    if not isinstance(payload, dict):
        raise DevFlowError("Hook input must be a JSON object")
    event = payload.get("hook_event_name")
    if event not in {"UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"}:
        return None
    cwd, session = payload.get("cwd"), payload.get("session_id")
    if not isinstance(cwd, str) or not cwd or not isinstance(session, str) or not sessions.SESSION_ID.fullmatch(session):
        raise DevFlowError("Hook requires project cwd and a valid Claude session ID")
    project = project_root(Path(cwd))
    all_stores = stores(project, HOOK_WAIT)
    record = sessions.read_record(project, session, WORKFLOWS)
    if event == "UserPromptSubmit":
        prompt = payload.get("prompt")
        if not isinstance(prompt, str):
            raise DevFlowError("UserPromptSubmit requires the original prompt")
        command, argument = public_command(prompt)
        if record is None and command is None:
            return None
        # Reject malformed commands before anything is written.
        if command == RESUME:
            validate_run_id(argument)
        elif command in WORKFLOWS:
            WORKFLOWS[command].normalize_args(argument, project)
            event_bytes(make_event(payload, "prompt"))
        with dispatch_lock(project, HOOK_WAIT):
            record = sessions.read_record(project, session, WORKFLOWS) or sessions.empty_record(session)
            record = _settle(project, record, all_stores)
            if command == RESUME:
                return _resume(project, session, argument, payload, record, all_stores, plugin_root)
            if command in WORKFLOWS:
                return _start(project, session, command, argument, payload, record, all_stores, plugin_root)
            sessions.write_record(project, record, WORKFLOWS)
            current = sessions.binding(project, record, all_stores)
            if current is None:
                return None
            if not current.owner:
                raise DevFlowError("This run is owned by another session, or its resume did not complete; "
                                   f"send /devflow:resume {current.run_id}")
            store = all_stores[current.flow]
            event_id = store.append_event(current.run_id, payload, "prompt")
            return _context(event, current.flow, store.read(current.run_id), store, plugin_root, event_id)
    if record is None:
        return None
    pending = record["pending_start"]
    if pending:
        if event == "PreToolUse":
            return guard.pending_decision(payload, project, plugin_root, pending, WORKFLOWS[pending["flow"]], WORKFLOWS)
        return None  # No run exists yet, so nothing is recorded as evidence.
    current = sessions.binding(project, record, all_stores)
    if current is None:
        return None
    if not current.owner:
        if event == "PreToolUse":
            return guard.deny("This session no longer owns its run; explicitly resume before using workflow tools")
        raise DevFlowError("A stale session cannot contribute human events to a transferred run")
    workflow, store = WORKFLOWS[current.flow], all_stores[current.flow]
    if event == "PreToolUse":
        return guard.bound_decision(payload, project, plugin_root, current.state,
                                    store.run_dir(current.run_id), workflow, WORKFLOWS)
    if event == "PostToolUse":
        if payload.get("tool_name") != "AskUserQuestion":
            return None
        event_id = store.append_event(current.run_id, payload, "question")
        if event_id is None:
            return None
        return guard.context(event, f"DevFlow recorded this human question/answer as event {event_id}. "
                                    "Use its exact response and event ID if resolving a human escalation.")
    store.append_event(current.run_id, payload, "stop")
    return None


def _overview(project: Path, all_stores: dict[str, RunStore], session: str | None = None) -> dict:
    runs, creating = [], []
    for flow, store in all_stores.items():
        infos, leftovers = store.scan()
        creating.extend(f"{flow}/{name}" for name in leftovers)
        for info in infos:
            item = info.public()
            if info.format == "current":
                item.update(status=info.state["status"], updated_at=info.state["updated_at"],
                            **WORKFLOWS[flow].summary(info.state))
            runs.append(item)
    blocking, invalid = [], []
    for name, record, error in sessions.scan_records(project, WORKFLOWS):
        if error:
            invalid.append({"record": name, "error": error})
            continue
        pending = record["pending_start"]
        if record["session_id"] == session or not pending:
            continue
        try:
            created = sessions.pending_created(pending, all_stores[pending["flow"]])
        except (DevFlowError, OSError) as exc:
            invalid.append({"record": name, "error": str(exc)})
            continue
        if not created:
            blocking.append({"record": name, "flow": pending["flow"], "args": pending["args"],
                             "created_at": pending["created_at"]})
    return {"runs": runs, "sessions": {"pending_starts": blocking, "invalid_records": invalid},
            "incomplete_creations": creating}


def status(project: Path, session: str | None, run_id: str | None) -> dict:
    """Read-only: never creates, repairs or releases anything."""
    all_stores = stores(project)
    if run_id:
        flow, info = resolve_run(project, run_id, all_stores)
        if info.format == "unsupported":
            return {"plugin": "devflow", **info.public()}
        if info.format == "invalid":
            raise DevFlowError(f"Run {run_id} cannot be read: {info.error}")
        value = {"plugin": "devflow", "flow": flow, **WORKFLOWS[flow].engine(project).status(run_id)}
        if info.recovery_required:
            value.update(recovery_required=True,
                         recovery="An interrupted transaction is pending; /devflow:resume or cancel completes it")
        return value
    record, record_error, current = None, None, None
    if session:
        try:
            record = sessions.read_record(project, session, WORKFLOWS)
            current = sessions.binding(project, record, all_stores)
        except DevFlowError as exc:
            record_error = str(exc)
    pending = record["pending_start"] if record else None
    caller = ("bound" if current.owner else "stale") if current else "pending_start" if pending else "idle"
    return {"plugin": "devflow", "version": __version__, "project": str(project), "status": caller,
            "run": record["run"] if current else None, "owner_matches": current.owner if current else None,
            "pending_start": pending and {key: pending[key] for key in ("flow", "run_id", "args", "created_at")},
            "session_record_error": record_error,
            **_overview(project, all_stores, session)}


def blocking_warnings(project: Path) -> list[str]:
    """What would stop a start, for doctor; read-only."""
    if not data_root(project).exists():
        return []
    overview = _overview(project, stores(project))
    warnings = [f"Pending {item['flow']} start in session record {item['record']} blocks new starts"
                for item in overview["sessions"]["pending_starts"]]
    warnings += [f"Invalid session record {item['record']}: {item['error']}" for item in overview["sessions"]["invalid_records"]]
    warnings += [f"Incomplete run creation {name}; a human may move it away" for name in overview["incomplete_creations"]]
    warnings += [f"Run {item['flow']}/{item['run_id']} needs recovery (interrupted transaction)"
                 for item in overview["runs"] if item.get("recovery_required")]
    warnings += [f"Run {item['flow']}/{item['run_id']} cannot be read and blocks new starts: {item.get('error')}"
                 for item in overview["runs"] if item["format"] == "invalid"]
    return warnings
