"""Implement workflow engine: only Claude edits project code; the runner keeps score."""
from __future__ import annotations

from pathlib import Path, PurePosixPath

from . import checks, snapshot
from .review import validate_review
from .workspace import branch, verify_workspace
from ..common.errors import DevFlowError
from ..common.review import (LIMIT_DECISIONS, REVIEW_BUDGET, after_failed_review, all_approved, ask_at_limit, decide_at_limit,
                             next_reviewer)
from ..common import summary
from ..common.reviewers import preflight, recorded_cli_version, review_tokens, run_review
from ..common.engine import RunEngine
from ..common.runs import RUNNER_WAIT, TERMINAL, open_escalation
from ..common.sessions import approval_candidates
from ..common.storage import checked_text, digest, json_bytes, no_symlinks, now, read_json, within


def _plan_bytes(path: Path, limit: int) -> bytes:
    """Read one bounded, stable UTF-8 input without newline/BOM rewriting."""
    no_symlinks(path)
    try:
        before = path.stat()
        if not path.is_file() or before.st_size > limit:
            raise DevFlowError(f"Input plan must be a regular file no larger than {limit} bytes")
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        no_symlinks(path)
        after = path.stat()
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
        if (len(data) > limit or len(data) != after.st_size
                or any(getattr(before, name) != getattr(after, name) for name in fields)):
            raise DevFlowError("Input plan changed while it was being read; start again after editing stops")
        text = data.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise DevFlowError(f"Input plan must be readable UTF-8: {path}: {exc}") from exc
    if not text.lstrip("\ufeff").strip() or "\x00" in text:
        raise DevFlowError("Input plan must be nonempty UTF-8 text without NUL")
    return data


