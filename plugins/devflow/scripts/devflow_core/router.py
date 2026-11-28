"""Session routing for every workflow, without per-workflow branches.

Hooks are session-scoped: a session without a record is inert and never
creates files. Hooks never deny a tool or block a prompt; they record human
events and add context. A session works on one run at a time; across
sessions, plan runs never block each other, and a workflow that occupies a
workspace (implement) allows at most one unfinished run per workspace, checked
when a run is created and when it is resumed. Ownership is decided by the run's
(``owner_host``, ``owner_session``). This module never dispatches a model itself.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
import json
from pathlib import Path

from .common import notices, sessions
from .common.paths import hook_project, project_root, same_path
from .common.errors import DevFlowError
from .common.hosts import host_profile, question_context, question_tool_name
from .common.operations import CHAIN_STATUSES, CHAINED, FLOW, flow_requested, public_command as detect_command
from .common.startup import parse_start
from .common.runs import (HOOK_WAIT, MAX_EVENT_BYTES, RUN_ID, RUNNER_WAIT, TERMINAL, RunStore, data_root,
                          ensure_data_root, event_bytes, make_event, new_run_id, unsupported_message,
                          validate_event, validate_run_id)
from .common.settings import read_project_settings
from .common.storage import file_lock, now, parse_json
from .workflows import WORKFLOWS

RESUME = "resume"


def public_command(prompt: str, host: str) -> tuple[str | None, str]:
    return detect_command(prompt, host, (*WORKFLOWS, FLOW, RESUME, "status", "doctor"))


def _started_workflow(command: str | None) -> str | None:
    """The workflow a human command starts: flow starts a Plan run (its Implement is chained later)."""
    return "plan" if command == FLOW else command if command in WORKFLOWS else None


def _entry_context(operation: str, host: str, session: str, project: Path, plugin_root: Path) -> dict | None:
    """What a host that does not expand the plugin's placeholders needs to run an operation."""
    profile = host_profile(host)
    if profile.expands_placeholders:
        return None
    return notices.context("UserPromptSubmit", f"DevFlow {profile.label} entry context: " + json.dumps({
        "operation": operation, "host": host, "session_id": session, "project": str(project),
        "runner": str(notices.runner_path(plugin_root)),
        "global_arguments": ["--host", host, "--project", str(project), "--session", session]}, ensure_ascii=False))


def stores(project: Path, wait: float = RUNNER_WAIT, *, host: str) -> dict[str, RunStore]:
    return {name: RunStore(project, workflow, wait, host=host) for name, workflow in WORKFLOWS.items()}


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


OCCUPIED_MESSAGE = ("Another unfinished {flow} run ({run_id}) uses this workspace ({path}); "
                    "resume or cancel it, or start in another worktree")
UNREADABLE_MESSAGE = ("Run {flow}/{run_id} cannot be read ({error}); its workspace is unknown, so preserve it "
                      "for human recovery before starting or resuming another {flow} run")


def blocks_workspace_check(flow: str, info) -> bool:
    """An unreadable run of a workspace-occupying workflow: its workspace is unknown.

    An entry whose name is not a run ID is not a run (it cannot be resumed), so it never blocks.
    """
    return (WORKFLOWS[flow].occupies_workspace and info.format == "invalid"
            and RUN_ID.fullmatch(info.run_id) is not None)


def assert_workspace_free(all_stores: dict[str, RunStore], flow: str, state: dict, selected=None) -> None:
    """At most one unfinished run per workspace; nothing for workflows that occupy none.

    Two checks: an unreadable run of a workspace-occupying workflow always stops a
    start or resume of that kind of run (its workspace is unknown); the
    occupancy check applies only while ``state`` itself is unfinished, because
    a finished run occupies no workspace.
    """
    workflow = WORKFLOWS[flow]
    if not workflow.occupies_workspace:
        return
    occupying = state["status"] not in TERMINAL  # A new run's fields start unfinished.
    target = workflow.workspace(state)
    for other_flow, store in all_stores.items():
        other = WORKFLOWS[other_flow]
        if not other.occupies_workspace:
            continue
        runs, _ = store.scan()
        for info in runs:
            if selected == (other_flow, info.run_id) or info.format == "unsupported":
                continue  # A run from an older format is never continued, so it never blocks.
            if blocks_workspace_check(other_flow, info):
                raise DevFlowError(UNREADABLE_MESSAGE.format(flow=other_flow, run_id=info.run_id, error=info.error))
            if (occupying and info.format == "current" and info.state["status"] not in TERMINAL
                    and _same_workspace(other.workspace(info.state), target)):
                raise DevFlowError(OCCUPIED_MESSAGE.format(flow=other_flow, run_id=info.run_id, path=target))


