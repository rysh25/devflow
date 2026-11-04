"""Plan workflow engine: the LLM never owns the counter, the order or approval."""

from __future__ import annotations

import json
from pathlib import Path
import re
from uuid import uuid4

from ..common.errors import CaptureError, DevFlowError
from ..common.review import (REVIEW_BUDGET, after_failed_review, all_approved, ask_at_limit, decide_at_limit,
                             next_reviewer)
from ..common.reviewers import preflight, run_review
from ..common.engine import RunEngine
from ..common.runs import RUNNER_WAIT, TERMINAL, open_escalation
from ..common.sessions import approval_candidates
from ..common.storage import atomic_json, checked_text, digest, json_bytes, no_symlinks, now, read_json, within, write_new
from .review import validate_review
from .transcript import capture_interview


class Engine(RunEngine):
    def __init__(self, project: Path, resources: Path | None = None, lock_wait: float = RUNNER_WAIT,
                 session: str | None = None):
        from .workflow import WORKFLOW
        super().__init__(project, WORKFLOW, resources, lock_wait, session)

    def _before_dispatch(self, state):
        state["last_error"] = None

    def _status_details(self, state: dict, result: dict) -> None:
        result.pop("transcript_marker", None)
        run = self._run(state["run_id"])
        result["current_plan"] = str(run / state["current_plan"]) if state["current_plan"] else None
        if state["last_review"]:
            result["review"] = read_json(run / state["last_review"])
        result["reviews"] = self._reviews(state)

    def _reviews(self, state: dict, with_reviews: bool = False) -> list[dict]:
        """Each review round, from its records: the reviewer, the plan hash it reviewed, the
        outcome, the tool calls when the reviewer ran to the end, and the verdict when the
        round succeeded (and with ``with_reviews`` the review)."""
        run, entries = self._run(state["run_id"]), []
        for number in range(1, state["review_count"] + 1):
            base = f"reviews/{number:02d}"
            if f"{base}/review.json" in state["artifacts"]:
                wrapper = read_json(run / base / "review.json")
                entry = {"number": number, "reviewer_id": wrapper["provider"], "status": "succeeded",
                         "verdict": wrapper["result"]["verdict"], "plan_hash": wrapper["plan_hash"],
                         **wrapper["activity"], **({"review": wrapper["result"]} if with_reviews else {})}
            else:
                # A failed or recovered round has its record; the round in flight, its reservation.
                ended = next((read_json(run / base / name) for name in ("error.json", "recovery.json")
                              if f"{base}/{name}" in state["artifacts"]), None)
                inflight = state["inflight"] or {}
                entry = {"number": number,
                         "reviewer_id": ended["provider"] if ended else inflight.get("reviewer_id"),
                         "status": "failed" if ended else "dispatched",
                         "plan_hash": ended["plan_hash"] if ended else inflight.get("target"),
                         **((ended or {}).get("activity") or {}), **({"review": None} if with_reviews else {})}
            entries.append(entry)
        return entries

    def _draft(self, run_id: str, path: Path) -> Path:
        if not path.is_absolute():
            path = self.project / path
        return within(path, self._run(run_id) / "draft")

    def seal(self, run_id: str, decisions_file: Path) -> dict:
        with self._lock(run_id):
            state = self._state(run_id)
            self._verify(state)
            if state["status"] != "interview":
                raise DevFlowError("Interview can be sealed exactly once, while status is interview.")
            decisions = read_json(self._draft(run_id, decisions_file))
            expected = {"summary", "decisions", "constraints", "open_questions"}
            if set(decisions) != expected or not isinstance(decisions["summary"], str):
                raise DevFlowError("Decisions require summary, decisions, constraints, open_questions.")
            for key in expected - {"summary"}:
                if not isinstance(decisions[key], list) or any(not isinstance(x, str) for x in decisions[key]):
                    raise DevFlowError(f"decisions.{key} must be an array of strings.")
            if decisions["open_questions"]:
                raise DevFlowError("Resolve initial human questions before sealing the interview.")
            try:
                interview = capture_interview(state["transcript_marker"], [event["payload"] for event in self._events(state)])
            except CaptureError as exc:
                raise DevFlowError(f"Cannot certify complete interview capture: {exc}. Do not replace it with a summary.") from exc
            if interview.get("coverage", {}).get("human_response_count", 0) < 1:
                raise DevFlowError("At least one recorded human answer is required after the original request.")
            self._artifact(state, "interview.json", json_bytes(interview))
            self._artifact(state, "decisions.json", json_bytes(decisions))
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
                raise DevFlowError("Initial plan requires a sealed interview and status planning.")
            self._commit_plan(state, checked_text(self._draft(run_id, plan_file)))
            state["status"] = "ready"
            self._save(state)
        return self.status(run_id)

    def _packet(self, state: dict, number: int, provider: dict) -> bytes:
        """Review data only: the reviewer reads the project itself, from its path."""
        run = self._run(state["run_id"])
        data = {
            "schema_version": 2, "run_id": state["run_id"], "review_number": number,
            "reviewer": {k: provider[k] for k in ("id", "model", "effort")},
            "project": str(self.project),
            "plan_sha256": state["plan_hash"],
            "original_request": read_json(run / "request.json"),
            "interview_verbatim": read_json(run / "interview.json"),
            "decisions_and_constraints": read_json(run / "decisions.json"),
            "plan_markdown": checked_text(run / state["current_plan"]),
            # Each earlier round in full, as in implement: the findings, their evidence and the verdict.
            "previous_reviews": [{key: entry.get(key) for key in ("number", "reviewer_id", "status", "plan_hash", "review")}
                                 for entry in self._reviews(state, with_reviews=True)],
            "revision_history": [], "human_resolutions": [],
        }
        for relative in sorted(state["artifacts"]):
            if re.fullmatch(r"reviews/\d{2}/reflection.json", relative):
                data["revision_history"].append(read_json(run / relative))
            if re.fullmatch(r"escalations/[a-f0-9]+-answer.json", relative):
                data["human_resolutions"].append(read_json(run / relative))
        # Review data only: common.reviewers adds the instructions exactly once.
        packet = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if len(packet) > state["config"]["max_packet_bytes"]:
            raise DevFlowError("Review packet exceeds configured limit. Escalate; do not silently truncate verbatim conversation.")
        return packet

    def review(self, run_id: str) -> dict:
        state = self._state(run_id)
        if state["status"] != "ready":
            raise DevFlowError(f"Review requires status ready, not {state['status']}.")
        # At the review limit no reviewer starts; the request asks the human instead.
        provider = (next_reviewer(state["config"]["reviewers"], state["review_count"], state["review_limit"])
                    if state["review_count"] < state["review_limit"] else None)
        # A missing or unsupported reviewer CLI must not consume an attempt.
        probe = preflight(provider) if provider else None

        def prepare(state: dict):
            self._verify(state)
            if state["status"] != "ready":
                raise DevFlowError(f"Review requires status ready, not {state['status']}.")
            approved = {item["provider"] for item in state["passes"] if item["plan_hash"] == state["plan_hash"]}
            if ask_at_limit(state, state["review_count"], approved,
                            lambda reason, questions: self._escalate(
                                state, {"reason": reason, "questions": questions}, "ready", kind=REVIEW_BUDGET)):
                return None
            if next_reviewer(state["config"]["reviewers"], state["review_count"], state["review_limit"]) != provider:
                raise DevFlowError("The run changed during the reviewer check; inspect status and retry.")
            number = state["review_count"] + 1
            relative_dir = f"reviews/{number:02d}"
            packet_path = self._artifact(state, f"{relative_dir}/packet.md", self._packet(state, number, provider))
            state["review_count"] = number
            return number, state["plan_hash"], provider["id"], {"packet": packet_path, "dir": relative_dir,
                                                                "number": number}

        def execute(work):
            # As in implement: the process's input, logs and raw output go to
            # review-processes/NN; reviews/NN keeps the run's own records.
            process_dir = self._run(run_id) / "review-processes" / f"{work['number']:02d}"
            return run_review(provider, work["packet"], process_dir, self.resources / "review.schema.json",
                              self.resources / "reviewer.md", validate_review, probe, cwd=self.project)

        return self._dispatch(run_id, "reviewing", "review", prepare, execute,
                              lambda state, reservation, work, result, error:
                              self._finish_review(state, reservation, work["dir"], provider, result, error))

    def _finish_review(self, state: dict, reservation: dict, relative_dir: str, provider: dict,
                       result: dict | None, error: BaseException | None) -> None:
        number = reservation["number"]
        review = result["review"] if error is None else None
        self._verify(state)
        if error is None and state["plan_hash"] != reservation["target"]:
            error = DevFlowError("The plan changed while it was being reviewed.")
        if error is not None:
            state["last_error"] = {"review_number": number, "provider": provider["id"],
                                   "plan_hash": reservation["target"], "message": str(error),
                                   "retry_consumes_another_attempt": True,
                                   # What the reviewer did, when it ran to the end.
                                   **({"activity": result["activity"]} if result is not None else {})}
            state["status"] = after_failed_review(number, state, "ready")
            self._artifact(state, f"{relative_dir}/error.json", json_bytes(state["last_error"]))
            return
        providers = state["config"]["reviewers"]
        wrapper = {"schema_version": 1, "number": number, "provider": provider["id"], "plan_hash": state["plan_hash"],
                   "requested_model": provider["model"], "requested_effort": provider["effort"], "result": review,
                   "activity": result["activity"]}
        relative = f"{relative_dir}/review.json"
        self._artifact(state, relative, json_bytes(wrapper))
        state["last_review"] = relative
        state["final_plan_reviewed"] = True
        if review["verdict"] == "pass":
            state["passes"].append({"provider": provider["id"], "plan_hash": state["plan_hash"], "number": number})
            approved = {item["provider"]: item["plan_hash"] for item in state["passes"] if item["plan_hash"] == state["plan_hash"]}
            # At the review limit, the next review request asks the human.
            state["status"] = "completed" if all_approved(providers, approved, state["plan_hash"]) else "ready"
            self._artifact(state, f"{relative_dir}/reflection.json", json_bytes({"review_number": number, "summary": "No findings; plan unchanged.", "decisions": [], "plan_hash": state["plan_hash"]}))
        elif review["verdict"] == "human_required":
            self._escalate(state, {"reason": review["summary"], "questions": [q["question"] for q in review["questions"]]}, "awaiting_revision")
        else:
            state["status"] = "awaiting_revision"

    def apply(self, run_id: str, plan_file: Path, decisions_file: Path, expected_hash: str) -> dict:
        with self._lock(run_id):
            state = self._state(run_id)
            self._verify(state)
            if state["status"] != "awaiting_revision" or expected_hash != state["plan_hash"]:
                raise DevFlowError("Reflection needs awaiting_revision and the exact reviewed plan hash.")
            review = read_json(self._run(run_id) / state["last_review"])
            if review["plan_hash"] != expected_hash:
                raise DevFlowError("Review does not refer to the current plan.")
            reflection = read_json(self._draft(run_id, decisions_file))
            if set(reflection) != {"review_number", "summary", "decisions"} or reflection["review_number"] != review["number"]:
                raise DevFlowError("Reflection must identify this review_number, summary, and decisions.")
            if not isinstance(reflection["summary"], str) or not reflection["summary"].strip() or not isinstance(reflection["decisions"], list):
                raise DevFlowError("Reflection summary and decisions are required.")
            findings = {f["id"]: f for f in review["result"]["findings"]}
            ids = set()
            requires_change = False
            has_human = any(x["review_number"] == review["number"] for x in state["resolved_escalations"])
            for item in reflection["decisions"]:
                if not isinstance(item, dict) or set(item) != {"finding_id", "action", "reason"}:
                    raise DevFlowError("Each decision needs finding_id, action, reason.")
                identifier = item["finding_id"]
                if identifier not in findings or identifier in ids:
                    raise DevFlowError("Every finding must be handled exactly once.")
                ids.add(identifier)
                if item["action"] not in {"accept", "reject", "already_addressed"} or not isinstance(item["reason"], str) or not item["reason"].strip():
                    raise DevFlowError("Use accept/reject/already_addressed with a concrete reason; defer requires escalation.")
                finding = findings[identifier]
                if (finding["requires_human"] or (finding["severity"] == "critical" and item["action"] != "accept")) and not has_human:
                    raise DevFlowError("This disposition requires a recorded human resolution. Use pause, then resolve.")
                requires_change |= item["action"] == "accept"
            if ids != set(findings):
                raise DevFlowError("Missing finding dispositions.")
            text = checked_text(self._draft(run_id, plan_file))
            if requires_change and digest(text.encode("utf-8")) == state["plan_hash"]:
                raise DevFlowError("Accepted changes require an actual changed plan, not only a ledger entry.")
            changed = self._commit_plan(state, text)
            reflection.update({"plan_hash": state["plan_hash"], "previous_plan_hash": expected_hash, "changed": changed})
            self._artifact(state, f"reviews/{review['number']:02d}/reflection.json", json_bytes(reflection))
            state["status"] = "ready"  # At the review limit, the next review request asks the human.
            self._save(state)
        return self.status(run_id)

    def _escalate(self, state: dict, reason: dict, return_status: str, kind: str | None = None) -> None:
        escalation_id = uuid4().hex
        value = open_escalation(state, escalation_id, reason["reason"], reason["questions"], return_status,
                                {"review_number": state["review_count"], **({"kind": kind} if kind else {})})
        self._artifact(state, f"escalations/{escalation_id}.json", json_bytes(value))
        state["status"] = "awaiting_human"

    def pause(self, run_id: str, reason_file: Path) -> dict:
        with self._lock(run_id):
            state = self._state(run_id)
            self._verify(state)
            if state["status"] in TERMINAL | {"reviewing", "awaiting_human"}:
                raise DevFlowError("Cannot open a second escalation or pause a terminal/in-flight run.")
            value = read_json(self._draft(run_id, reason_file))
            if set(value) != {"reason", "questions"} or not isinstance(value["reason"], str) or not value["reason"].strip() or not isinstance(value["questions"], list) or not value["questions"] or any(not isinstance(q, str) or not q.strip() for q in value["questions"]):
                raise DevFlowError("Escalation needs a nonempty reason and an array of questions.")
            self._escalate(state, value, state["status"])
            self._save(state)
        return self.status(run_id)

    def resolve(self, run_id: str, answer_file: Path, session: str | None = None) -> dict:
        with self._lock(run_id):
            state = self._state(run_id)
            self._verify(state)
            escalation = state["escalation"]
            if state["status"] != "awaiting_human" or not escalation:
                raise DevFlowError("There is no pending human question.")
            if session != state["owner_session"]:
                raise DevFlowError("Only the session that owns the run may resolve its human question.")
            value = read_json(self._draft(run_id, answer_file))
            # Only the review limit question also takes the human's decision.
            budget = escalation["detail"].get("kind") == REVIEW_BUDGET
            fields = {"answer", "event_ids", "decision"} if budget else {"answer", "event_ids"}
            if (not isinstance(value, dict) or set(value) != fields or not isinstance(value["answer"], str)
                    or not isinstance(value["event_ids"], list) or not value["event_ids"]
                    or (budget and value["decision"] not in ("continue", "stop"))):
                raise DevFlowError("Human answer requires answer and actual recorded event_ids, and decision "
                                   "continue or stop for the review limit question.")
            available = {item["event_id"]: item["answer_text"] for item in approval_candidates(state, self._events(state))}
            if len(set(value["event_ids"])) != len(value["event_ids"]) or any(not isinstance(i, str) or i not in available for i in value["event_ids"]):
                raise DevFlowError("Resolution must cite new successful AskUserQuestion responses after the pause; ordinary prompts are not approval.")
            expected = "\n".join(available[i] for i in value["event_ids"])
            if value["answer"] != expected:
                raise DevFlowError("Human answer must exactly match recorded answer_text; do not paraphrase or invent consent.")
            review_number = escalation["detail"]["review_number"]
            value.update({"escalation_id": escalation["id"], "review_number": review_number, "questions": escalation["questions"]})
            self._artifact(state, f"escalations/{escalation['id']}-answer.json", json_bytes(value))
            state["resolved_escalations"].append({"id": escalation["id"], "review_number": review_number})
            if budget:
                decide_at_limit(state, value["decision"], escalation["return_status"])
            else:
                state["status"] = escalation["return_status"]
            state["escalation"] = None
            self._save(state)
        return self.status(run_id)

    def recover(self, run_id: str, confirmed: bool = False, expect=None, *, expected_review: int | None = None) -> dict:
        """Explicit operator recovery of the exact interrupted review (human only)."""
        def exact(state: dict) -> None:
            if expected_review is None or state["review_count"] != expected_review:
                raise DevFlowError("Recovery requires the exact interrupted review number.")
            if expect is not None:
                expect(state)

        return super().recover(run_id, confirmed, exact)

    def cancel(self, run_id: str) -> dict:
        with self._lock(run_id):
            state = self._state(run_id)
            if state["status"] == "reviewing":
                raise DevFlowError("Stop/recover the in-flight reviewer before cancelling.")
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
                raise DevFlowError("Only the identical plan approved by both reviewers can be published.")
            if not state["config"]["publish"]["enabled"]:
                return self.status(run_id)
            publication = dict(state.get("publication", {}))
            try:
                run = self._run(run_id)
                source = run / state["current_plan"]
                no_symlinks(source)
                data = source.read_bytes()
                if digest(data) != state["plan_hash"]:
                    raise DevFlowError("The approved plan bytes changed; publication is forbidden.")
                if publication.get("path"):
                    if publication.get("plan_hash") != state["plan_hash"]:
                        raise DevFlowError("A publication intent belongs to a different plan hash.")
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
                        raise DevFlowError("Both publication filenames already exist; existing files were preserved.")
                    publication = {"status": "publishing", "path": target.relative_to(self.project).as_posix(),
                                   "plan_hash": state["plan_hash"], "intent_at": now()}
                    state["publication"] = publication
                    # Persist intent before writing outside the run directory.
                    self._save(state)
                no_symlinks(target)
                if target.exists():
                    if not target.is_file() or target.read_bytes() != data:
                        raise DevFlowError("The publication target differs from the approved plan; it was not overwritten.")
                else:
                    if "publication.json" in state["artifacts"]:
                        raise DevFlowError("The previously published plan was removed; it was not silently recreated.")
                    write_new(target, data)
                no_symlinks(target)
                if target.read_bytes() != data:
                    raise DevFlowError("The exported plan changed during publication.")
                receipt_path = run / "publication.json"
                if receipt_path.exists():
                    receipt = read_json(receipt_path)
                    if receipt.get("path") != publication["path"] or receipt.get("plan_hash") != state["plan_hash"]:
                        raise DevFlowError("The existing publication receipt does not match this target.")
                else:
                    receipt = {"schema_version": 1, "run_id": run_id, "path": publication["path"],
                               "plan_hash": state["plan_hash"], "published_at": now(), "approval": "reviewers"}
                    self._artifact(state, "publication.json", json_bytes(receipt))
                publication.update(status="published", published_at=receipt["published_at"])
                publication.pop("error", None)
                state["publication"] = publication
                self._save(state)
            except (DevFlowError, OSError, ValueError) as exc:
                publication.update(status="error", error=str(exc))
                state["publication"] = publication
                self._save(state)
                raise DevFlowError(f"Plan reviews completed, but publication failed: {exc}") from exc
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
            except (DevFlowError, OSError):
                value["published_plan_matches_at_report"] = False
            if not value["published_plan_matches_at_report"]:
                value["warning"] = "The plan was published, but its current file is missing or changed. Inspect before implementation."
        elif value["converged"] and state["config"]["publish"]["enabled"]:
            value["warning"] = "Reviews completed, but no successful publication has been recorded. Run plan publish or resolve the reported error."
        value["report_path"] = str(self._run(run_id) / "report.json")
        atomic_json(self._run(run_id) / "report.json", value)
        return value