class Engine(RunEngine):
    def __init__(self, project: Path, resources: Path | None = None, lock_wait: float = RUNNER_WAIT,
                 session: str | None = None):
        from .workflow import WORKFLOW
        super().__init__(project, WORKFLOW, resources, lock_wait, session)

    def _verify(self, state, source=True):
        super()._verify(state)
        if source:
            verify_source(state)

    def _owner(self, state, session_id):
        self._require_owner(state, session_id)

    def _workspace(self, state) -> Path:
        """The frozen workspace, re-verified (path, repository, directory identity)."""
        return verify_workspace(self.project, state["workspace"], state["config"]["snapshot"]["mode"])

    def _draft(self, state, path):
        path = Path(path)
        if not path.is_absolute():
            path = self.project / path
        return within(path, self._run(state["run_id"]) / "draft")

    def _require(self, state, allowed):
        if state["status"] not in allowed:
            raise DevFlowError(f"Cannot perform this action in {state['status']}; expected {sorted(allowed)}")

    def _escalate(self, state, kind, questions, return_status, reason):
        index = len(state["escalations"]) + 1
        identifier = f"E{index:03}"
        snapshot_id = (state.get("candidate") or {}).get("snapshot_id")
        value = open_escalation(state, identifier, reason, questions, return_status,
                                {"kind": kind, "snapshot_id": snapshot_id})
        path = f"escalations/{index:03}.json"
        self._artifact(state, path, json_bytes({**value, "created_at": now()}))
        state["escalations"].append(path)
        state["status"] = "awaiting_human"
        if kind != REVIEW_BUDGET:
            # The review limit question changes neither the code nor the plan, so
            # approvals of the current snapshot stay (as in plan); others clear them.
            state["approvals"] = {}

    def checkpoint(self, run_id, session_id, summary_file):
        with self._lock(run_id):
            state = self._state(run_id)
            self._owner(state,session_id)
            self._require(state,{"implementing","ready_check","ready_review"})
            self._verify(state)
            summary = checked_text(self._draft(state, summary_file), 256*1024)
            workspace = self._workspace(state)
            current = snapshot.capture(workspace,state["config"])
            baseline = read_json(self._run(run_id)/"baseline.json")
            # The changes and their diff are written once; no listing or contents are copied.
            record = snapshot.changes(baseline,current)
            diff = snapshot.diff(workspace,self._run(run_id),baseline,current,record)
            number = state["checkpoint_count"] + 1
            base = f"snapshots/{number:03}"
            self._artifact(state,base+".json",json_bytes(record))
            self._artifact(state,base+".diff",diff.encode("utf-8"))
            self._artifact(state,base+"-summary.md",summary.encode("utf-8"))
            prior = (state.get("candidate") or {}).get("snapshot_id")
            if prior != current["snapshot_id"]:
                state["approvals"] = {}
                state["binary_attestation"] = None
            state.update(status="ready_check",checkpoint_count=number,validation=None,
                candidate={"snapshot_id":current["snapshot_id"],"path":base+".json","diff":base+".diff","summary":base+"-summary.md"})
            if record["binary_changes"] and not state.get("binary_attestation"):
                self._escalate(state,"binary_changes",["Review these binary changes independently and confirm whether their contents are acceptable: " + ", ".join(record["binary_changes"])],"ready_check","Text-only reviewers cannot inspect binary content")
            self._save(state)
        return self.status(run_id)

    def _accept_current(self, state):
        """The version the human accepts: the checkpoint the workspace still holds,
        with the validation a review would need (as ``review`` requires)."""
        try:
            current = self._current(state)
        except DevFlowError as exc:
            raise DevFlowError(f"Cannot accept: {exc}. Restore the workspace to the checkpoint, "
                               "or answer continue or stop") from exc
        validation = state.get("validation") or {}
        if validation.get("snapshot_id") != current["snapshot_id"] or validation.get("status") not in {"passed","human_attested"}:
            raise DevFlowError("Cannot accept: the current checkpoint has no passing checks or human validation; "
                               "answer continue or stop")
        return current

    def _current(self, state):
        self._verify(state)
        candidate = state.get("candidate")
        if not candidate:
            raise DevFlowError("Create a checkpoint first")
        current = snapshot.capture(self._workspace(state),state["config"])
        if current["snapshot_id"] != candidate["snapshot_id"]:
            raise DevFlowError("Code changed since checkpoint; create a fresh checkpoint and rerun checks")
        return current

    def check(self, run_id, session_id):
        def prepare(state):
            self._owner(state, session_id)
            self._require(state, {"ready_check"})
            self._current(state)
            if state["check_attempts"] >= state["config"]["max_check_attempts"]:
                self._escalate(state,"check_budget",["Repeated checks did not converge. Review the failures before authorizing another bounded attempt."],"implementing","Check attempt limit reached")
                return None
            state["check_attempts"] += 1
            state["check_dispatch_count"] += 1
            index = state["check_dispatch_count"]
            # No lock is held while project code runs. Checks run in the
            # workspace that _current has just verified.
            return index, state["candidate"]["snapshot_id"], None, {
                "index": index, "checks": state["config"]["checks"], "root": state["workspace"]["path"]}

        def execute(work):
            return checks.run_checks(Path(work["root"]), work["checks"],
                                     self._run(run_id) / "check-processes" / f"{work['index']:03}")

        return self._dispatch(run_id, "checking", "check", prepare, execute, self._finish_check)

    def _finish_check(self, state, reservation, work, result, error):
        if error is None:
            try:
                self._current(state)
            except DevFlowError as exc:  # e.g. the check itself rewrote source
                error = exc
        if error is not None:
            state.update(status="paused_error",resume_status="implementing",last_error=str(error),approvals={})
            return
        path = f"checks/{reservation['number']:03}.json"
        self._artifact(state,path,json_bytes(result))
        state["check_history"].append(path)
        state["validation"] = {"snapshot_id":state["candidate"]["snapshot_id"],"kind":"automated","status":result["status"],"path":path}
        if result["status"] == "not_configured":
            self._escalate(state,"manual_validation",["No project checks are configured. Provide actual manual validation evidence for this exact code version, or cancel and configure checks for a new run."],"ready_review","No checks configured")
        elif result["status"] == "passed":
            state["status"] = "ready_review"  # At the review limit, the next review request asks the human.
        else:
            state.update(status="implementing",approvals={})
            if state["check_attempts"] >= state["config"]["max_check_attempts"]:
                self._escalate(state,"check_budget",["Checks continue to fail. Determine the cause and authorize another bounded attempt, or cancel."],"implementing","Check attempt limit reached")

    def _packet(self,state,current,workspace,diff_path):
        """Review data only: the reviewer reads the code itself, from the workspace and the diff file."""
        run = self._run(state["run_id"])
        record = read_json(run/state["candidate"]["path"])
        for path in state["config"]["snapshot"]["context_paths"]:
            if path not in current["files"] or current["files"][path].get("binary"):
                raise DevFlowError(f"Context path must name an included text file: {path}")
        history = []
        for item in state["attempts"]:
            entry = {k:item[k] for k in ("number","reviewer_id","snapshot_id","status")}
            if item.get("review_path"):
                entry["review"] = read_json(run/item["review_path"])
            history.append(entry)
        # The check record names its log files; the output is only in the logs.
        check_results = read_json(run/state["validation"]["path"]) if state["validation"].get("path") else None
        mode = state["config"]["snapshot"]["mode"]
        value = {"schema_version":2,"kind":"code_review","snapshot_id":current["snapshot_id"],
            "workspace":{"path":str(workspace),"mode":mode,"branch":branch(workspace,mode),
                         "start_commit":(read_json(run/"baseline.json").get("git") or {}).get("head")},
            "plan":(run/"plan.md").read_bytes().decode("utf-8"),"plan_path":str(run/"plan.md"),
            "plan_sha256":state["artifacts"]["plan.md"],"baseline_id":state["baseline_id"],
            "implementation_summary":checked_text(run/state["candidate"]["summary"]),
            "changes":snapshot.packet_changes(record),"diff_path":str(diff_path),
            "context_paths":list(state["config"]["snapshot"]["context_paths"]),
            "scope":{"mode":current["mode"],"excluded":current.get("excluded",[]),"limitations":current.get("scope_note")},
            "validation":state["validation"],"check_results":check_results,
            "binary_attestation":state.get("binary_attestation"),"previous_reviews":history,
            "reflections":[read_json(run/p) for p in state["reflections"]],
            "human_resolutions":[read_json(run/p) for p in state["resolutions"]]}
        data = json_bytes(value)
        if len(data) > state["config"]["snapshot"]["max_packet_bytes"]:
            raise DevFlowError("Review packet exceeds configured limit. Do not silently truncate; narrow the plan/scope or explicitly configure a larger limit for a new run")
        return data

    def review(self,run_id,session_id):
        state = self._state(run_id)
        self._owner(state,session_id)
        self._require(state,{"ready_review"})
        # At the review limit no reviewer starts; the request asks the human instead.
        provider = (next_reviewer(state["config"]["reviewers"],len(state["attempts"]),state["review_limit"])
                    if len(state["attempts"]) < state["review_limit"] else None)
        # A missing or unsupported reviewer CLI must not consume an attempt.
        probe = preflight(provider) if provider else None

        def prepare(state):
            self._owner(state,session_id)
            self._require(state,{"ready_review"})
            # The workspace is verified first, so the question names only approvals
            # of the code that is actually there.
            current = self._current(state)
            approved = {reviewer for reviewer, snapshot_id in state["approvals"].items()
                        if snapshot_id == current["snapshot_id"]}
            if ask_at_limit(state,len(state["attempts"]),approved,current["snapshot_id"],
                            lambda reason, questions: self._escalate(state,REVIEW_BUDGET,questions,"ready_review",reason)):
                return None
            validation = state.get("validation") or {}
            if validation.get("snapshot_id") != current["snapshot_id"] or validation.get("status") not in {"passed","human_attested"}:
                raise DevFlowError("Fresh successful checks or explicit human validation are required")
            if next_reviewer(state["config"]["reviewers"],len(state["attempts"]),state["review_limit"]) != provider:
                raise DevFlowError("The run changed during the reviewer check; inspect status and retry")
            number = len(state["attempts"]) + 1
            # The diff was written once, at the checkpoint.
            diff_path = self._run(run_id)/state["candidate"]["diff"]
            # _current has just verified the workspace.
            workspace = Path(state["workspace"]["path"])
            packet = self._packet(state,current,workspace,diff_path)
            packet_relative = f"reviews/{number:02}/packet.json"
            self._artifact(state,packet_relative,packet)
            state["attempts"].append({"number":number,"reviewer_id":provider["id"],"snapshot_id":current["snapshot_id"],
                "snapshot_path":state["candidate"]["path"],
                "packet_path":packet_relative,"packet_sha256":digest(packet),"status":"dispatched","started_at":now()})
            # Reserved BEFORE dispatch, including unknown-outcome failures.
            return number, current["snapshot_id"], provider["id"], {
                "number": number, "packet": packet_relative, "workspace": str(workspace)}

        def execute(work):
            return run_review(provider,self._run(run_id)/work["packet"],
                self._run(run_id)/"review-processes"/f"{work['number']:02}",self.resources/"review.schema.json",
                self.resources/"reviewer.md",validate_review,probe,cwd=Path(work["workspace"]))

        def finish(state, reservation, work, result, error):
            if error is None and result["review"]["snapshot_id"] != reservation["target"]:
                error = DevFlowError("Reviewer response is for a different snapshot")
            self._finish_review(state, reservation, provider, result, error)

        return self._dispatch(run_id, "reviewing", "review", prepare, execute, finish)

    def _finish_review(self,state,reservation,provider,result,error):
        number = reservation["number"]
        review = result["review"] if error is None else None
        # The token counts the CLI printed, whatever the outcome.
        tokens = review_tokens(provider, self._run(state["run_id"])/"review-processes"/f"{number:02}")
        current = None
        if error is None:
            try:
                current = self._current(state)
            except DevFlowError as exc:
                error = exc
        if error is not None:
            # What the reviewer did is kept when it ran to the end.
            state["attempts"][-1].update(status="failed",error=str(error),finished_at=now(),tokens=tokens,
                                         **({"activity":result["activity"]} if result is not None else {}))
            # A reviewer failure says nothing about the code: approvals for this
            # snapshot stay (as in plan); this attempt never adds one.
            state.update(status=after_failed_review(number,state,"ready_review"),resume_status="ready_review",last_error=str(error))
            return
        review_path = f"reviews/{number:02}/review.json"
        self._artifact(state,review_path,json_bytes(review))
        state["attempts"][-1].update(status="succeeded",verdict=review["verdict"],review_path=review_path,activity=result["activity"],
                                     finished_at=now(),tokens=tokens)
        critical = any(f["severity"] == "critical" for f in review["findings"])
        if review["verdict"] == "human_required" or critical:
            questions = review["questions"] or ["A critical issue was reported. Confirm the safe scope and remediation before continuing."]
            self._escalate(state,"review_decision",questions,"needs_revision","Independent review requires a human decision")
        elif review["verdict"] == "changes_required":
            state.update(status="needs_revision",approvals={})
        else:
            state["approvals"][provider["id"]] = current["snapshot_id"]
            # At the review limit, the next review request asks the human.
            done = all_approved(state["config"]["reviewers"],state["approvals"],current["snapshot_id"])
            state["status"] = "completed" if done else "ready_review"

    def reflect(self,run_id,session_id,decisions_file):
        with self._lock(run_id):
            state = self._state(run_id)
            self._owner(state,session_id)
            self._require(state,{"needs_revision"})
            self._verify(state)
            value = read_json(self._draft(state,decisions_file))
            last = state["attempts"][-1]
            review = read_json(self._run(run_id)/last["review_path"])
            if (set(value) != {"review_number","summary","decisions"}
                    or type(value["review_number"]) is not int or value["review_number"] != last["number"]
                    or not isinstance(value["summary"],str) or not value["summary"].strip()
                    or not isinstance(value["decisions"],list)):
                raise DevFlowError("Reflection needs review_number, nonempty summary, and decisions")
            expected = {f["id"] for f in review["findings"]}
            seen = set()
            for item in value["decisions"]:
                if not isinstance(item,dict) or set(item) != {"finding_id","action","reason"} or not isinstance(item["finding_id"],str) or item["finding_id"] in seen or item["finding_id"] not in expected:
                    raise DevFlowError("Every finding needs exactly one decision")
                seen.add(item["finding_id"])
                if not isinstance(item["action"],str) or item["action"] not in {"accept","reject","already_addressed","escalate"} or not isinstance(item["reason"],str) or not item["reason"].strip():
                    raise DevFlowError("Invalid disposition or missing evidence/reason")
            if seen != expected:
                raise DevFlowError("Missing finding dispositions")
            if any(x["action"] == "escalate" for x in value["decisions"]):
                self._escalate(state,"reflection_decision",[x["reason"] for x in value["decisions"] if x["action"] == "escalate"],"needs_revision","Claude cannot safely resolve a finding")
            else:
                path = f"decisions/review-{last['number']:02}.json"
                self._artifact(state,path,json_bytes(value))
                state["reflections"].append(path)
                state.update(status="implementing",revision=state["revision"]+1,check_attempts=0,validation=None,approvals={})
            self._save(state)
        return self.status(run_id)

    def escalate(self,run_id,session_id,questions_file):
        with self._lock(run_id):
            state = self._state(run_id)
            self._owner(state,session_id)
            self._require(state,{"implementing","needs_revision","ready_check","ready_review"})
            value = read_json(self._draft(state,questions_file))
            if set(value) != {"reason","questions"} or not isinstance(value["reason"],str) or not value["reason"].strip() or not isinstance(value["questions"],list) or not value["questions"] or any(not isinstance(q,str) or not q.strip() for q in value["questions"]):
                raise DevFlowError("Escalation requires nonempty reason and question strings")
            self._escalate(state,"implementation_decision",value["questions"],state["status"],value["reason"])
            self._save(state)
        return self.status(run_id)

    def resolve(self,run_id,session_id,event_id,decision,reason):
        with self._lock(run_id):
            state = self._state(run_id)
            self._owner(state,session_id)
            self._require(state,{"awaiting_human"})
            esc = state["escalation"]
            matches = [x for x in approval_candidates(state, self.store.events(state)) if x["event_id"] == event_id]
            if len(matches) != 1:
                raise DevFlowError("Use the event ID of a successful AskUserQuestion answer recorded AFTER this escalation, "
                                   "in the session that owns the run; ordinary prompts and the resume command are not answers")
            kind = esc["detail"]["kind"]
            budget = kind == REVIEW_BUDGET
            allowed = set(LIMIT_DECISIONS) if budget else {"continue","cancel"}
            if decision not in allowed or not isinstance(reason,str) or not reason.strip():
                raise DevFlowError("Supply continue/stop/accept for the review limit question (continue/cancel otherwise) "
                                   "and a faithful explanation of the human's decision")
            # Verify before holding new immutable artifacts: _current checks every
            # listed artifact, and a held one is not on disk until the save.
            if decision == "continue" and kind in {"manual_validation", "binary_changes"}:
                self._current(state)
            elif decision == "accept":
                self._accept_current(state)
            else:
                self._verify(state)
            value = {"escalation_id":esc["id"],"decision":decision,"interpretation":reason,"human_event":matches[0],
                     "snapshot_id":esc["detail"].get("snapshot_id"),"created_at":now()}
            path = f"resolutions/{len(state['resolutions'])+1:03}.json"
            self._artifact(state,path,json_bytes(value))
            state["resolutions"].append(path)
            if budget:
                # Neither the code nor the plan changes: approvals of the current snapshot stay.
                decide_at_limit(state,decision,esc["return_status"])
                state["escalation"] = None
            else:
                state.update(status="cancelled" if decision=="cancel" else esc["return_status"],escalation=None,approvals={})
            if decision == "continue" and not budget:
                if kind == "manual_validation":
                    # At the review limit, the next review request asks the human.
                    state["validation"] = {"snapshot_id":state["candidate"]["snapshot_id"],"kind":"manual","status":"human_attested","resolution":path}
                elif kind == "binary_changes":
                    state["binary_attestation"] = {"snapshot_id":state["candidate"]["snapshot_id"],"resolution":path}
                elif kind == "check_budget":
                    state["check_attempts"] = 0  # Explicit human authorization, never a review-budget reset.
            self._save(state)
        return self.status(run_id)

    def cancel(self,run_id,session_id,reason):
        with self._lock(run_id):
            state = self._state(run_id)
            self._owner(state,session_id)
            if state["status"] in {"reviewing","checking"}:
                raise DevFlowError("Confirm active child processes stopped before cancellation")
            # As in plan, a run that has ended stays as it ended (with its report).
            if state["status"] not in TERMINAL:
                state.update(status="cancelled",last_error=reason,approvals={})
                self._save(state)
        return self.status(run_id)

    def _before_dispatch(self, state):
        state["last_error"] = None

    def _status_details(self, state, result):
        result["reviews"] = review_rounds(state)
        if state["status"] in TERMINAL:
            run = self._run(state["run_id"])
            report_path = within(run / state["report_path"], run)
            if digest(report_path.read_bytes()) != state["artifacts"][state["report_path"]]:
                raise DevFlowError("The recorded terminal report was modified")
            result["report"] = read_json(report_path)