def _same_workspace(frozen: str, target: str) -> bool:
    """Whether another run's frozen workspace is the target. A path that can no longer be
    resolved (a symlink loop raises RuntimeError before Python 3.13) is not the resolved target."""
    try:
        return same_path(frozen, target)
    except (OSError, ValueError, RuntimeError):
        return False


@contextmanager
def _prepared(project: Path, pending: dict, all_stores: dict[str, RunStore], documents, options: dict | None = None):
    """The workflow fields and start records of a pending start, before any run exists.

    Large start records are written into the scratch directory, which is removed
    afterwards; ``_commit`` moves them into the run.
    """
    workflow = WORKFLOWS[pending["flow"]]
    project_config = workflow.load_config(documents)
    with all_stores[pending["flow"]].scratch(pending["run_id"]) as scratch:
        yield workflow.build_start(project, pending, dict(options or {}), scratch, project_config=project_config)


def _commit(session: str, pending: dict, all_stores: dict[str, RunStore], prepared) -> dict:
    """Create the prepared run; always called inside ``dispatch_lock``."""
    fields, artifacts = prepared
    store = all_stores[pending["flow"]]
    assert_workspace_free(all_stores, pending["flow"], fields)
    return store.create(fields, artifacts, run_id=pending["run_id"], owner=session,
                        first_event=pending["event"], owner_host=store.host)


def _create(project: Path, session: str, pending: dict, all_stores: dict[str, RunStore], documents) -> dict:
    """Prepare and create in one step, for a start whose records are small (inside the prompt hook)."""
    with _prepared(project, pending, all_stores, documents) as prepared:
        return _commit(session, pending, all_stores, prepared)


def _bound(record: dict, flow: str, run_id: str) -> dict:
    return dict(record, run={"flow": flow, "run_id": run_id}, pending_start=None)


def _settle(project: Path, record: dict, all_stores: dict[str, RunStore]) -> dict:
    """Finish an applied start, drop an unapplied one, and release a finished run
    or a run from an older DevFlow format (never continued).

    Only an actual next prompt reaches this point, so a finished run stays
    bound for the rest of the turn that finished it.
    """
    record = sessions.settle_pending_start(record, all_stores)
    if record["run"]:
        info = all_stores[record["run"]["flow"]].inspect(record["run"]["run_id"])
        if info.format == "unsupported" or (info.format == "current" and info.state["status"] in TERMINAL):
            record = dict(record, run=None)
    return record


def _context(event: str, flow: str, state: dict, store: RunStore, plugin_root: Path, event_id: str | None) -> dict:
    run_dir = store.run_dir(state["run_id"])
    return notices.context(event, notices.status_context(WORKFLOWS[flow], state, run_dir, plugin_root, event_id))


def _inactive_reason(current: sessions.Binding, host: str) -> str:
    if not current.supported:
        return unsupported_message(current.run_id, current.schema_version) + "; your next prompt releases it."
    return (f"This session no longer owns DevFlow run {current.run_id} (or its resume did not complete). "
            f"DevFlow does not record this session; send {host_profile(host).command_prefix}resume "
            f"{current.run_id} to continue the run.")


