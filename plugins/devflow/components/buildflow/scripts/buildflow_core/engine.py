"""Bounded implementation/review state machine; only Claude edits project code."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from . import __version__, adapters, checks, snapshot
from .config import load_config
from .storage import (BuildFlowError, atomic_write_json, checked_text, digest, file_lock,
    json_bytes, no_symlinks, now, read_json, session_path, valid_run_id, within, write_new)
from ._vendor.flow_common.review import all_approved, next_reviewer

TERMINAL = {"completed", "exhausted", "cancelled"}
STATUSES = TERMINAL | {"implementing", "needs_revision", "ready_check", "checking", "ready_review", "reviewing", "awaiting_human", "paused_error"}


def _plan_bytes(path: Path, limit: int) -> bytes:
    """Read one bounded, stable UTF-8 input without newline/BOM rewriting."""
    no_symlinks(path)
    try:
        before = path.stat()
        if not path.is_file() or before.st_size > limit:
            raise BuildFlowError(f"Input plan must be a regular file no larger than {limit} bytes")
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        no_symlinks(path)
        after = path.stat()
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
        if (len(data) > limit or len(data) != after.st_size
                or any(getattr(before, name) != getattr(after, name) for name in fields)):
            raise BuildFlowError("Input plan changed while it was being read; start again after editing stops")
        text = data.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise BuildFlowError(f"Input plan must be readable UTF-8: {path}: {exc}") from exc
    if not text.lstrip("\ufeff").strip() or "\x00" in text:
        raise BuildFlowError("Input plan must be nonempty UTF-8 text without NUL")
    return data


class Engine:
    def __init__(self, project: Path, plugin_root: Path):
        self.project = project.resolve()
        if not self.project.is_dir():
            raise BuildFlowError("Project must be an existing explicit directory")
        self.plugin_root = plugin_root.resolve()
        self.root = self.project / ".devflow"
        no_symlinks(self.root)
        self._locked = set()

    def _prepare(self):
        for path in (self.root, self.root / "runs" / "implement", self.root / "sessions" / "implement"):
            no_symlinks(path)
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        ignore = self.root / ".gitignore"
        if not ignore.exists():
            write_new(ignore, b"# Private DevFlow records; do not publish.\n*\n")

    def _run(self, run_id):
        return within(self.root / "runs" / "implement" / valid_run_id(run_id), self.root)

    def _session(self, session_id):
        value = read_json(session_path(self.project, session_id))
        if value.get("session_id") != session_id:
            raise BuildFlowError("Session capture mismatch")
        return value

    @contextmanager
    def _lock(self, run_id):
        with file_lock(self._run(run_id) / "state.lock"):
            self._locked.add(run_id)
            try:
                yield
            finally:
                self._locked.discard(run_id)

    def _finish_transaction(self, run_id):
        run = self._run(run_id)
        journal_path = run / "transaction.json"
        if not journal_path.exists():
            return
        journal = read_json(journal_path, 256 * 1024 * 1024)
        desired = journal.get("state")
        if (journal.get("schema_version") != 1 or not isinstance(desired, dict)
                or desired.get("run_id") != run_id or desired.get("project") != str(self.project)
                or desired.get("state_revision") != journal.get("base_revision", -2) + 1
                or not isinstance(journal.get("writes"), dict)):
            raise BuildFlowError("Invalid pending transaction; preserve all records for human recovery")
        current = read_json(run / "state.json") if (run / "state.json").exists() else None
        if current:
            if current.get("state_revision") not in {journal["base_revision"], desired["state_revision"]}:
                raise BuildFlowError("Stale transaction would overwrite newer state")
            if current["state_revision"] == desired["state_revision"] and current != desired:
                raise BuildFlowError("Conflicting committed state")
        for relative, encoded in journal["writes"].items():
            target = within(run / relative, run)
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError) as exc:
                raise BuildFlowError("Invalid transaction bytes") from exc
            if digest(data) != desired.get("artifacts", {}).get(relative):
                raise BuildFlowError("Transaction artifact hash mismatch")
            if target.exists():
                if not target.is_file() or target.read_bytes() != data:
                    raise BuildFlowError(f"Divergent immutable artifact: {relative}")
            else:
                write_new(target, data)
        atomic_write_json(run / "state.json", desired)
        journal_path.unlink()

    def _state(self, run_id):
        run = self._run(run_id)
        if (run / "transaction.json").exists():
            if run_id in self._locked:
                self._finish_transaction(run_id)
            else:
                with self._lock(run_id):
                    self._finish_transaction(run_id)
        value = read_json(run / "state.json")
        if (value.get("schema_version") != 1 or value.get("run_id") != run_id
                or value.get("project") != str(self.project) or value.get("status") not in STATUSES
                or not isinstance(value.get("attempts"), list) or len(value["attempts"]) > 6):
            raise BuildFlowError("Incompatible/corrupt run state; never reset the review count")
        return value

    def _save(self, state):
        if state["status"] in TERMINAL:
            relative = f"reports/final-{state['state_revision'] + 1:06}.json"
            self._artifact(state, relative, json_bytes(self._report(state)))
            state["report_path"] = relative
        writes = state.pop("_pending_writes", {})
        desired = dict(state, updated_at=now(), state_revision=state["state_revision"] + 1)
        journal = self._run(state["run_id"]) / "transaction.json"
        if journal.exists():
            raise BuildFlowError("Recover the prior transaction before another mutation")
        atomic_write_json(journal, {"schema_version":1, "base_revision":state["state_revision"], "state":desired, "writes":writes})
        self._finish_transaction(state["run_id"])
        state.clear()
        state.update(desired)

    def _artifact(self, state, relative, data):
        target = within(self._run(state["run_id"]) / relative, self._run(state["run_id"]))
        if target.exists() and target.read_bytes() != data:
            raise BuildFlowError(f"Cannot overwrite immutable artifact: {relative}")
        state.setdefault("_pending_writes", {})[relative] = base64.b64encode(data).decode("ascii")
        state["artifacts"][relative] = digest(data)
        return target

    def _verify(self, state, source=True):
        run = self._run(state["run_id"])
        for relative, expected in state["artifacts"].items():
            path = within(run / relative, run)
            if not path.is_file() or digest(path.read_bytes()) != expected:
                raise BuildFlowError(f"Immutable artifact changed/missing: {relative}")
        if read_json(run / "config.json") != state["config"]:
            raise BuildFlowError("Run configuration changed; start a new run for new configuration")
        if source:
            path = Path(state["plan_source"])
            no_symlinks(path)
            if not path.is_file() or digest(path.read_bytes()) != state["source_digest"]:
                raise BuildFlowError("Original plan changed/missing. Resolve requirements and start a new run; do not silently replace it")

    def _owner(self, state, session_id):
        if not session_id or state["session_id"] != session_id:
            raise BuildFlowError("Use /devflow:resume RUN_ID in this session first")
        record = self._session(session_id)
        if record.get("active_run") != state["run_id"]:
            raise BuildFlowError("This session no longer owns this run")

    def _draft(self, state, path):
        path = Path(path)
        if not path.is_absolute():
            path = self.project / path
        return within(path, self._run(state["run_id"]) / "draft")

    def _require(self, state, allowed):
        if state["status"] not in allowed:
            raise BuildFlowError(f"Cannot perform this action in {state['status']}; expected {sorted(allowed)}")

    def _bind(self, state, session_id, old_session=None):
        if old_session and old_session != session_id:
            previous = self._session(old_session)
            if previous.get("active_run") == state["run_id"]:
                previous.update(active_run=None, intent=None)
                atomic_write_json(session_path(self.project, old_session), previous)
        record = self._session(session_id)
        record["active_run"] = state["run_id"]
        record["intent"] = None
        atomic_write_json(session_path(self.project, session_id), record)
        atomic_write_json(self.root / "active-implement.json", {"run_id":state["run_id"], "session_id":session_id})

    def _assert_no_other_live_run(self, selected=None):
        # active.json is a convenience pointer, not the source of truth. A crash
        # can occur between committing a run and binding its session/pointer.
        runs = self.root / "runs" / "implement"
        if not runs.exists():
            return
        for path in sorted(runs.iterdir()):
            no_symlinks(path)
            if not path.is_dir():
                continue
            identifier = valid_run_id(path.name)
            if identifier == selected:
                continue
            if not (path / "state.json").exists() and not (path / "transaction.json").exists():
                raise BuildFlowError(f"Incomplete run {identifier}; inspect and recover its records before starting another run")
            if self._state(identifier)["status"] not in TERMINAL:
                raise BuildFlowError(f"Another BuildFlow run is active ({identifier}); explicitly resume or cancel it")

    def _escalate(self, state, kind, questions, return_status, reason):
        event_seq = self._session(state["session_id"]).get("seq", 0)
        index = len(state["escalations"]) + 1
        value = {"id":f"E{index:03}", "kind":kind, "reason":reason, "questions":questions,
                 "return_status":return_status, "human_seq":event_seq, "session_id":state["session_id"],
                 "snapshot_id":(state.get("candidate") or {}).get("snapshot_id"), "created_at":now()}
        path = f"escalations/{index:03}.json"
        self._artifact(state, path, json_bytes(value))
        state["escalations"].append(path)
        state.update(status="awaiting_human", pending_escalation=value, approvals={})

    def start(self, session_id: str, plan_file: Path):
        self._prepare()
        record = self._session(session_id)
        intent = record.get("intent") or {}
        if intent.get("action") != "implement":
            raise BuildFlowError("Start only from an explicit /devflow:implement PLAN user command")
        supplied = str(intent.get("args", "")).strip()
        if len(supplied) >= 2 and supplied[0] == supplied[-1] and supplied[0] in "\"'":
            supplied = supplied[1:-1]
        supplied = supplied.removeprefix("@")
        intended = Path(supplied)
        if not intended.is_absolute():
            intended = self.project / intended
        plan = Path(plan_file)
        if not plan.is_absolute():
            plan = self.project / plan
        if plan.resolve() != intended.resolve():
            raise BuildFlowError("Plan path does not match the user's explicit start command")
        no_symlinks(plan)
        config = load_config(self.project, self.plugin_root)
        plan_data = _plan_bytes(plan, config["max_plan_bytes"])
        if plan.suffix.lower() not in {".md", ".markdown"}:
            raise BuildFlowError("The input plan must be a UTF-8 Markdown file")
        # No implementation may begin when the requested reviewer programs are unavailable.
        capabilities = [adapters.preflight(r) for r in config["reviewers"]]
        with file_lock(self.root / "implement-project.lock"):
            self._assert_no_other_live_run()
            pointer = self.root / "active-implement.json"
            if pointer.exists():
                old = self._state(read_json(pointer)["run_id"])
                if old["status"] not in TERMINAL:
                    raise BuildFlowError("Another BuildFlow run is active in this project; explicitly resume or cancel it")
            baseline = snapshot.capture(self.project, config)
            if _plan_bytes(plan, config["max_plan_bytes"]) != plan_data:
                raise BuildFlowError("Input plan changed during startup; no run was created. Confirm the plan and invoke implement again")
            run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:12]
            run = self._run(run_id)
            run.mkdir(mode=0o700)
            (run / "draft").mkdir(mode=0o700)
            state = {"schema_version":1, "engine_version":__version__, "flow":"implement", "state_revision":-1, "run_id":run_id,
                "project":str(self.project), "session_id":session_id, "status":"implementing",
                "plan_source":str(plan.resolve()), "source_digest":digest(plan_data), "config":config,
                "created_at":now(), "updated_at":now(), "artifacts":{}, "attempts":[], "approvals":{},
                "revision":0, "checkpoint_count":0, "check_attempts":0, "check_dispatch_count":0, "check_history":[], "escalations":[],
                "resolutions":[], "reflections":[], "candidate":None, "validation":None,
                "pending_escalation":None, "binary_attestation":None, "last_error":None,
                "baseline_id":baseline["snapshot_id"], "capabilities":capabilities}
            self._artifact(state, "plan.md", plan_data)
            self._artifact(state, "config.json", json_bytes(config))
            self._artifact(state, "baseline.json", json_bytes(baseline))
            self._artifact(state, "request.json", json_bytes({"intent":intent, "events":record.get("events", [])[-1:]}))
            if baseline.get("git", {}) and baseline["git"].get("dirty") and not config["allow_dirty"]:
                self._escalate(state,"dirty_worktree",["The worktree has existing changes. Preserve them and continue from this captured baseline?"],"implementing","Existing changes require explicit acknowledgement")
            self._save(state)
            self._bind(state, session_id)
        return self.status(run_id)

    def resume(self, run_id, session_id):
        # Lock ordering matches start: project, then run. Never steal the project
        # pointer from an unrelated live run, even when resuming a terminal run.
        with file_lock(self.root / "implement-project.lock"), self._lock(run_id):
            state = self._state(run_id)
            record = self._session(session_id)
            intent = record.get("intent") or {}
            if intent.get("action") != "resume" or str(intent.get("args", "")).strip() != run_id:
                raise BuildFlowError("Resume requires the user's explicit /devflow:resume RUN_ID command")
            self._assert_no_other_live_run(run_id)
            pointer = self.root / "active-implement.json"
            if pointer.exists():
                active_id = read_json(pointer)["run_id"]
                if active_id != run_id and self._state(active_id)["status"] not in TERMINAL:
                    raise BuildFlowError("Another BuildFlow run is active in this project; explicitly resume or cancel it first")
            bound = record.get("active_run")
            if bound and bound != run_id and self._state(bound)["status"] not in TERMINAL:
                raise BuildFlowError("This session is still bound to another active run")
            self._verify(state)
            if state["status"] in {"reviewing", "checking"}:
                raise BuildFlowError("A process may still be running; human recovery must confirm it stopped")
            old_session = state["session_id"]
            state["session_id"] = session_id
            if state["pending_escalation"] and old_session != session_id:
                # New question boundary; the resume command itself is not an answer.
                state["pending_escalation"] = dict(state["pending_escalation"], session_id=session_id, human_seq=record.get("seq",0))
            if state["status"] == "paused_error":
                state["status"] = state.pop("resume_status", "implementing")
                if len(state["attempts"]) >= state["config"]["max_reviews"]:
                    state["status"] = "exhausted"
            self._save(state)
            self._bind(state,session_id,old_session)
        return self.status(run_id)

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
            raise BuildFlowError("Create a checkpoint first")
        current = snapshot.capture(self.project,state["config"])
        if current["snapshot_id"] != candidate["snapshot_id"]:
            raise BuildFlowError("Code changed since checkpoint; create a fresh checkpoint and rerun checks")
        return current

    def check(self, run_id, session_id):
        with self._lock(run_id):
            state = self._state(run_id)
            self._owner(state,session_id)
            self._require(state,{"ready_check"})
            self._current(state)
            if state["check_attempts"] >= state["config"]["max_check_attempts"]:
                self._escalate(state,"check_budget",["Repeated checks did not converge. Review the failures before authorizing another bounded attempt."],"implementing","Check attempt limit reached")
                self._save(state)
                return self.status(run_id)
            state["check_attempts"] += 1
            state["check_dispatch_count"] += 1
            index = state["check_dispatch_count"]
            state.update(status="checking",last_error=None)
            self._save(state)
            try:
                result = checks.run_checks(self.project,state["config"]["checks"],self._run(run_id)/"check-processes"/f"{index:03}")
                self._current(state)
            except (BuildFlowError,OSError) as exc:
                state.update(status="paused_error",resume_status="implementing",last_error=str(exc),approvals={})
                self._save(state)
                return self.status(run_id)
            path = f"checks/{index:03}.json"
            self._artifact(state,path,json_bytes(result))
            state["check_history"].append(path)
            state["validation"] = {"snapshot_id":state["candidate"]["snapshot_id"],"kind":"automated","status":result["status"],"path":path}
            if result["status"] == "not_configured":
                self._escalate(state,"manual_validation",["No project checks are configured. Provide actual manual validation evidence for this exact code version, or cancel and configure checks for a new run."],"ready_review","No automated test pass can be claimed")
            elif result["status"] == "passed":
                state["status"] = "exhausted" if len(state["attempts"]) >= state["config"]["max_reviews"] else "ready_review"
            else:
                state.update(status="implementing",approvals={})
                if state["check_attempts"] >= state["config"]["max_check_attempts"]:
                    self._escalate(state,"check_budget",["Checks continue to fail. Determine the cause and authorize another bounded attempt, or cancel."],"implementing","Check attempt limit reached")
            self._save(state)
        return self.status(run_id)

    def _packet(self,state,current):
        run = self._run(state["run_id"])
        delta = read_json(run/state["candidate"]["diff"])
        context = {}
        for path in state["config"]["snapshot"]["context_paths"]:
            if path not in current["files"] or current["files"][path].get("binary"):
                raise BuildFlowError(f"Context path must name an included text file: {path}")
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
            raise BuildFlowError("Review packet exceeds configured limit. Do not silently truncate; narrow the plan/scope or explicitly configure a larger limit for a new run")
        return data

    def review(self,run_id,session_id):
        with self._lock(run_id):
            state = self._state(run_id)
            self._owner(state,session_id)
            self._require(state,{"ready_review"})
            current = self._current(state)
            validation = state.get("validation") or {}
            if validation.get("snapshot_id") != current["snapshot_id"] or validation.get("status") not in {"passed","human_attested"}:
                raise BuildFlowError("Fresh successful checks or explicit human validation are required")
            provider = next_reviewer(state["config"]["reviewers"],len(state["attempts"]),state["config"]["max_reviews"])
            adapters.preflight(provider)
            packet = self._packet(state,current)
            number = len(state["attempts"]) + 1
            packet_relative = f"reviews/{number:02}/packet.json"
            self._artifact(state,packet_relative,packet)
            attempt = {"number":number,"reviewer_id":provider["id"],"snapshot_id":current["snapshot_id"],
                "packet_path":packet_relative,"packet_sha256":digest(packet),"status":"dispatched","started_at":now()}
            state["attempts"].append(attempt)
            state.update(status="reviewing",last_error=None)
            self._save(state)  # Reserve BEFORE dispatch, including unknown-outcome failures.
            try:
                review = adapters.run_review(provider,self._run(run_id)/packet_relative,
                    self._run(run_id)/"review-processes"/f"{number:02}",self.plugin_root/"schemas"/"review.schema.json",self.plugin_root/"prompts"/"reviewer.md")
                adapters.validate_review(review)
                if review["snapshot_id"] != current["snapshot_id"]:
                    raise BuildFlowError("Reviewer response is for a different snapshot")
                self._current(state)
            except (BuildFlowError,OSError) as exc:
                state["attempts"][-1].update(status="failed",error=str(exc),finished_at=now())
                state.update(status="paused_error" if number < state["config"]["max_reviews"] else "exhausted",resume_status="ready_review",last_error=str(exc),approvals={})
                self._save(state)
                return self.status(run_id)
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
            self._save(state)
        return self.status(run_id)

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
                raise BuildFlowError("Reflection needs review_number, nonempty summary, and decisions")
            expected = {f["id"] for f in review["findings"]}
            seen = set()
            for item in value["decisions"]:
                if not isinstance(item,dict) or set(item) != {"finding_id","action","reason"} or not isinstance(item["finding_id"],str) or item["finding_id"] in seen or item["finding_id"] not in expected:
                    raise BuildFlowError("Every finding needs exactly one decision")
                seen.add(item["finding_id"])
                if not isinstance(item["action"],str) or item["action"] not in {"accept","reject","already_addressed","escalate"} or not isinstance(item["reason"],str) or not item["reason"].strip():
                    raise BuildFlowError("Invalid disposition or missing evidence/reason")
            if seen != expected:
                raise BuildFlowError("Missing finding dispositions")
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
                raise BuildFlowError("Escalation requires nonempty reason and question strings")
            self._escalate(state,"implementation_decision",value["questions"],state["status"],value["reason"])
            self._save(state)
        return self.status(run_id)

    def resolve(self,run_id,session_id,event_id,decision,reason):
        with self._lock(run_id):
            state = self._state(run_id)
            self._owner(state,session_id)
            self._require(state,{"awaiting_human"})
            esc = state["pending_escalation"]
            record = self._session(session_id)
            matches = [x for x in record.get("events",[]) if x.get("event_id") == event_id]
            if len(matches) != 1 or matches[0].get("seq",0) <= esc["human_seq"] or esc["session_id"] != session_id:
                raise BuildFlowError("Use an authentic human event captured AFTER this escalation")
            event = matches[0]
            if event.get("kind") != "ask_user_question":
                raise BuildFlowError("A workflow command or ordinary prompt is not a human answer; use a successful AskUserQuestion response after the escalation")
            if decision not in {"continue","cancel"} or not isinstance(reason,str) or not reason.strip():
                raise BuildFlowError("Supply continue/cancel and a faithful explanation of the human's decision")
            # Verify before queueing new immutable artifacts: _current checks
            # every committed artifact, not the next transaction's draft writes.
            if decision == "continue" and esc["kind"] in {"manual_validation", "binary_changes"}:
                self._current(state)
            else:
                self._verify(state)
            value = {"escalation_id":esc["id"],"decision":decision,"interpretation":reason,"human_event":event,"snapshot_id":esc.get("snapshot_id"),"created_at":now()}
            path = f"resolutions/{len(state['resolutions'])+1:03}.json"
            self._artifact(state,path,json_bytes(value))
            state["resolutions"].append(path)
            state.update(status="cancelled" if decision=="cancel" else esc["return_status"],pending_escalation=None,approvals={})
            if decision == "continue":
                if esc["kind"] == "manual_validation":
                    state["validation"] = {"snapshot_id":state["candidate"]["snapshot_id"],"kind":"manual","status":"human_attested","resolution":path}
                    if len(state["attempts"]) >= state["config"]["max_reviews"]:
                        state["status"] = "exhausted"
                elif esc["kind"] == "binary_changes":
                    state["binary_attestation"] = {"snapshot_id":state["candidate"]["snapshot_id"],"resolution":path}
                elif esc["kind"] == "check_budget":
                    state["check_attempts"] = 0  # Explicit human authorization, never a review-budget reset.
            self._save(state)
        return self.status(run_id)

    def cancel(self,run_id,session_id,reason):
        with self._lock(run_id):
            state = self._state(run_id)
            self._owner(state,session_id)
            if state["status"] in {"reviewing","checking"}:
                raise BuildFlowError("Confirm active child processes stopped before cancellation")
            state.update(status="cancelled",last_error=reason,approvals={})
            self._save(state)
        return self.status(run_id)

    def recover(self,run_id,confirmed=False):
        if not confirmed:
            raise BuildFlowError("Human must confirm all associated processes stopped")
        with self._lock(run_id):
            state = self._state(run_id)
            self._require(state,{"reviewing","checking"})
            if state["status"] == "reviewing":
                state["attempts"][-1].update(status="failed",error="Human-confirmed interrupted dispatch; outcome unknown",finished_at=now())
                resume = "ready_review"
            else:
                resume = "implementing"
            state.update(status="paused_error" if len(state["attempts"]) < state["config"]["max_reviews"] else "exhausted",resume_status=resume,approvals={},last_error="Recovered interrupted operation; no automatic retry")
            self._save(state)
        return self.status(run_id)

    def status(self,run_id):
        state = self._state(run_id)
        result = dict(state)
        result.pop("artifacts",None)
        result["run_dir"] = str(self._run(run_id))
        result["draft_dir"] = str(self._run(run_id)/"draft")
        result["review_count"] = len(state["attempts"])
        result["remaining_reviews"] = state["config"]["max_reviews"] - len(state["attempts"])
        result["next_reviewer"] = state["config"]["reviewers"][len(state["attempts"]) % 2]["id"] if result["remaining_reviews"] and state["status"] not in TERMINAL else None
        session = self._session(state["session_id"])
        boundary = (state.get("pending_escalation") or {}).get("human_seq",session.get("seq",0))
        result["human_events"] = [e for e in session.get("events",[]) if e.get("seq",0)>boundary]
        if state["status"] in TERMINAL:
            report_path = within(self._run(run_id) / state["report_path"], self._run(run_id))
            if digest(report_path.read_bytes()) != state["artifacts"][state["report_path"]]:
                raise BuildFlowError("The recorded terminal report was modified")
            result["report"] = read_json(report_path)
        return result

    def _report(self,state):
        candidate = state.get("candidate") or {}
        actual = None
        capture_error = None
        try:
            actual = snapshot.capture(self.project,state["config"])["snapshot_id"]
        except (BuildFlowError,OSError) as exc:
            # Cancellation must remain possible even if a new unsafe/oversized
            # file prevents capture. Unknown is never represented as a pass.
            capture_error = str(exc)
        matches = actual == candidate.get("snapshot_id") if actual and candidate else False if actual else None
        reviewed = bool(actual) and any(a["status"] == "succeeded" and a["snapshot_id"] == actual for a in state["attempts"])
        validation = state.get("validation") or {}
        return {"run_id":state["run_id"],"status":state["status"],"review_count":len(state["attempts"]),
            "snapshot_id":candidate.get("snapshot_id"),"both_reviewers_approved":bool(matches) and all_approved(state["config"]["reviewers"],state["approvals"],candidate["snapshot_id"]),
            "validation":state.get("validation"),"binary_attestation":state.get("binary_attestation"),
            "workspace_snapshot_id_at_report":actual,"workspace_matches_checkpoint_at_report":matches,
            "final_workspace_verified":bool(matches) and validation.get("snapshot_id") == actual and validation.get("status") in {"passed","human_attested"},
            "workspace_capture_error":capture_error,"final_snapshot_reviewed":reviewed,
            "unreviewed_final_changes":(actual != state["baseline_id"] and not reviewed) if actual else None,
            "reported_at":now(),"automatic_commit_push_deploy":False,
            "note":"This report describes its captured snapshot and time only. Later edits are not covered. Reviews/checks are not a correctness guarantee or permission to publish."}