def review_rounds(state):
    """Each review attempt: its reviewer, snapshot, outcome, its tool calls when the reviewer ran to
    the end, and its verdict when it succeeded."""
    return [{"number":a["number"],"reviewer_id":a["reviewer_id"],"snapshot_id":a["snapshot_id"],"status":a["status"],
             **({"verdict":a["verdict"]} if "verdict" in a else {}),**(a.get("activity") or {})} for a in state["attempts"]]


def verify_source(state):
    """The original plan file must still be exactly the one this run started from."""
    path = Path(state["plan_source"])
    no_symlinks(path)
    if not path.is_file() or digest(path.read_bytes()) != state["source_digest"]:
        raise DevFlowError("Original plan changed/missing. Resolve requirements and start a new run; do not silently replace it")


ACCEPTED_WARNING = "Accepted by the human at the review limit without approval from every reviewer."


def _checkpoint_label(path: object) -> str:
    """``snapshots/003.json`` is checkpoint 3."""
    try:
        return f"checkpoint {int(PurePosixPath(path).stem)}"
    except (TypeError, ValueError):
        return "unknown"


def human_decisions(state: dict, read) -> tuple[list[dict], dict | None]:
    """The human's answers (``common.summary`` shape), and the acceptance at the review
    limit when the human accepted the code."""
    questions = {}
    for path in state["escalations"]:
        value = read(path)
        questions[value["id"]] = value["questions"]
    decisions, acceptance = [], None
    for path in state["resolutions"]:
        value = read(path)
        event = value["human_event"]
        decisions.append({"at": value.get("created_at"), "review_number": None,
                          "questions": questions.get(value["escalation_id"], []), "answer": event.get("answer_text"),
                          "decision": value["decision"], "interpretation": value.get("interpretation"),
                          "event_ids": [event["event_id"]]})
        if value["decision"] == "accept":
            acceptance = {"event_ids": [event["event_id"]], "answer": event.get("answer_text"),
                          "version": value.get("snapshot_id"), "accepted_at": value.get("created_at"), "record": path}
    return decisions, acceptance