def _start(project: Path, session: str, flow: str, argument: str, payload: dict, record: dict,
           all_stores: dict[str, RunStore], plugin_root: Path) -> dict:
    workflow = WORKFLOWS[flow]
    current = sessions.binding(project, record, all_stores)
    if current is not None and not current.terminal:
        raise DevFlowError("Finish or cancel this session's active workflow before starting another")
    args = workflow.normalize_args(parse_start(argument).body, project)
    documents = read_project_settings(project, all_stores[flow].host, WORKFLOWS)
    if workflow.start_option is not None:
        # The launch hook confirms the selected settings from the documents it just
        # read; the actual start reads the files again and freezes its own values.
        workflow.load_config(documents)
    pending = {"flow": flow, "run_id": new_run_id(), "args": args, "event": make_event(payload, "prompt"),
               "created_at": now()}
    record = dict(record, run=None, pending_start=pending)
    sessions.write_record(project, record, WORKFLOWS)
    if workflow.start_option is not None:
        extra = "".join(f" {option.flag} is optional." for option in workflow.extra_start_options)
        entry = _entry_context(flow, all_stores[flow].host, session, project, plugin_root)
        entry = entry["hookSpecificOutput"]["additionalContext"] if entry else ""
        return notices.context("UserPromptSubmit", (entry + " " +
            f"DevFlow {flow} start is pending. Run the {flow} start action with {workflow.start_option} naming "
            f"exactly this input.{extra} Runner: {notices.runner_path(plugin_root)}. Pending input: {args}"))
    try:
        state = _create(project, session, pending, all_stores, documents)
    except Exception:
        # No run exists after a failed creation; drop the pending start, which
        # no run will apply. A killed process is settled next prompt.
        sessions.write_record(project, dict(record, pending_start=None), WORKFLOWS)
        raise
    sessions.write_record(project, _bound(record, flow, state["run_id"]), WORKFLOWS)
    return _context("UserPromptSubmit", flow, state, all_stores[flow], plugin_root, pending["event"]["event_id"])


def _resume(project: Path, session: str, argument: str, payload: dict, record: dict,
            all_stores: dict[str, RunStore], plugin_root: Path) -> dict:
    run_id = validate_run_id(argument.strip())
    flow, info = resolve_run(project, run_id, all_stores)
    if info.format == "unsupported":
        raise DevFlowError(unsupported_message(run_id, info.schema_version))
    current = sessions.binding(project, record, all_stores)
    if current is not None and (current.flow, current.run_id) != (flow, run_id) and not current.terminal:
        raise DevFlowError("Finish or cancel this session's active workflow before resuming another")
    store = all_stores[flow]
    with store.lock(run_id):
        state = store.load(run_id)
        reason = store.transfer_refusal(state, session)
        if reason:
            raise DevFlowError(reason)
        assert_workspace_free(all_stores, flow, state, selected=(flow, run_id))
        # The binding is written first: a stop before the ownership change
        # leaves only a non-owning binding, which asks for /devflow:resume again.
        sessions.write_record(project, _bound(record, flow, run_id), WORKFLOWS)
        state = store.transfer(state, session, payload)
    return _context("UserPromptSubmit", flow, state, store, plugin_root, state["event_ids"][-1])


def runner_start(project: Path, session: str | None, flow: str, raw: str, options: dict | None = None,
                 *, host: str) -> dict:
    """``FLOW start`` from the runner: creates exactly the run the user asked for."""
    if not session:
        raise DevFlowError("start requires the actual current host session ID")
    # The --session names the session that asked; it must also be THIS conversation.
    sessions.require_caller(host, session, flow=flow)
    workflow = WORKFLOWS[flow]
    all_stores = stores(project, host=host)
    refusal = f"Start only from an explicit /devflow:{flow} command in this session"
    with dispatch_lock(project, RUNNER_WAIT):
        record = sessions.read_record(project, session, WORKFLOWS, host)
        pending = record and record["pending_start"]
        # A chained start's pending start copies a Plan prompt, never the human's own command;
        # only a workflow that can be chained (``from_plan_option``) has such pending starts.
        if (not pending or pending["flow"] != flow
                or (workflow.from_plan_option and pending["event"]["payload"].get("operation") != flow)):
            raise DevFlowError(refusal)
        if workflow.normalize_args(raw, project) != pending["args"]:
            raise DevFlowError(f"The {workflow.start_option} value does not match the user's explicit start command")
        documents = read_project_settings(project, host, WORKFLOWS)
    # Prepared outside the lock: an implementation start captures its baseline
    # snapshot here, which can take long. The lock is taken again to re-check
    # the pending start and the workspace before the run is created.
    with _prepared(project, pending, all_stores, documents, options) as prepared:
        with dispatch_lock(project, RUNNER_WAIT):
            record = sessions.read_record(project, session, WORKFLOWS, host)
            if not record or record["pending_start"] != pending:
                raise DevFlowError(refusal)
            state = _commit(session, pending, all_stores, prepared)
            sessions.write_record(project, _bound(record, flow, state["run_id"]), WORKFLOWS)
    return _start_result(project, flow, state["run_id"], all_stores, host)


def _start_result(project: Path, flow: str, run_id: str, all_stores: dict[str, RunStore], host: str) -> dict:
    store = all_stores[flow]
    return {**WORKFLOWS[flow].engine(project, host=host).status(run_id),
            **WORKFLOWS[flow].start_fields(store, store.read(run_id))}


