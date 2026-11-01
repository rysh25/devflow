"""Implement workflow engine: only Claude edits project code; the runner keeps score."""
from __future__ import annotations

from pathlib import Path

from . import checks, snapshot
from .review import validate_review
from ..common.errors import DevFlowError
from ..common.review import all_approved, next_reviewer
from ..common.reviewers import preflight, run_review
from ..common.engine import RunEngine
from ..common.runs import RUNNER_WAIT, TERMINAL, open_escalation
from ..common.sessions import approval_candidates
from ..common.storage import checked_text, digest, json_bytes, no_symlinks, now, read_json, within

EDIT_STATUSES = frozenset({"implementing", "needs_revision"})


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
        state.update(status="awaiting_human", approvals={})

    def checkpoint(self, run_id, session_id, summary_file):
        with self._lock(run_id):
            state = self._state(run_id)
            self._owner(state,session_id)
            self._require(state,{"implementing","ready_check","ready_review"})
            self._verify(state)
            summary = checked_text(self._draft(state, summary_file), 256*1024)
            current = snapshot.capture(self.project,state["config"])
            delta = snapshot.compare(read_json(self._run(run_id)/"baseline.json"),current)
            number = state["checkpoint_count"] + 1
            base = f"snapshots/{number:03}"
            self._artifact(state,base+".json",json_bytes(current))
            self._artifact(state,base+"-diff.json",json_bytes(delta))
            self._artifact(state,base+"-summary.md",summary.encode("utf-8"))
            prior = (state.get("candidate") or {}).get("snapshot_id")
            if prior != current["snapshot_id"]:
                state["approvals"] = {}
                state["binary_attestation"] = None
            state.update(status="ready_check",checkpoint_count=number,validation=None,
                candidate={"snapshot_id":current["snapshot_id"],"path":base+".json","diff":base+"-diff.json","summary":base+"-summary.md"})
            if delta["binary_changes"] and not state.get("binary_attestation"):
                self._escalate(state,"binary_changes",["Review these binary changes independently and confirm whether their contents are acceptable: " + ", ".join(delta["binary_changes"])],"ready_check","Text-only reviewers cannot inspect binary content")
            self._save(state)
        return self.status(run_id)

    def _current(self, state):
        self._verify(state)
        candidate = state.get("candidate")
        if not candidate:
            raise DevFlowError("Create a checkpoint first")
        current = snapshot.capture(self.project,state["config"])
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
            # No lock is held while project code runs.
            return index, state["candidate"]["snapshot_id"], None, {"index": index, "checks": state["config"]["checks"]}

        def execute(work):
            return checks.run_checks(self.project, work["checks"], self._run(run_id) / "check-processes" / f"{work['index']:03}")

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
            state["status"] = "exhausted" if len(state["attempts"]) >= state["config"]["max_reviews"] else "ready_review"
        else:
            state.update(status="implementing",approvals={})
            if state["check_attempts"] >= state["config"]["max_check_attempts"]:
                self._escalate(state,"check_budget",["Checks continue to fail. Determine the cause and authorize another bounded attempt, or cancel."],"implementing","Check attempt limit reached")

    def _packet(self,state,current):
        run = self._run(state["run_id"])
        delta = read_json(run/state["candidate"]["diff"])
        context = {}
        for path in state["config"]["snapshot"]["context_paths"]:
            if path not in current["files"] or current["files"][path].get("binary"):
                raise DevFlowError(f"Context path must name an included text file: {path}")
            context[path] = current["files"][path]
        history = []
        for item in state["attempts"]:
            entry = {k:item[k] for k in ("number","reviewer_id","snapshot_id","status")}
            if item.get("review_path"):
                entry["review"] = read_json(run/item["review_path"])
            history.append(entry)
        value = {"schema_version":1,"kind":"code_review","snapshot_id":current["snapshot_id"],
            "plan":(run/"plan.md").read_bytes().decode("utf-8"),
            "plan_sha256":state["artifacts"]["plan.md"],"baseline_id":state["baseline_id"],
            "implementation_summary":checked_text(run/state["candidate"]["summary"]),"changes":delta,
            "context_files":context,"file_manifest":{p:{k:v[k] for k in ("sha256","size","mode","binary") if k in v} for p,v in current["files"].items()},
            "scope":{"mode":current["mode"],"excluded":current.get("excluded",[]),"limitations":current.get("coverage", "Only included files; ignored/excluded files are outside coverage")},
            "validation":state["validation"],"check_results":read_json(run/state["validation"]["path"]) if state["validation"].get("path") else None,
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
        provider = next_reviewer(state["config"]["reviewers"],len(state["attempts"]),state["config"]["max_reviews"])
        # A missing or unsupported reviewer CLI must not consume an attempt.
        probe = preflight(provider)

        def prepare(state):
            self._owner(state,session_id)
            self._require(state,{"ready_review"})
            current = self._current(state)
            validation = state.get("validation") or {}
            if validation.get("snapshot_id") != current["snapshot_id"] or validation.get("status") not in {"passed","human_attested"}:
                raise DevFlowError("Fresh successful checks or explicit human validation are required")
            if next_reviewer(state["config"]["reviewers"],len(state["attempts"]),state["config"]["max_reviews"]) != provider:
                raise DevFlowError("The run changed during the reviewer check; inspect status and retry")
            packet = self._packet(state,current)
            number = len(state["attempts"]) + 1
            packet_relative = f"reviews/{number:02}/packet.json"
            self._artifact(state,packet_relative,packet)
            state["attempts"].append({"number":number,"reviewer_id":provider["id"],"snapshot_id":current["snapshot_id"],
                "packet_path":packet_relative,"packet_sha256":digest(packet),"status":"dispatched","started_at":now()})
            # Reserved BEFORE dispatch, including unknown-outcome failures.
            return number, current["snapshot_id"], provider["id"], {"number": number, "packet": packet_relative}

        def execute(work):
            review = run_review(provider,self._run(run_id)/work["packet"],
                self._run(run_id)/"review-processes"/f"{work['number']:02}",self.resources/"review.schema.json",
                self.resources/"reviewer.md",validate_review,probe)
            return review

        def finish(state, reservation, work, review, error):
            if error is None and review["snapshot_id"] != reservation["target"]:
                error = DevFlowError("Reviewer response is for a different snapshot")
            self._finish_review(state, reservation, provider, review, error)

        return self._dispatch(run_id, "reviewing", "review", prepare, execute, finish)

    def _finish_review(self,state,reservation,provider,review,error):
        number = reservation["number"]
        current = None
        if error is None:
            try:
                current = self._current(state)
            except DevFlowError as exc:
                error = exc
        if error is not None:
            state["attempts"][-1].update(status="failed",error=str(error),finished_at=now())
            state.update(status="paused_error" if number < state["config"]["max_reviews"] else "exhausted",resume_status="ready_review",last_error=str(error),approvals={})
            return
        review_path = f"reviews/{number:02}/review.json"
        self._artifact(state,review_path,json_bytes(review))
        state["attempts"][-1].update(status="succeeded",review_path=review_path,finished_at=now())
        critical = any(f["severity"] == "critical" for f in review["findings"])
        if review["verdict"] == "human_required" or critical:
            questions = review["questions"] or ["A critical issue was reported. Confirm the safe scope and remediation before continuing."]
            self._escalate(state,"review_decision",questions,"needs_revision","Independent review requires a human decision")
        elif review["verdict"] == "changes_required":
            state.update(status="needs_revision",approvals={})
        else:
            state["approvals"][provider["id"]] = current["snapshot_id"]
            if all_approved(state["config"]["reviewers"],state["approvals"],current["snapshot_id"]):
                state["status"] = "completed"
            else:
                state["status"] = "exhausted" if number >= state["config"]["max_reviews"] else "ready_review"

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
            if decision not in {"continue","cancel"} or not isinstance(reason,str) or not reason.strip():
                raise DevFlowError("Supply continue/cancel and a faithful explanation of the human's decision")
            kind = esc["detail"]["kind"]
            # Verify before queueing new immutable artifacts: _current checks
            # every committed artifact, not the next transaction's draft writes.
            if decision == "continue" and kind in {"manual_validation", "binary_changes"}:
                self._current(state)
            else:
                self._verify(state)
            value = {"escalation_id":esc["id"],"decision":decision,"interpretation":reason,"human_event":matches[0],
                     "snapshot_id":esc["detail"].get("snapshot_id"),"created_at":now()}
            path = f"resolutions/{len(state['resolutions'])+1:03}.json"
            self._artifact(state,path,json_bytes(value))
            state["resolutions"].append(path)
            state.update(status="cancelled" if decision=="cancel" else esc["return_status"],escalation=None,approvals={})
            if decision == "continue":
                if kind == "manual_validation":
                    state["validation"] = {"snapshot_id":state["candidate"]["snapshot_id"],"kind":"manual","status":"human_attested","resolution":path}
                    if len(state["attempts"]) >= state["config"]["max_reviews"]:
                        state["status"] = "exhausted"
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
            state.update(status="cancelled",last_error=reason,approvals={})
            self._save(state)
        return self.status(run_id)

    def _before_dispatch(self, state):
        state["last_error"] = None

    def _status_details(self, state, result):
        if state["status"] in TERMINAL:
            run = self._run(state["run_id"])
            report_path = within(run / state["report_path"], run)
            if digest(report_path.read_bytes()) != state["artifacts"][state["report_path"]]:
                raise DevFlowError("The recorded terminal report was modified")
            result["report"] = read_json(report_path)

def verify_source(state):
    """The original plan file must still be exactly the one this run started from."""
    path = Path(state["plan_source"])
    no_symlinks(path)
    if not path.is_file() or digest(path.read_bytes()) != state["source_digest"]:
        raise DevFlowError("Original plan changed/missing. Resolve requirements and start a new run; do not silently replace it")


def terminal_report(project, state):
    """Evidence at the moment a run ends; later edits are not covered."""
    candidate = state.get("candidate") or {}
    actual = None
    capture_error = None
    try:
        actual = snapshot.capture(project,state["config"])["snapshot_id"]
    except (DevFlowError,OSError) as exc:
        # Cancellation must remain possible even if a new unsafe/oversized
        # file prevents capture. Unknown is never represented as a pass.
        capture_error = str(exc)
    matches = actual == candidate.get("snapshot_id") if actual and candidate else False if actual else None
    reviewed = bool(actual) and any(a["status"] == "succeeded" and a["snapshot_id"] == actual for a in state["attempts"])
    validation = state.get("validation") or {}
    return {"run_id":state["run_id"],"status":state["status"],"review_count":len(state["attempts"]),
        "snapshot_id":candidate.get("snapshot_id"),"all_reviewers_approved":bool(matches) and all_approved(state["config"]["reviewers"],state["approvals"],candidate["snapshot_id"]),
        "validation":state.get("validation"),"binary_attestation":state.get("binary_attestation"),
        "workspace_snapshot_id_at_report":actual,"workspace_matches_checkpoint_at_report":matches,
        "final_workspace_verified":bool(matches) and validation.get("snapshot_id") == actual and validation.get("status") in {"passed","human_attested"},
        "workspace_capture_error":capture_error,"final_snapshot_reviewed":reviewed,
        "unreviewed_final_changes":(actual != state["baseline_id"] and not reviewed) if actual else None,
        "reported_at":now(),"automatic_commit_push_deploy":False,
        "note":"This report describes its captured snapshot and time only. Later edits are not covered. Reviews/checks are not a correctness guarantee or permission to publish."}