def summary_source(store, state: dict, workspace: dict | None = None) -> dict:
    """This implement run in the shape of ``common.summary`` (see that module).

    After the run ends, the current version is the workspace version the terminal
    report captured (``workspace``: its ``snapshot_id`` and ``reviewed``, i.e.
    ``final_snapshot_reviewed``); without ``workspace`` it is read from the saved
    report, never captured again. Before the end it is the checkpoint, which
    ``review`` has just verified before asking at the limit.
    """
    run = store.run_dir(state["run_id"])
    read = lambda relative: store.record_json(state, relative)
    attempts, candidate = state["attempts"], state.get("candidate") or {}
    reviews = []
    for index, attempt in enumerate(attempts):
        outcome = attempt["status"] if attempt["status"] in {"succeeded", "failed"} else "in_progress"
        entry = {"number": attempt["number"], "reviewer_id": attempt["reviewer_id"], "status": outcome,
                 "cli_version": recorded_cli_version(run / "review-processes" / f"{attempt['number']:02}"),
                 "cli_version_source": f"review-processes/{attempt['number']:02}/invocation.json",
                 "started_at": attempt.get("started_at"), "finished_at": attempt.get("finished_at"),
                 "version": {"id": attempt["snapshot_id"], "label": _checkpoint_label(attempt.get("snapshot_path"))},
                 "verdict": attempt.get("verdict"), "error": attempt.get("error"), "findings": [], "fix": None,
                 "tokens": attempt.get("tokens"), "record": attempt.get("review_path")}
        if outcome == "succeeded":
            review = read(attempt["review_path"])
            reflected = f"decisions/review-{attempt['number']:02}.json"
            reflection = read(reflected) if reflected in state["reflections"] else None
            dispositions = {item["finding_id"]: item for item in (reflection or {}).get("decisions", [])}
            entry["findings"] = [{"id": f["id"], "severity": f["severity"], "category": f["category"],
                                  "location": f"{f['file']}:{f['line_start']}-{f['line_end']}",
                                  "recommendation": f["recommendation"],
                                  "action": dispositions.get(f["id"], {}).get("action"),
                                  "reason": dispositions.get(f["id"], {}).get("reason")} for f in review["findings"]]
            if reflection is not None:
                # What changed from the reviewed checkpoint to the next one reviewed (or the latest).
                earlier = attempt.get("snapshot_path")
                later = attempts[index + 1].get("snapshot_path") if index + 1 < len(attempts) else candidate.get("path")
                files = None
                if earlier and later:
                    files = [] if earlier == later else snapshot.between(read(earlier), read(later))
                entry["fix"] = {"summary": reflection["summary"], "version_change": None, "file_changes": files}
        reviews.append(entry)
    decisions, acceptance = human_decisions(state, read)
    approvals = state["approvals"]
    if state["status"] in TERMINAL:
        if workspace is None:
            report = read(state["report_path"])
            workspace = {"snapshot_id": report["workspace_snapshot_id_at_report"],
                         "reviewed": report["final_snapshot_reviewed"]}
        actual = workspace["snapshot_id"]
        if actual is None:
            current = {"id": None, "label": "unknown (workspace capture failed)"}
        elif actual == candidate.get("snapshot_id"):
            current = {"id": actual, "label": _checkpoint_label(candidate.get("path"))}
        else:
            current = {"id": actual, "label": "workspace at report (not a checkpoint)"}
        reviewed = bool(actual) and bool(workspace["reviewed"])
        approving = [reviewer for reviewer, snapshot_id in approvals.items() if actual is not None and snapshot_id == actual]
    elif candidate:
        current = {"id": candidate["snapshot_id"], "label": _checkpoint_label(candidate.get("path"))}
        reviewed = any(a["status"] == "succeeded" and a["snapshot_id"] == candidate["snapshot_id"] for a in attempts)
        approving = [reviewer for reviewer, snapshot_id in approvals.items() if snapshot_id == candidate["snapshot_id"]]
    else:
        current, reviewed, approving = {"id": None, "label": "none (no checkpoint yet)"}, False, []
    return {
        "flow": "implement", "run_id": state["run_id"], "status": state["status"],
        "pending_question": ((state["escalation"] or {}).get("detail") or {}).get("kind"),
        "created_at": state["created_at"], "finished_at": state["finished_at"],
        "review_count": len(attempts), "review_limit": state["review_limit"],
        "reviewers": [{"id": r["id"], "requested_model": r["model"], "requested_effort": r["effort"],
                       "timeout_seconds": r["timeout_seconds"]} for r in state["config"]["reviewers"]],
        "current_version": current, "latest_version_reviewed": reviewed, "approving_current": approving,
        "reviews": reviews, "human_decisions": decisions, "human_acceptance": acceptance}