def _chained_run(all_stores: dict[str, RunStore], plan_run_id: str, plan_hash: str) -> dict | None:
    """The Implement run chained from this Plan run (check 7): only current-format runs
    whose frozen plan has the Plan's hash have their ``request.json`` read."""
    store = all_stores["implement"]
    for info in store.scan()[0]:
        if info.format != "current" or info.state["source_digest"] != plan_hash:
            continue
        intent = store.record_json(info.state, "request.json").get("intent") or {}
        if intent.get("action") == CHAINED and intent.get("plan_run_id") == plan_run_id:
            return info.state
    return None


def _chain_source(project: Path, session: str, plan_run_id: str, all_stores: dict[str, RunStore],
                  host: str, history: dict | None = None) -> tuple[dict, dict, dict | None, dict | None]:
    """The Plan run, its publication record, the run already chained from it, and the
    Plan run's verified history (None when a chained run is returned): its committed
    event digests and its last prompt event.

    Checks 2–6 (ownership, the flow marker, the ending, the publication, no resume since
    the end) apply only when nothing was chained yet: a chained run is returned as it is.
    Every Plan record it uses (request, publication receipt, events) is read
    digest-checked. Of the events only the tail from the last prompt on is read, when
    ``history`` is None; a later check passes that history back, and it still holds only
    while the run's committed event IDs and digests are unchanged.
    """
    flow, info = resolve_run(project, plan_run_id, all_stores)
    if flow != "plan" or info.format != "current":
        raise DevFlowError(f"Run {plan_run_id} is not a current Plan run")
    state, store = info.state, all_stores["plan"]
    chained = _chained_run(all_stores, plan_run_id, state["plan_hash"]) if state["plan_hash"] else None
    if chained is not None:
        return state, {}, chained, None
    manual = f"start Implement with {host_profile(host).command_prefix}implement PATH"
    if (state["owner_host"], state["owner_session"]) != (host, session):
        raise DevFlowError(f"Plan run {plan_run_id} is not owned by this conversation; {manual}")
    if not flow_requested(store.record_json(state, "request.json"), state["owner_host"]):
        raise DevFlowError(f"Plan run {plan_run_id} was not started by {host_profile(host).command_prefix}flow; {manual}")
    if state["status"] not in CHAIN_STATUSES:
        raise DevFlowError(f"Plan run {plan_run_id} ended {state['status']}, not completed, accepted or "
                           "auto_accepted; there is no approved plan to chain")
    if state["publication"]["status"] != "published":
        raise DevFlowError(f"Plan run {plan_run_id}'s plan is not published ({state['publication']['status']})")
    publication = store.record_json(state, "publication.json")
    if publication.get("plan_hash") != state["plan_hash"]:
        raise DevFlowError(f"The published plan no longer matches Plan run {plan_run_id}'s approved hash")
    digests = [(identifier, state["artifacts"].get(f"events/{identifier}.json")) for identifier in state["event_ids"]]
    if history is not None:
        if history["digests"] != digests:
            raise DevFlowError(f"Plan run {plan_run_id}'s events changed during the start; no run was created")
        return state, publication, None, history
    prompt = _last_prompt(store, state)
    if prompt is None:
        raise DevFlowError(f"Plan run {plan_run_id} has no recorded prompt to continue from; {manual}")
    # Events are appended in order, so the last prompt is the latest. Both are now()'s
    # second-precision UTC strings; a resume in the same second also refuses.
    if prompt["recorded_at"] >= state["finished_at"]:
        raise DevFlowError(f"Plan run {plan_run_id} was resumed after it ended; {manual}")
    return state, publication, None, {"digests": digests, "prompt": prompt}


def _last_prompt(store: RunStore, state: dict) -> dict | None:
    """The run's last prompt event, reading each event from the newest back to it once,
    against its recorded digest (the questions of the final turn and that prompt)."""
    for identifier in reversed(state["event_ids"]):
        relative = f"events/{identifier}.json"
        event = validate_event(parse_json(store.record(state, relative, limit=MAX_EVENT_BYTES + 4096), relative))
        if event["event_id"] != identifier:
            raise DevFlowError("Human event record does not match its ID")
        if event["kind"] == "prompt":
            return event
    return None


