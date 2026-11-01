"""Bounded state machine; the LLM never owns the counter or provider order."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from uuid import uuid4

from . import __version__
from .config import load_config
from .storage import (FlowError, atomic_json, checked_text, digest, file_lock,
                      json_bytes, no_symlinks, now, project_root, read_json,
                      valid_run_id, within, write_new)
from .transcript import CaptureError, capture_interview, human_answer_text, transcript_marker
from ._vendor.flow_common.review import all_approved, next_reviewer


TERMINAL = {"completed", "exhausted", "cancelled"}


class Engine:
    def __init__(self, project: Path, plugin_root: Path):
        self.project = project_root(project)
        self.plugin_root = plugin_root.resolve()
        self.root = self.project / ".devflow"
        self._held_locks: set[str] = set()
        no_symlinks(self.root)

    def _prepare(self) -> None:
        self.root.mkdir(mode=0o700, exist_ok=True)
        for relative in ("runs", "runs/plan", "sessions", "sessions/plan"):
            path = self.root / relative
            no_symlinks(path)
            path.mkdir(mode=0o700, exist_ok=True)
        ignore = self.root / ".gitignore"
        if not ignore.exists():
            # A run can contain customer requirements and private conversation.
            write_new(ignore, b"# DevFlow run data is private by default.\n*\n")

    def _run(self, run_id: str) -> Path:
        path = self.root / "runs" / "plan" / valid_run_id(run_id)
        no_symlinks(path)
        return path

    def _state(self, run_id: str) -> dict:
        transaction = self._run(run_id) / "transaction.json"
        if transaction.exists():
            if run_id in self._held_locks:
                self._finish_transaction(run_id)
            else:
                with self._lock(run_id):
                    self._finish_transaction(run_id)
        state = read_json(self._run(run_id) / "state.json")
        if state.get("schema_version") != 1 or state.get("run_id") != run_id:
            raise FlowError("Incompatible or corrupt run state; do not reset its review counter.")
        if state.get("project") != str(self.project):
            raise FlowError("Run belongs to a different project path. Move recovery requires human review.")
        return state

    @contextmanager
    def _lock(self, run_id: str):
        with file_lock(self._run(run_id) / "state.lock"):
            self._held_locks.add(run_id)
            try:
                yield
            finally:
                self._held_locks.discard(run_id)

    def _finish_transaction(self, run_id: str) -> None:
        """Replay only precommitted file/state writes, never model dispatches."""
        run = self._run(run_id)
        path = run / "transaction.json"
        if not path.exists():
            return
        journal = read_json(path, limit=128 * 1024 * 1024)
        desired = journal.get("state")
        if (journal.get("schema_version") != 1 or not isinstance(desired, dict)
                or desired.get("run_id") != run_id or desired.get("project") != str(self.project)
                or desired.get("state_revision") != journal.get("base_revision", -2) + 1
                or not isinstance(journal.get("writes"), dict)):
            raise FlowError("Invalid pending transaction. Preserve it and investigate; do not reset the run.")
        current = read_json(run / "state.json") if (run / "state.json").exists() else None
        if current is not None:
            revision = current.get("state_revision")
            if revision not in {journal["base_revision"], desired["state_revision"]}:
                raise FlowError("Pending transaction is stale; refusing to replace newer state.")
            if revision == desired["state_revision"] and current != desired:
                raise FlowError("Conflicting committed transaction state.")
        for relative, encoded in journal["writes"].items():
            target = within(run / relative, run)
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError) as exc:
                raise FlowError("Corrupt pending artifact bytes.") from exc
            if digest(data) != desired["artifacts"].get(relative):
                raise FlowError("Pending artifact does not match its committed hash.")
            if target.exists():
                if not target.is_file() or target.read_bytes() != data:
                    raise FlowError(f"Divergent artifact blocks recovery: {relative}. Never overwrite it.")
            else:
                write_new(target, data)
        atomic_json(run / "state.json", desired)
        path.unlink()

    def _save(self, state: dict) -> None:
        writes = state.pop("_pending_writes", {})
        base_revision = state["state_revision"]
        desired = dict(state, updated_at=now(), state_revision=base_revision + 1)
        path = self._run(state["run_id"]) / "transaction.json"
        if path.exists():
            raise FlowError("A prior transaction must recover before another mutation.")
        atomic_json(path, {"schema_version": 1, "base_revision": base_revision,
                           "state": desired, "writes": writes})
        self._finish_transaction(state["run_id"])
        state.clear()
        state.update(desired)

    def _artifact(self, state: dict, relative: str, data: bytes) -> Path:
        target = within(self._run(state["run_id"]) / relative, self._run(state["run_id"]))
        if target.exists() and (not target.is_file() or target.read_bytes() != data):
            raise FlowError(f"Refusing to overwrite a different immutable artifact: {relative}")
        state.setdefault("_pending_writes", {})[relative] = base64.b64encode(data).decode("ascii")
        state["artifacts"][relative] = digest(data)
        return target

    def _verify(self, state: dict) -> None:
        for relative, expected in state["artifacts"].items():
            path = within(self._run(state["run_id"]) / relative, self._run(state["run_id"]))
            if not path.is_file() or digest(path.read_bytes()) != expected:
                raise FlowError(f"Immutable artifact changed or is missing: {relative}. Stop and investigate.")
        if read_json(self._run(state["run_id"]) / "config.json") != state["config"]:
            raise FlowError("Run configuration changed; never replace frozen configuration during a run.")

    def _session_file(self, session: str) -> Path:
        if not isinstance(session, str) or not session.strip() or len(session) > 512:
            raise FlowError("A real Claude session ID is required.")
        return self.root / "sessions" / "plan" / (digest(session.encode("utf-8")) + ".json")

    def find_active(self, session: str) -> str | None:
        mapping = self._session_file(session)
        if not mapping.exists():
            return None
        record = read_json(mapping)
        run_id = record.get("run_id")
        if not record.get("guard_active", True):
            return None
        self._state(run_id)
        return run_id

    def session_run(self, session: str) -> str | None:
        mapping = self._session_file(session)
        return read_json(mapping).get("run_id") if mapping.exists() else None

    def release_session(self, session: str) -> None:
        mapping = self._session_file(session)
        if mapping.exists():
            record = read_json(mapping)
            record["guard_active"] = False
            atomic_json(mapping, record)

    def require_owner(self, run_id: str, session: str | None) -> None:
        if session is not None and self._state(run_id)["owner_session"] != session:
            raise FlowError("This session no longer owns the run. Use the explicit resume command.")

    def start(self, session: str, prompt: str, transcript_path: str | None) -> dict:
        if not isinstance(prompt, str) or not prompt.strip():
            raise FlowError("Start with /devflow:plan followed by the original request.")
        request = re.sub(r"^/devflow:plan(?:\s+|$)", "", prompt, count=1)
        if not request.strip():
            raise FlowError("The original request is empty. Add it after /devflow:plan.")
        self._prepare()
        with file_lock(self.root / "sessions" / "plan.lock"):
            active = self.find_active(session)
            if active and self._state(active)["status"] not in TERMINAL:
                raise FlowError("This session already has an active run. Resume or explicitly cancel it first.")
            config = load_config(self.project, self.plugin_root)
            marker = transcript_marker(transcript_path)
            run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:12]
            run = self._run(run_id)
            run.mkdir(mode=0o700)
            for name in ("draft", "events", "plans", "reviews", "escalations"):
                (run / name).mkdir(mode=0o700)
            state = {
                "schema_version": 1, "plugin_version": __version__, "flow": "plan", "run_id": run_id,
                "project": str(self.project), "owner_session": session,
                "created_at": now(), "updated_at": now(), "state_revision": 0,
                "status": "interview", "review_count": 0, "plan_version": None,
                "plan_hash": None, "current_plan": None, "passes": [],
                "config": config, "transcript_marker": marker, "event_ids": [],
                "artifacts": {}, "pending_escalation": None,
                "resolved_escalations": [], "last_error": None, "last_review": None,
                "final_plan_reviewed": False,
                "publication": {"status": "pending" if config["publish"]["enabled"] else "disabled"},
            }
            self._artifact(state, "request.json", json_bytes({
                "schema_version": 1, "raw_prompt": prompt, "request": request,
                "source": "Claude Code UserPromptSubmit", "session_id": session,
            }))
            self._artifact(state, "config.json", json_bytes(config))
            self._save(state)
            atomic_json(self._session_file(session), {"run_id": run_id, "guard_active": True})
        return self.status(run_id)

    def record_event(self, run_id: str, payload: dict) -> str:
        with self._lock(run_id):
            state = self._state(run_id)
            if payload.get("session_id") != state["owner_session"]:
                raise FlowError("This run was moved to another Claude session; stale-session events cannot become human evidence.")
            if payload.get("hook_event_name") == "PostToolUse" and payload.get("tool_name") == "AskUserQuestion":
                use_id = payload.get("tool_use_id")
                if (not isinstance(use_id, str) or not use_id.strip() or len(use_id) > 512
                        or not isinstance(payload.get("tool_input"), dict) or "tool_response" not in payload):
                    raise FlowError("Question capture requires its actual tool identity, input and result.")
                # Hook retries are delivery retries, not a new human answer.
                # The original event position remains before any later pause.
                for old in self._events(state):
                    if (old.get("hook_event_name") == "PostToolUse"
                            and old.get("tool_name") == "AskUserQuestion"
                            and old.get("session_id") == payload["session_id"]
                            and old.get("tool_use_id") == use_id):
                        if (old.get("tool_input") != payload["tool_input"]
                                or old.get("tool_response") != payload["tool_response"]
                                or bool(old.get("is_error")) != bool(payload.get("is_error"))):
                            raise FlowError("A captured question ID was replayed with different content.")
                        return old["planflow_event_id"]
            event_id = uuid4().hex
            value = dict(payload)
            value["planflow_event_id"] = event_id
            value["planflow_recorded_at"] = now()
            self._artifact(state, f"events/{event_id}.json", json_bytes(value))
            state["event_ids"].append(event_id)
            self._save(state)
        return event_id

    def _events(self, state: dict) -> list[dict]:
        return [read_json(self._run(state["run_id"]) / "events" / (item + ".json")) for item in state["event_ids"]]

    @staticmethod
    def _human_text(event: dict) -> str | None:
        # UserPromptSubmit can also originate from scheduled jobs and other
        # sessions. Preserve it as conversation evidence, never as approval.
        if event.get("hook_event_name") == "PostToolUse" and event.get("tool_name") == "AskUserQuestion":
            question = event.get("tool_input")
            use_id = event.get("tool_use_id")
            if (not isinstance(use_id, str) or not use_id.strip() or len(use_id) > 512
                    or not isinstance(question, dict) or question.get("answers")):
                return None
            value = event.get("tool_response")
            if not event.get("is_error"):
                try:
                    return human_answer_text(value)
                except (CaptureError, ValueError, TypeError):
                    return None
        return None

    def status(self, run_id: str) -> dict:
        state = self._state(run_id)
        result = dict(state)
        result.pop("transcript_marker", None)
        result.pop("artifacts", None)
        result.pop("event_ids", None)
        run = self._run(run_id)
        result["run_dir"] = str(run)
        result["draft_dir"] = str(run / "draft")
        result["current_plan"] = str(run / state["current_plan"]) if state["current_plan"] else None
        providers = state["config"]["reviewers"]
        result["next_reviewer"] = providers[state["review_count"] % len(providers)]["id"] if state["review_count"] < state["config"]["max_reviews"] and state["status"] not in TERMINAL else None
        result["human_events"] = []
        boundary = 0
        if state["pending_escalation"]:
            boundary = state["pending_escalation"]["event_boundary"]
        for event in self._events(state)[boundary:]:
            text = self._human_text(event)
            if text is not None:
                result["human_events"].append({"event_id": event["planflow_event_id"], "answer_text": text})
        result["human_events"] = result["human_events"][-10:]
        if state["last_review"]:
            result["review"] = read_json(run / state["last_review"])
        return result

    def _draft(self, run_id: str, path: Path) -> Path:
        if not path.is_absolute():
            path = self.project / path
        return within(path, self._run(run_id) / "draft")

    def seal(self, run_id: str, decisions_file: Path, context_file: Path | None = None) -> dict:
        with self._lock(run_id):
            state = self._state(run_id)
            self._verify(state)
            if state["status"] != "interview":
                raise FlowError("Interview can be sealed exactly once, while status is interview.")
            decisions = read_json(self._draft(run_id, decisions_file))
            expected = {"summary", "decisions", "constraints", "open_questions"}
            if set(decisions) != expected or not isinstance(decisions["summary"], str):
                raise FlowError("Decisions require summary, decisions, constraints, open_questions.")
            for key in expected - {"summary"}:
                if not isinstance(decisions[key], list) or any(not isinstance(x, str) for x in decisions[key]):
                    raise FlowError(f"decisions.{key} must be an array of strings.")
            if decisions["open_questions"]:
                raise FlowError("Resolve initial human questions before sealing the interview.")
            try:
                interview = capture_interview(state["transcript_marker"], self._events(state))
            except CaptureError as exc:
                raise FlowError(f"Cannot certify complete interview capture: {exc}. Do not replace it with a summary.") from exc
            if interview.get("coverage", {}).get("human_response_count", 0) < 1:
                raise FlowError("At least one recorded human answer is required after the original request.")
            context = checked_text(self._draft(run_id, context_file)) if context_file else None
            self._artifact(state, "interview.json", json_bytes(interview))
            self._artifact(state, "decisions.json", json_bytes(decisions))
            if context is not None:
                self._artifact(state, "context.md", context.encode("utf-8"))
            state["status"] = "planning"
            self._save(state)
        return self.status(run_id)

    def _commit_plan(self, state: dict, text: str) -> bool:
        value = text.encode("utf-8")
        fingerprint = digest(value)
        if fingerprint == state["plan_hash"]:
            return False
        version = 0 if state["plan_version"] is None else state["plan_version"] + 1
        relative = f"plans/plan-v{version:03d}.md"
        self._artifact(state, relative, value)
        state.update(plan_version=version, plan_hash=fingerprint, current_plan=relative, final_plan_reviewed=False)
        return True

    def plan(self, run_id: str, plan_file: Path) -> dict:
        with self._lock(run_id):
            state = self._state(run_id)
            self._verify(state)
            if state["status"] != "planning":
                raise FlowError("Initial plan requires a sealed interview and status planning.")
            self._commit_plan(state, checked_text(self._draft(run_id, plan_file)))
            state["status"] = "ready"
            self._save(state)
        return self.status(run_id)

    def _packet(self, state: dict, number: int, provider: dict) -> bytes:
        run = self._run(state["run_id"])
        data = {
            "schema_version": 1, "run_id": state["run_id"], "review_number": number,
            "reviewer": {k: provider[k] for k in ("id", "model", "effort")},
            "plan_sha256": state["plan_hash"],
            "original_request": read_json(run / "request.json"),
            "interview_verbatim": read_json(run / "interview.json"),
            "decisions_and_constraints": read_json(run / "decisions.json"),
            "plan_markdown": checked_text(run / state["current_plan"]),
            "revision_history": [], "human_resolutions": [],
            "repository_context": checked_text(run / "context.md") if "context.md" in state["artifacts"] else None,
        }
        for relative in sorted(state["artifacts"]):
            if re.fullmatch(r"reviews/\d{2}/reflection.json", relative):
                data["revision_history"].append(read_json(run / relative))
            if re.fullmatch(r"escalations/[a-f0-9]+-answer.json", relative):
                data["human_resolutions"].append(read_json(run / relative))
        instructions = checked_text(self.plugin_root / "prompts" / "reviewer.md")
        # A plain JSON data payload avoids interpolating requests into shell argv.
        packet = (instructions + "\n\nBEGIN UNTRUSTED REVIEW DATA (JSON)\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\nEND UNTRUSTED REVIEW DATA\n").encode("utf-8")
        if len(packet) > state["config"]["max_packet_bytes"]:
            raise FlowError("Review packet exceeds configured limit. Escalate; do not silently truncate verbatim conversation.")
        return packet

    def review(self, run_id: str) -> dict:
        from .adapters import run_review, validate_review
        with self._lock(run_id):
            state = self._state(run_id)
            self._verify(state)
            if state["status"] != "ready":
                raise FlowError(f"Review requires status ready, not {state['status']}.")
            if state["review_count"] >= state["config"]["max_reviews"]:
                raise FlowError("Review budget exhausted. A resume never resets this counter.")
            number = state["review_count"] + 1
            providers = state["config"]["reviewers"]
            provider = next_reviewer(providers, state["review_count"], state["config"]["max_reviews"])
            packet = self._packet(state, number, provider)
            relative_dir = f"reviews/{number:02d}"
            output_dir = self._run(run_id) / relative_dir
            output_dir.mkdir(mode=0o700, exist_ok=True)
            packet_path = self._artifact(state, f"{relative_dir}/packet.md", packet)
            state.update(status="reviewing", review_count=number, last_error=None)
            state["inflight"] = {"number": number, "provider": provider["id"], "plan_hash": state["plan_hash"], "started_at": now()}
            self._save(state)  # Reserve attempt BEFORE spawning; crashes cannot refund it.
        try:
            review = validate_review(run_review(provider, packet_path, self.plugin_root / "schemas" / "review.schema.json", output_dir, self.project))
        except Exception as exc:
            with self._lock(run_id):
                state = self._state(run_id)
                inflight = state.get("inflight", {})
                if state["status"] != "reviewing" or inflight.get("number") != number or inflight.get("provider") != provider["id"]:
                    # A late failure from a recovered invocation cannot demote a
                    # newer round. Keep evidence without mutating current state.
                    late = output_dir / ("late-error-" + uuid4().hex + ".json")
                    write_new(late, json_bytes({"review_number": number, "provider": provider["id"], "message": str(exc)}))
                    raise FlowError("A stale reviewer failed after recovery; current run state was left unchanged.") from exc
                state["last_error"] = {"review_number": number, "provider": provider["id"], "message": str(exc), "retry_consumes_another_attempt": True}
                state["status"] = "paused_error" if number < state["config"]["max_reviews"] else "exhausted"
                self._artifact(state, f"{relative_dir}/error.json", json_bytes(state["last_error"]))
                self._save(state)
            return self.status(run_id)
        with self._lock(run_id):
            state = self._state(run_id)
            self._verify(state)
            if state["status"] != "reviewing" or state["inflight"]["number"] != number or state["plan_hash"] != state["inflight"]["plan_hash"]:
                raise FlowError("Review result is stale; it must not be applied to another plan.")
            wrapper = {"schema_version": 1, "number": number, "provider": provider["id"], "plan_hash": state["plan_hash"], "requested_model": provider["model"], "requested_effort": provider["effort"], "result": review}
            relative = f"{relative_dir}/review.json"
            self._artifact(state, relative, json_bytes(wrapper))
            state["last_review"] = relative
            state["final_plan_reviewed"] = True
            if review["verdict"] == "pass":
                state["passes"].append({"provider": provider["id"], "plan_hash": state["plan_hash"], "number": number})
                approved = {item["provider"]: item["plan_hash"] for item in state["passes"] if item["plan_hash"] == state["plan_hash"]}
                if all_approved(providers, approved, state["plan_hash"]):
                    state["status"] = "completed"
                else:
                    state["status"] = "ready" if number < state["config"]["max_reviews"] else "exhausted"
                self._artifact(state, f"{relative_dir}/reflection.json", json_bytes({"review_number": number, "summary": "No findings; plan unchanged.", "decisions": [], "plan_hash": state["plan_hash"]}))
            elif review["verdict"] == "human_required":
                self._escalate(state, {"reason": review["summary"], "questions": [q["question"] for q in review["questions"]]}, "awaiting_revision")
            else:
                state["status"] = "awaiting_revision"
            self._save(state)
        return self.status(run_id)

    def apply(self, run_id: str, plan_file: Path, decisions_file: Path, expected_hash: str) -> dict:
        with self._lock(run_id):
            state = self._state(run_id)
            self._verify(state)
            if state["status"] != "awaiting_revision" or expected_hash != state["plan_hash"]:
                raise FlowError("Reflection needs awaiting_revision and the exact reviewed plan hash.")
            review = read_json(self._run(run_id) / state["last_review"])
            if review["plan_hash"] != expected_hash:
                raise FlowError("Review does not refer to the current plan.")
            reflection = read_json(self._draft(run_id, decisions_file))
            if set(reflection) != {"review_number", "summary", "decisions"} or reflection["review_number"] != review["number"]:
                raise FlowError("Reflection must identify this review_number, summary, and decisions.")
            if not isinstance(reflection["summary"], str) or not reflection["summary"].strip() or not isinstance(reflection["decisions"], list):
                raise FlowError("Reflection summary and decisions are required.")
            findings = {f["id"]: f for f in review["result"]["findings"]}
            ids = set()
            requires_change = False
            has_human = any(x["review_number"] == review["number"] for x in state["resolved_escalations"])
            for item in reflection["decisions"]:
                if not isinstance(item, dict) or set(item) != {"finding_id", "action", "reason"}:
                    raise FlowError("Each decision needs finding_id, action, reason.")
                identifier = item["finding_id"]
                if identifier not in findings or identifier in ids:
                    raise FlowError("Every finding must be handled exactly once.")
                ids.add(identifier)
                if item["action"] not in {"accept", "reject", "already_addressed"} or not isinstance(item["reason"], str) or not item["reason"].strip():
                    raise FlowError("Use accept/reject/already_addressed with a concrete reason; defer requires escalation.")
                finding = findings[identifier]
                if (finding["requires_human"] or (finding["severity"] == "critical" and item["action"] != "accept")) and not has_human:
                    raise FlowError("This disposition requires a recorded human resolution. Use pause, then resolve.")
                requires_change |= item["action"] == "accept"
            if ids != set(findings):
                raise FlowError("Missing finding dispositions.")
            text = checked_text(self._draft(run_id, plan_file))
            if requires_change and digest(text.encode("utf-8")) == state["plan_hash"]:
                raise FlowError("Accepted changes require an actual changed plan, not only a ledger entry.")
            changed = self._commit_plan(state, text)
            reflection.update({"plan_hash": state["plan_hash"], "previous_plan_hash": expected_hash, "changed": changed})
            self._artifact(state, f"reviews/{review['number']:02d}/reflection.json", json_bytes(reflection))
            state["status"] = "ready" if state["review_count"] < state["config"]["max_reviews"] else "exhausted"
            self._save(state)
        return self.status(run_id)

    def _escalate(self, state: dict, reason: dict, return_status: str) -> None:
        escalation_id = uuid4().hex
        value = {"id": escalation_id, "review_number": state["review_count"], "reason": reason["reason"], "questions": reason["questions"], "return_status": return_status, "event_boundary": len(state["event_ids"])}
        self._artifact(state, f"escalations/{escalation_id}.json", json_bytes(value))
        state["pending_escalation"] = value
        state["status"] = "awaiting_human"

    def pause(self, run_id: str, reason_file: Path) -> dict:
        with self._lock(run_id):
            state = self._state(run_id)
            self._verify(state)
            if state["status"] in TERMINAL | {"reviewing", "awaiting_human"}:
                raise FlowError("Cannot open a second escalation or pause a terminal/in-flight run.")
            value = read_json(self._draft(run_id, reason_file))
            if set(value) != {"reason", "questions"} or not isinstance(value["reason"], str) or not value["reason"].strip() or not isinstance(value["questions"], list) or not value["questions"] or any(not isinstance(q, str) or not q.strip() for q in value["questions"]):
                raise FlowError("Escalation needs a nonempty reason and an array of questions.")
            self._escalate(state, value, state["status"])
            self._save(state)
        return self.status(run_id)

    def resolve(self, run_id: str, answer_file: Path) -> dict:
        with self._lock(run_id):
            state = self._state(run_id)
            self._verify(state)
            escalation = state["pending_escalation"]
            if state["status"] != "awaiting_human" or not escalation:
                raise FlowError("There is no pending human question.")
            value = read_json(self._draft(run_id, answer_file))
            if set(value) != {"answer", "event_ids"} or not isinstance(value["answer"], str) or not isinstance(value["event_ids"], list) or not value["event_ids"]:
                raise FlowError("Human answer requires answer and actual recorded event_ids.")
            available = {e["planflow_event_id"]: self._human_text(e) for e in self._events(state)[escalation["event_boundary"]:]}
            if len(set(value["event_ids"])) != len(value["event_ids"]) or any(not isinstance(i, str) or not available.get(i) for i in value["event_ids"]):
                raise FlowError("Resolution must cite new successful AskUserQuestion responses after the pause; ordinary prompts are not approval.")
            expected = "\n".join(available[i] for i in value["event_ids"])
            if value["answer"] != expected:
                raise FlowError("Human answer must exactly match recorded answer_text; do not paraphrase or invent consent.")
            value.update({"escalation_id": escalation["id"], "review_number": escalation["review_number"], "questions": escalation["questions"]})
            self._artifact(state, f"escalations/{escalation['id']}-answer.json", json_bytes(value))
            state["resolved_escalations"].append({"id": escalation["id"], "review_number": escalation["review_number"]})
            state["status"] = escalation["return_status"]
            state["pending_escalation"] = None
            self._save(state)
        return self.status(run_id)

    def resume(self, run_id: str, session: str) -> dict:
        self._prepare()
        with file_lock(self.root / "sessions" / "plan.lock"), self._lock(run_id):
            state = self._state(run_id)
            self._verify(state)
            active = self.find_active(session)
            if active and active != run_id and self._state(active)["status"] not in TERMINAL:
                raise FlowError("This session already owns another active run.")
            if state["status"] in TERMINAL:
                state["owner_session"] = session
                self._save(state)
                atomic_json(self._session_file(session), {"run_id": run_id, "guard_active": True})
                return self.status(run_id)
            if state["status"] == "reviewing":
                raise FlowError("A review is still marked in-flight. Confirm its process stopped, then use recover from your own terminal; do not dispatch a duplicate.")
            if state["status"] == "interview" and state["owner_session"] != session:
                raise FlowError("An unsealed interview must resume its original Claude session so its transcript remains complete.")
            state["owner_session"] = session
            if state["status"] == "paused_error":
                state["status"] = "ready" if state["review_count"] < state["config"]["max_reviews"] else "exhausted"
            self._save(state)
            atomic_json(self._session_file(session), {"run_id": run_id, "guard_active": True})
        return self.status(run_id)

    def recover(self, run_id: str, expected_review: int) -> dict:
        """Explicit operator recovery; the hook guard never allows this command."""
        with self._lock(run_id):
            state = self._state(run_id)
            self._verify(state)
            if state["status"] != "reviewing" or state["review_count"] != expected_review:
                raise FlowError("Recovery requires the exact interrupted review number.")
            state["status"] = "paused_error" if expected_review < state["config"]["max_reviews"] else "exhausted"
            state["last_error"] = {"review_number": expected_review, "message": "Operator confirmed the interrupted reviewer process is stopped; attempt remains consumed."}
            self._artifact(state, f"reviews/{expected_review:02d}/recovery.json", json_bytes(state["last_error"]))
            self._save(state)
        return self.status(run_id)

    def cancel(self, run_id: str) -> dict:
        with self._lock(run_id):
            state = self._state(run_id)
            if state["status"] == "reviewing":
                raise FlowError("Stop/recover the in-flight reviewer before cancelling.")
            if state["status"] not in TERMINAL:
                state["status"] = "cancelled"
                self._save(state)
        return self.status(run_id)

    def publish(self, run_id: str) -> dict:
        """Export reviewed bytes, retaining an intent so interrupted writes resume.

        Review completion and publication are deliberately separate facts. A
        filesystem error cannot turn into a successful export or reopen reviews.
        This method never commits to Git and never starts implementation.
        """
        with self._lock(run_id):
            state = self._state(run_id)
            self._verify(state)
            approvals = {item["provider"]: item["plan_hash"] for item in state["passes"]
                         if item["plan_hash"] == state["plan_hash"]}
            if (state["status"] != "completed" or not state["final_plan_reviewed"]
                    or not all_approved(state["config"]["reviewers"], approvals, state["plan_hash"])):
                raise FlowError("Only the identical plan approved by both reviewers can be published.")
            if not state["config"]["publish"]["enabled"]:
                return self.status(run_id)
            publication = dict(state.get("publication", {}))
            try:
                run = self._run(run_id)
                source = run / state["current_plan"]
                no_symlinks(source)
                data = source.read_bytes()
                if digest(data) != state["plan_hash"]:
                    raise FlowError("The approved plan bytes changed; publication is forbidden.")
                if publication.get("path"):
                    if publication.get("plan_hash") != state["plan_hash"]:
                        raise FlowError("A publication intent belongs to a different plan hash.")
                    target = within(self.project / publication["path"], self.project)
                else:
                    directory = within(self.project / state["config"]["publish"]["directory"], self.project)
                    request = read_json(run / "request.json")["request"]
                    identifier = re.search(r"(?<![A-Za-z0-9_])[A-Z][A-Z0-9]{0,31}-[0-9]{1,12}(?![A-Za-z0-9_])", request)
                    name = identifier.group() if identifier else "plan-" + run_id
                    target = within(directory / (name + ".md"), self.project)
                    if target.exists():
                        target = within(directory / (name + "-" + run_id + ".md"), self.project)
                    if target.exists():
                        raise FlowError("Both publication filenames already exist; existing files were preserved.")
                    publication = {"status": "publishing", "path": target.relative_to(self.project).as_posix(),
                                   "plan_hash": state["plan_hash"], "intent_at": now()}
                    state["publication"] = publication
                    # Persist intent before writing outside the run directory.
                    self._save(state)
                no_symlinks(target)
                if target.exists():
                    if not target.is_file() or target.read_bytes() != data:
                        raise FlowError("The publication target differs from the approved plan; it was not overwritten.")
                else:
                    if "publication.json" in state["artifacts"]:
                        raise FlowError("The previously published plan was removed; it was not silently recreated.")
                    write_new(target, data)
                no_symlinks(target)
                if target.read_bytes() != data:
                    raise FlowError("The exported plan changed during publication.")
                receipt_path = run / "publication.json"
                if receipt_path.exists():
                    receipt = read_json(receipt_path)
                    if receipt.get("path") != publication["path"] or receipt.get("plan_hash") != state["plan_hash"]:
                        raise FlowError("The existing publication receipt does not match this target.")
                else:
                    receipt = {"schema_version": 1, "run_id": run_id, "path": publication["path"],
                               "plan_hash": state["plan_hash"], "published_at": now(), "approval": "reviewers"}
                    self._artifact(state, "publication.json", json_bytes(receipt))
                publication.update(status="published", published_at=receipt["published_at"])
                publication.pop("error", None)
                state["publication"] = publication
                self._save(state)
            except (FlowError, OSError, ValueError) as exc:
                publication.update(status="error", error=str(exc))
                state["publication"] = publication
                self._save(state)
                raise FlowError(f"Plan reviews completed, but publication failed: {exc}") from exc
        return self.status(run_id)

    def report(self, run_id: str) -> dict:
        state = self._state(run_id)
        self._verify(state)
        value = self.status(run_id)
        value["converged"] = state["status"] == "completed"
        value["warning"] = None if value["converged"] else "Not a converged plan. Inspect remaining findings, errors, and whether the final revision was reviewed."
        publication = state.get("publication", {})
        value["published_plan"] = None
        value["published_plan_matches_at_report"] = None
        if publication.get("status") == "published":
            target = self.project / publication["path"]
            value["published_plan"] = str(target)
            try:
                no_symlinks(target)
                value["published_plan_matches_at_report"] = target.is_file() and digest(target.read_bytes()) == state["plan_hash"]
            except (FlowError, OSError):
                value["published_plan_matches_at_report"] = False
            if not value["published_plan_matches_at_report"]:
                value["warning"] = "The plan was published, but its current file is missing or changed. Inspect before implementation."
        elif value["converged"] and state["config"]["publish"]["enabled"]:
            value["warning"] = "Reviews completed, but no successful publication has been recorded. Run plan publish or resolve the reported error."
        value["report_path"] = str(self._run(run_id) / "report.json")
        atomic_json(self._run(run_id) / "report.json", value)
        return value