def terminal_report(store, state, observed=(), observed_error=None):
    """Evidence at the moment a run ends; later edits are not covered.

    ``observed`` holds the prefilled question events (None: unreadable, with
    ``observed_error``). The review summary is built from the same workspace
    capture as ``final_snapshot_reviewed``.
    """
    project = store.project
    candidate = state.get("candidate") or {}
    actual = None
    capture_error = None
    try:
        # A removed or recreated workspace is reported, never captured.
        root = verify_workspace(project, state["workspace"], state["config"]["snapshot"]["mode"])
        actual = snapshot.capture(root,state["config"])["snapshot_id"]
    except (DevFlowError,OSError) as exc:
        # Cancellation must remain possible even if a new unsafe/oversized
        # file prevents capture. Unknown is never represented as a pass.
        capture_error = str(exc)
    matches = actual == candidate.get("snapshot_id") if actual and candidate else False if actual else None
    reviewed = bool(actual) and any(a["status"] == "succeeded" and a["snapshot_id"] == actual for a in state["attempts"])
    validation = state.get("validation") or {}
    read = lambda relative: store.record_json(state, relative)
    # Ending a run never depends on the summary: a summary that cannot be built is reported.
    try:
        review_summary = summary.build(summary_source(store, state, {"snapshot_id": actual, "reviewed": reviewed}), now())
        review_summary_error = None
    except (DevFlowError, OSError, ValueError, KeyError, TypeError) as exc:
        review_summary, review_summary_error = None, f"The review summary cannot be built: {exc}"
    try:
        acceptance, acceptance_error = human_decisions(state, read)[1], None
    except (DevFlowError, OSError, ValueError, KeyError, TypeError) as exc:
        acceptance, acceptance_error = None, f"The human decisions cannot be read: {exc}"
    warnings = [ACCEPTED_WARNING] if state["status"] == "accepted" else []
    return {"run_id":state["run_id"],"status":state["status"],"review_count":len(state["attempts"]),
        "review_limit":state["review_limit"],
        "snapshot_id":candidate.get("snapshot_id"),"all_reviewers_approved":bool(matches) and all_approved(state["config"]["reviewers"],state["approvals"],candidate["snapshot_id"]),
        "validation":state.get("validation"),"binary_attestation":state.get("binary_attestation"),
        "workspace_snapshot_id_at_report":actual,"workspace_matches_checkpoint_at_report":matches,
        "final_workspace_verified":bool(matches) and validation.get("snapshot_id") == actual and validation.get("status") in {"passed","human_attested"},
        "workspace_capture_error":capture_error,"final_snapshot_reviewed":reviewed,
        "unreviewed_final_changes":(actual != state["baseline_id"] and not reviewed) if actual else None,
        "workspace":state["workspace"],
        "reviews":review_rounds(state),
        "prefilled_question_events":None if observed is None else list(observed),
        "prefilled_question_events_error":observed_error,
        "human_acceptance":acceptance,"human_acceptance_error":acceptance_error,
        "warning":" ".join(warnings) or None,
        "review_summary":review_summary,"review_summary_error":review_summary_error,
        "reported_at":now(),"automatic_commit_push_deploy":False,
        "note":"This report describes its captured snapshot and time only. Later edits are not covered. Reviews/checks are not a correctness guarantee or permission to publish."}