def _rejoin(project: Path, session: str, plan_run_id: str, chained: dict, all_stores: dict[str, RunStore],
            host: str, record: dict | None) -> str:
    """A repeated chain start: binds the run already chained from this Plan run, when it is
    this conversation's unfinished run and nothing else holds the session (inside
    ``dispatch_lock``), and returns its ID; the caller builds the result after the lock."""
    run_id = chained["run_id"]
    taken = f"Plan run {plan_run_id} already chained to Implement run {run_id}"
    if chained["status"] in TERMINAL or (chained["owner_host"], chained["owner_session"]) != (host, session):
        raise DevFlowError(taken)
    record = record or sessions.empty_record(session, host)
    target = {"flow": "implement", "run_id": run_id}
    pending = record["pending_start"]
    current = sessions.binding(project, record, all_stores)
    free = current is None or current.terminal or record["run"] == target
    if not free or (pending and (pending["flow"], pending["run_id"]) != ("implement", run_id)):
        raise DevFlowError(f"{taken}, but this conversation is bound to another run")
    sessions.write_record(project, _bound(record, "implement", run_id), WORKFLOWS)
    return run_id


def runner_start_from_plan(project: Path, session: str | None, plan_run_id: str, options: dict | None = None,
                           *, host: str) -> dict:
    """``implement start --from-plan``: the Implement run of a human's flow command, chained
    from its finished, published Plan run in the conversation that owns it.

    It uses the same pending start, preparation, re-check and creation as a manual start;
    the Plan run is checked again under its lock right before the run is created. Calling
    again returns the run already chained from the Plan run (``already_started``).
    """
    if not session:
        raise DevFlowError("start requires the actual current host session ID")
    sessions.require_caller(host, session, flow="implement")
    plan_run_id = validate_run_id(plan_run_id)
    workflow = WORKFLOWS["implement"]
    all_stores = stores(project, host=host)
    rejoined = None
    with dispatch_lock(project, RUNNER_WAIT):
        # Finding a chained run, reading the Plan's last prompt and writing this start's
        # pending start share one section: two calls cannot both find none, and a newer
        # prompt in this session clears the pending start.
        record = sessions.read_record(project, session, WORKFLOWS, host)
        plan, publication, chained, history = _chain_source(project, session, plan_run_id, all_stores, host)
        if chained is not None:
            rejoined = _rejoin(project, session, plan_run_id, chained, all_stores, host, record)
        else:
            record = record or sessions.empty_record(session, host)
            current = sessions.binding(project, record, all_stores)
            if current is not None and not current.terminal:
                raise DevFlowError("Finish or cancel this session's active workflow before starting another")
            if record["pending_start"]:
                raise DevFlowError("A start is already pending in this session; it is cleared by your next prompt "
                                   "when it created no run")
            pending = {"flow": "implement", "run_id": new_run_id(),
                       "args": workflow.normalize_args(publication["path"], project),
                       "event": history["prompt"],
                       "created_at": now()}
            record = dict(record, run=None, pending_start=pending)
            sessions.write_record(project, record, WORKFLOWS)
            documents = read_project_settings(project, host, WORKFLOWS)
    if rejoined is not None:
        return {**_start_result(project, "implement", rejoined, all_stores, host), "already_started": True}
    chain = {"plan_run_id": plan_run_id, "plan_hash": plan["plan_hash"], "plan_path": publication["path"],
             "approval_path": publication["approval_path"]}
    with _prepared(project, pending, all_stores, documents, {**(options or {}), "chain": chain}) as prepared:
        # The Plan run's lock is tried once: waiting for it while holding the project-wide
        # dispatch lock would hold up every other conversation's prompt hook.
        with dispatch_lock(project, RUNNER_WAIT), all_stores["plan"].lock(plan_run_id, wait=0):
            record = sessions.read_record(project, session, WORKFLOWS, host)
            if not record or record["pending_start"] != pending:
                raise DevFlowError("The chained start was superseded by a newer prompt in this session; "
                                   "no run was created")
            plan, publication, chained, _ = _chain_source(project, session, plan_run_id, all_stores, host, history)
            if chained is not None:
                record = dict(record, pending_start=None)
                sessions.write_record(project, record, WORKFLOWS)
                rejoined = _rejoin(project, session, plan_run_id, chained, all_stores, host, record)
            else:
                if publication["path"] != chain["plan_path"] or plan["plan_hash"] != chain["plan_hash"]:
                    raise DevFlowError(f"Plan run {plan_run_id}'s publication changed during the start; "
                                       "no run was created")
                state = _commit(session, pending, all_stores, prepared)
                sessions.write_record(project, _bound(record, "implement", state["run_id"]), WORKFLOWS)
    if rejoined is not None:
        return {**_start_result(project, "implement", rejoined, all_stores, host), "already_started": True}
    return _start_result(project, "implement", state["run_id"], all_stores, host)


def flow_warnings(project: Path, host: str, plan_state: dict) -> list[str]:
    """What a flow run will wait for or stop at: Plan from its frozen settings, Implement
    from the current project settings (as the chained start resolves them). Never raises."""
    config = plan_state["config"]
    try:
        implement, problem = WORKFLOWS["implement"].load_config(read_project_settings(project, host, WORKFLOWS)), None
        # The same settings diagnostic as implement doctor (for example an unsafe snapshot pattern).
        WORKFLOWS["implement"].doctor(project, implement, host=host)
    except (DevFlowError, OSError, ValueError) as exc:
        implement, problem = None, exc
    warnings = []
    if implement is not None and not implement["checks"]:
        warnings.append("Implement has no checks configured: every checkpoint will wait for human validation.")
    if config.get("review_limit_auto") == "ask":
        warnings.append('Plan review_limit_auto is "ask": the Plan review limit waits for the human.')
    if implement is not None and implement["review_limit_auto"] == "ask":
        warnings.append('Implement review_limit_auto is "ask": the Implement review limit waits for the human.')
    if not config["publish"]["enabled"]:
        warnings.append("Plan publication is disabled: the flow stops after Plan and does not start Implement.")
    if problem is not None:
        warnings.append(f"Implement settings cannot be read ({problem}): the chained Implement start will fail "
                        "until they are fixed.")
    return warnings


def flow_doctor(project: Path, run_id: str, *, host: str) -> dict:
    """``flow_warnings`` for a Plan run started by the flow command (none for another Plan run),
    or ``flow_marker_error`` when that cannot be verified. Implement's settings are read for
    the Plan run's owner host, the only host that can chain from it."""
    store = stores(project, host=host)["plan"]
    state = store.read(run_id)
    marker = WORKFLOWS["plan"].flow_marker(store, state)
    if marker["continues_to_implement"] is None:
        return {"flow_marker_error": marker["flow_marker_error"]}
    return ({"flow_warnings": flow_warnings(project, state["owner_host"], state)}
            if marker["continues_to_implement"] else {})


def selected_run(project: Path, session: str | None, flow: str, explicit: str | None,
                 *, host: str) -> str | None:
    """The run a workflow command acts on: an explicit ``--run`` or the session's binding."""
    if explicit:
        selected_flow, info = resolve_run(project, explicit, stores(project, host=host))
        if selected_flow != flow:
            raise DevFlowError("The selected run belongs to another workflow")
        if info.format == "unsupported":
            raise DevFlowError(unsupported_message(explicit, info.schema_version))
        return explicit
    if not session:
        return None
    record = sessions.read_record(project, session, WORKFLOWS, host)
    if record and record["run"] and record["run"]["flow"] == flow:
        run_id = record["run"]["run_id"]
        info = RunStore(project, WORKFLOWS[flow], host=host).inspect(run_id)
        if info.format == "unsupported":
            raise DevFlowError(unsupported_message(run_id, info.schema_version))
        return run_id
    return None


def handle_hook(payload: dict, plugin_root: Path, environ=None, *, host: str) -> dict | None:
    """Record human events and add context; never deny a tool or block a prompt.

    A failure is raised for the CLI to report without blocking. A question that
    could not be recorded at PreToolUse simply never counts as a human answer,
    because approval requires that record (``sessions.approval_candidates``).
    """
    if not isinstance(payload, dict):
        raise DevFlowError("Hook input must be a JSON object")
    event = payload.get("hook_event_name")
    # Each host's hook file registers these three events only (tools/package.py checks it):
    # Supported hosts send only these configured events to this hook.
    if event not in {"UserPromptSubmit", "PreToolUse", "PostToolUse"}:
        return None
    session = payload.get("session_id")
    if not isinstance(session, str) or not sessions.SESSION_ID.fullmatch(session):
        raise DevFlowError("Hook requires a valid host session ID")
    profile = host_profile(host)
    payload = dict(payload, host=host, source=payload)
    project = hook_project(payload.get("cwd"), os.environ if environ is None else environ,
                           profile.project_variable, from_cwd=profile.project_from_cwd)
    all_stores = stores(project, HOOK_WAIT, host=host)
    if event == "UserPromptSubmit" and isinstance(payload.get("prompt"), str):
        operation = public_command(payload["prompt"], host)[0]
        if operation in {"status", "doctor"}:
            return _entry_context(operation, host, session, project, plugin_root)
    path = sessions.session_path(project, session)
    info = sessions.inspect_record(path, WORKFLOWS) if path.exists() else None
    if info is not None and info.host is not None and info.host != host:
        raise DevFlowError("Session host collision; no records were changed")
    record = (info.record if info is not None and info.format == "unsupported"
              else sessions.read_record(project, session, WORKFLOWS, host))
    if event == "UserPromptSubmit":
        prompt = payload.get("prompt")
        if not isinstance(prompt, str):
            raise DevFlowError("UserPromptSubmit requires the original prompt")
        command, argument = public_command(prompt, host)
        if record is None and command is None:
            return None
        try:
            if command == RESUME:
                validate_run_id(argument.strip())
            elif _started_workflow(command):
                WORKFLOWS[_started_workflow(command)].normalize_args(parse_start(argument).body, project)
                event_bytes(make_event(payload, "prompt"))
            return _submit(project, session, command, argument, payload, all_stores, plugin_root)
        except (DevFlowError, OSError, ValueError) as exc:
            if command is not None:
                return notices.context(event, (
                    f"DevFlow did not accept {host_profile(host).command_prefix}{command} ({exc}). Check the runner's status before "
                    "continuing, and tell the human this reason."))
            return notices.context(event, _unrecorded(record, exc, "this prompt was not recorded as a run event."))
    if info is not None and info.format == "unsupported":
        return None
    if record is None or record["pending_start"]:
        return None  # No run exists yet, so nothing is recorded as evidence.
    if payload.get("tool_name") != question_tool_name(host):
        return None
    if event == "PreToolUse":
        return _tool_event(project, event, payload, record, all_stores, plugin_root)
    try:
        return _tool_event(project, event, payload, record, all_stores, plugin_root)
    except (DevFlowError, OSError, ValueError) as exc:
        return notices.context(event, _unrecorded(
            record, exc, "this answer was not recorded and cannot resolve a DevFlow escalation."))


def _unrecorded(record: dict, exc: Exception, what: str) -> str:
    target = record["run"] or record["pending_start"]
    return (f"DevFlow could not read or record {target['flow']} run {target['run_id']} ({exc}); {what} "
            "Check the run with the runner's status command and tell the human; do not repair or replace "
            "run records.")


def _submit(project: Path, session: str, command: str | None, argument: str, payload: dict,
            all_stores: dict[str, RunStore], plugin_root: Path) -> dict | None:
    """UserPromptSubmit for a session with a record or a DevFlow command."""
    event = "UserPromptSubmit"
    with dispatch_lock(project, HOOK_WAIT):
        host = payload["host"]
        path = sessions.session_path(project, session)
        info = sessions.inspect_record(path, WORKFLOWS) if path.exists() else None
        if info is not None and info.format == "unsupported":
            original = sessions.empty_record(session, host)
            sessions.write_record(project, original, WORKFLOWS)
        else:
            original = sessions.read_record(project, session, WORKFLOWS, host) or sessions.empty_record(session, host)
        record = _settle(project, original, all_stores)
        if record != original:
            # Kept even if the command below fails (e.g. resuming a run from an older format).
            sessions.write_record(project, record, WORKFLOWS)
        if command == RESUME:
            return _resume(project, session, argument, payload, record, all_stores, plugin_root)
        if _started_workflow(command):
            return _start(project, session, _started_workflow(command), argument, payload, record, all_stores,
                          plugin_root)
        sessions.write_record(project, record, WORKFLOWS)
        current = sessions.binding(project, record, all_stores)
        if current is None:
            return None
        if not current.owner:
            # A prompt here is ordinary conversation, never a human event of the run.
            return notices.context(event, _inactive_reason(current, host))
        store = all_stores[current.flow]
        event_id = store.append_event(current.run_id, payload, "prompt")
        return _context(event, current.flow, store.read(current.run_id), store, plugin_root, event_id)


def _tool_event(project: Path, event: str, payload: dict, record: dict, all_stores: dict[str, RunStore],
                plugin_root: Path) -> dict | None:
    """AskUserQuestion (PreToolUse and PostToolUse) for the session that owns its run."""
    current = sessions.binding(project, record, all_stores)
    if current is None or not current.owner or not current.supported:
        return None  # Never recorded: this session does not own a current run.
    store = all_stores[current.flow]
    payload = question_context(payload, store.events(store.read(current.run_id)))
    if event == "PreToolUse":
        # The native question, before the screen adds the human's answers.
        store.append_event(current.run_id, payload, "observation")
        return None
    event_id = store.append_event(current.run_id, payload, "question")
    if event_id is None:
        return None
    return notices.context(event, f"DevFlow recorded this human question/answer as event {event_id}. "
                                  "Use its exact response and event ID if resolving a human escalation.")


def _overview(project: Path, all_stores: dict[str, RunStore]) -> dict:
    runs, creating = [], []
    for flow, store in all_stores.items():
        infos, leftovers = store.scan()
        creating.extend(f"{flow}/{name}" for name in leftovers)
        for info in infos:
            item = info.public()
            if info.format == "current":
                try:
                    item.update(status=info.state["status"], updated_at=info.state["updated_at"],
                                **WORKFLOWS[flow].summary(info.state))
                except Exception as exc:  # noqa: BLE001 - one unreadable run must not hide the others
                    item.update(format="invalid", error=f"Cannot summarize run: {type(exc).__name__}: {exc}")
            runs.append(item)
    invalid, unsupported = [], []
    for info in sessions.scan_records(project, WORKFLOWS):
        if info.format == "unsupported":
            unsupported.append({"record": info.name, "host": info.host, "schema_version": info.schema_version})
        elif info.error:
            invalid.append({"record": info.name, "error": info.error})
    return {"runs": runs, "sessions": {"invalid_records": invalid, "unsupported_records": unsupported},
            "incomplete_creations": creating}


def status(project: Path, session: str | None, run_id: str | None, host: str) -> dict:
    """Changes nothing: never creates, repairs or releases anything."""
    all_stores = stores(project, host=host)
    if run_id:
        flow, info = resolve_run(project, run_id, all_stores)
        if info.format == "unsupported":
            return info.public()
        if info.format == "invalid":
            raise DevFlowError(f"Run {run_id} cannot be read: {info.error}")
        return {"flow": flow, **WORKFLOWS[flow].engine(project, host=host).status(run_id)}
    record, record_error, current = None, None, None
    if session:
        try:
            path = sessions.session_path(project, session)
            info = sessions.inspect_record(path, WORKFLOWS) if path.exists() else None
            if info is not None and info.format == "unsupported":
                return {"status": "unsupported", "session_host": info.host,
                        "session_schema_version": info.schema_version, "owner_matches": False,
                        "session_record_error": (None if info.host == host else "Session host collision"),
                        **_overview(project, all_stores)}
            record = sessions.read_record(project, session, WORKFLOWS, host)
            current = sessions.binding(project, record, all_stores)
        except DevFlowError as exc:
            record_error = str(exc)
    pending = record["pending_start"] if record else None
    caller = (("unsupported" if not current.supported else "bound" if current.owner else "stale") if current
              else "pending_start" if pending else "idle")
    extra = {"message": unsupported_message(current.run_id, current.schema_version)} if caller == "unsupported" else {}
    return {"project": str(project), "status": caller, **extra,
            "run": record["run"] if current else None, "owner_matches": current.owner if current else None,
            "pending_start": pending and {key: pending[key] for key in ("flow", "run_id", "args", "created_at")},
            "session_record_error": record_error,
            **_overview(project, all_stores)}


def record_warnings(project: Path, host: str) -> list[str]:
    """Records a human may need to look at, for doctor; changes nothing.

    Run warnings come from the same ``RunStore.scan`` records the workspace
    check reads, so a warning says it blocks exactly when the check would stop.
    """
    if not data_root(project).exists():
        return []
    all_stores = stores(project, host=host)
    overview = _overview(project, all_stores)
    warnings = [f"Invalid session record {item['record']}: {item['error']}" for item in overview["sessions"]["invalid_records"]]
    warnings += [f"Incomplete run creation {name}; if no DevFlow start is still running, a human may move it away"
                 for name in overview["incomplete_creations"]]
    for flow, store in all_stores.items():
        for info in store.scan()[0]:
            if info.format != "invalid":
                continue
            effect = f" and blocks {flow} starts and resumes" if blocks_workspace_check(flow, info) else ""
            warnings.append(f"Run {flow}/{info.run_id} cannot be read{effect}: {info.error}")
    return warnings
