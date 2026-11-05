"""Plan workflow engine: the LLM never owns the counter, the order or approval."""

from __future__ import annotations

import difflib
import json
from pathlib import Path, PurePosixPath
import re
from uuid import uuid4

from ..common.errors import CaptureError, DevFlowError
from ..common.review import (LIMIT_DECISIONS, REVIEW_BUDGET, after_failed_review, all_approved, ask_at_limit, decide_at_limit,
                             next_reviewer)
from ..common.reviewers import preflight, recorded_cli_version, review_tokens, run_review
from ..common.engine import RunEngine
from ..common.runs import RUNNER_WAIT, TERMINAL, open_escalation
from ..common.sessions import approval_candidates
from ..common import summary
from ..common.storage import (atomic_json, check_json_size, checked_text, digest, json_bytes, no_symlinks, now,
                              read_json, within, write_new)
from .review import validate_review
from .transcript import capture_interview


def count_lines(before: str, after: str) -> tuple[int, int]:
    """Lines added and removed between two plan versions.

    The lines both versions share at the start and at the end are set aside first,
    as diff tools do, and the rest is compared with ``SequenceMatcher``. Its
    ``autojunk`` heuristic stays off: it would treat a plan's repeated lines as
    junk and count a one-line change in them as thousands.
    """
    old, new = before.splitlines(keepends=True), after.splitlines(keepends=True)
    start = 0
    while start < min(len(old), len(new)) and old[start] == new[start]:
        start += 1
    end = 0
    while end < min(len(old), len(new)) - start and old[-1 - end] == new[-1 - end]:
        end += 1
    old, new = old[start:len(old) - end], new[start:len(new) - end]
    added = removed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed


ACCEPTED_WARNING = ("Accepted by the human at the review limit without approval from every reviewer. "
                    "Inspect remaining findings and whether the final revision was reviewed.")


def _matches(path: Path, expected: str) -> bool:
    """Whether a published file still holds the recorded bytes (False when unreadable)."""
    try:
        no_symlinks(path)
        return path.is_file() and digest(path.read_bytes()) == expected
    except (DevFlowError, OSError):
        return False


def review_records(state: dict, read):
    """Each review round's record, in order: ``(number, relative, outcome, record)``.

    ``outcome`` is ``succeeded`` (``review.json``), ``failed`` (``error.json`` or
    ``recovery.json``) or ``in_progress`` (the reservation in flight; ``relative``
    and ``record`` are None). ``read(relative)`` reads one record.
    """
    for number in range(1, state["review_count"] + 1):
        base = f"reviews/{number:02d}"
        for name, outcome in (("review.json", "succeeded"), ("error.json", "failed"), ("recovery.json", "failed")):
            relative = f"{base}/{name}"
            if relative in state["artifacts"]:
                yield number, relative, outcome, read(relative)
                break
        else:
            yield number, None, "in_progress", None


def _version_label(number: object) -> str:
    return f"v{number:03d}" if type(number) is int else "unknown"


def human_decisions(state: dict, read) -> tuple[list[dict], dict | None]:
    """The human's answers during reviews (``common.summary`` shape), and the
    acceptance at the review limit when the human accepted the plan."""
    decisions, acceptance = [], None
    for item in state["resolved_escalations"]:
        question = read(f"escalations/{item['id']}.json")
        answer_path = f"escalations/{item['id']}-answer.json"
        answer = read(answer_path)
        decisions.append({"at": answer.get("created_at"), "review_number": item["review_number"],
                          "questions": question["questions"], "answer": answer["answer"],
                          "decision": answer.get("decision"), "interpretation": None, "event_ids": answer["event_ids"]})
        if answer.get("decision") == "accept":
            acceptance = {"event_ids": answer["event_ids"], "answer": answer["answer"], "version": answer.get("plan_hash"),
                          "accepted_at": answer.get("created_at"), "record": answer_path}
    return decisions, acceptance


def summary_source(store, state: dict) -> dict:
    """This plan run in the shape of ``common.summary`` (see that module)."""
    run = store.run_dir(state["run_id"])
    read = lambda relative: store.record_json(state, relative)
    reviews = []
    for number, relative, outcome, record in review_records(state, read):
        entry = {"number": number, "status": outcome,
                 "cli_version": recorded_cli_version(run / "review-processes" / f"{number:02d}"),
                 "cli_version_source": f"review-processes/{number:02d}/invocation.json", "verdict": None,
                 "error": None, "findings": [], "fix": None, "tokens": None, "record": None}
        if record is None:
            inflight = state["inflight"] or {}
            entry.update(reviewer_id=inflight.get("reviewer_id"), started_at=inflight.get("started_at"),
                         finished_at=None, version={"id": inflight.get("target"), "label": _version_label(state["plan_version"])})
            reviews.append(entry)
            continue
        entry.update(reviewer_id=record["provider"], started_at=record.get("started_at"),
                     finished_at=record.get("finished_at"), tokens=record.get("tokens"),
                     version={"id": record["plan_hash"], "label": _version_label(record.get("plan_version"))},
                     record=relative)
        if outcome == "failed":
            entry["error"] = record["message"]
        else:
            result = record["result"]
            reflected = f"reviews/{number:02d}/reflection.json"
            reflection = read(reflected) if reflected in state["artifacts"] else None
            dispositions = {item["finding_id"]: item for item in (reflection or {}).get("decisions", [])}
            entry["verdict"] = result["verdict"]
            entry["findings"] = [{"id": f["id"], "severity": f["severity"], "category": f["category"],
                                  "location": f["location"], "recommendation": f["recommendation"],
                                  "action": dispositions.get(f["id"], {}).get("action"),
                                  "reason": dispositions.get(f["id"], {}).get("reason")} for f in result["findings"]]
            if reflection is not None and result["verdict"] != "pass":
                change = None
                if reflection.get("changed"):
                    change = {"from": _version_label(reflection.get("previous_plan_version")),
                              "to": _version_label(reflection.get("plan_version")),
                              "lines_added": reflection.get("lines_added"), "lines_removed": reflection.get("lines_removed")}
                entry["fix"] = {"summary": reflection["summary"], "version_change": change, "file_changes": None}
        reviews.append(entry)
    decisions, acceptance = human_decisions(state, read)
    if state["plan_version"] is None:
        current, approving = {"id": None, "label": "none (no plan yet)"}, []
    else:
        current = {"id": state["plan_hash"], "label": _version_label(state["plan_version"])}
        approving = [item["provider"] for item in state["passes"] if item["plan_hash"] == state["plan_hash"]]
    return {
        "flow": "plan", "run_id": state["run_id"], "status": state["status"],
        "pending_question": ((state["escalation"] or {}).get("detail") or {}).get("kind"),
        "created_at": state["created_at"], "finished_at": state["finished_at"],
        "review_count": state["review_count"], "review_limit": state["review_limit"],
        "reviewers": [{"id": r["id"], "requested_model": r["model"], "requested_effort": r["effort"],
                       "timeout_seconds": r["timeout_seconds"]} for r in state["config"]["reviewers"]],
        "current_version": current, "latest_version_reviewed": bool(state["final_plan_reviewed"]),
        "approving_current": approving, "reviews": reviews, "human_decisions": decisions,
        "human_acceptance": acceptance}


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
        for number, _, outcome, record in review_records(state, lambda relative: read_json(run / relative)):
            if outcome == "succeeded":
                entry = {"number": number, "reviewer_id": record["provider"], "status": "succeeded",
                         "verdict": record["result"]["verdict"], "plan_hash": record["plan_hash"],
                         **record["activity"], **({"review": record["result"]} if with_reviews else {})}
            else:
                # A failed or recovered round has its record; the round in flight, its reservation.
                inflight = state["inflight"] or {}
                entry = {"number": number,
                         "reviewer_id": record["provider"] if record else inflight.get("reviewer_id"),
                         "status": "failed" if record else "dispatched",
                         "plan_hash": record["plan_hash"] if record else inflight.get("target"),
                         **((record or {}).get("activity") or {}), **({"review": None} if with_reviews else {})}
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
            if ask_at_limit(state, state["review_count"], approved, state["plan_hash"],
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
        # The time and the token counts of the attempt, whatever its outcome.
        attempt = {"started_at": reservation["started_at"], "finished_at": now(),
                   "tokens": review_tokens(provider, self._run(state["run_id"]) / "review-processes" / f"{number:02d}"),
                   "plan_version": state["plan_version"]}
        if error is not None:
            state["last_error"] = {"review_number": number, "provider": provider["id"],
                                   "plan_hash": reservation["target"], "message": str(error),
                                   "retry_consumes_another_attempt": True, **attempt,
                                   # What the reviewer did, when it ran to the end.
                                   **({"activity": result["activity"]} if result is not None else {})}
            state["status"] = after_failed_review(number, state, "ready")
            self._artifact(state, f"{relative_dir}/error.json", json_bytes(state["last_error"]))
            return
        providers = state["config"]["reviewers"]
        wrapper = {"schema_version": 2, "number": number, "provider": provider["id"], "plan_hash": state["plan_hash"],
                   "requested_model": provider["model"], "requested_effort": provider["effort"], "result": review,
                   "activity": result["activity"], **attempt}
        relative = f"{relative_dir}/review.json"
        self._artifact(state, relative, json_bytes(wrapper))
        state["last_review"] = relative
        state["final_plan_reviewed"] = True
        if review["verdict"] == "pass":
            state["passes"].append({"provider": provider["id"], "plan_hash": state["plan_hash"], "number": number})
            approved = {item["provider"]: item["plan_hash"] for item in state["passes"] if item["plan_hash"] == state["plan_hash"]}
            # At the review limit, the next review request asks the human.
            state["status"] = "completed" if all_approved(providers, approved, state["plan_hash"]) else "ready"
            self._artifact(state, f"{relative_dir}/reflection.json", json_bytes({
                "review_number": number, "summary": "No findings; plan unchanged.", "decisions": [],
                "plan_hash": state["plan_hash"], "previous_plan_hash": state["plan_hash"], "changed": False,
                "previous_plan_version": state["plan_version"], "plan_version": state["plan_version"],
                "lines_added": None, "lines_removed": None}))
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
            previous_version, previous_text = state["plan_version"], self.store.record(state, state["current_plan"]).decode("utf-8")
            changed = self._commit_plan(state, text)
            # Counted once, here; summaries read the numbers and never compare versions again.
            added, removed = count_lines(previous_text, text) if changed else (None, None)
            reflection.update({"plan_hash": state["plan_hash"], "previous_plan_hash": expected_hash, "changed": changed,
                               "previous_plan_version": previous_version, "plan_version": state["plan_version"],
                               "lines_added": added, "lines_removed": removed})
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
                    or (budget and value["decision"] not in LIMIT_DECISIONS)):
                raise DevFlowError("Human answer requires answer and actual recorded event_ids, and decision "
                                   "continue, stop or accept for the review limit question.")
            available = {item["event_id"]: item["answer_text"] for item in approval_candidates(state, self._events(state))}
            if len(set(value["event_ids"])) != len(value["event_ids"]) or any(not isinstance(i, str) or i not in available for i in value["event_ids"]):
                raise DevFlowError("Resolution must cite new successful AskUserQuestion responses after the pause; ordinary prompts are not approval.")
            expected = "\n".join(available[i] for i in value["event_ids"])
            if value["answer"] != expected:
                raise DevFlowError("Human answer must exactly match recorded answer_text; do not paraphrase or invent consent.")
            review_number = escalation["detail"]["review_number"]
            value.update({"escalation_id": escalation["id"], "review_number": review_number,
                          "questions": escalation["questions"], "created_at": now()})
            if budget:
                # The version the question was about; an accepted plan is this one.
                value["plan_hash"] = state["plan_hash"]
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
        """Export the approved bytes and their approval record, retaining an intent so
        interrupted writes resume.

        Review completion and publication are deliberately separate facts. A
        filesystem error cannot turn into a successful export or reopen reviews.
        The plan is published when every reviewer approved it (completed) or the
        human accepted it at the review limit (accepted); the approval record
        beside it says which. This method never commits to Git and never starts
        implementation.
        """
        with self._lock(run_id):
            state = self._state(run_id)
            self._verify(state)
            approvals = {item["provider"]: item["plan_hash"] for item in state["passes"]
                         if item["plan_hash"] == state["plan_hash"]}
            acceptance = human_decisions(state, lambda relative: self.store.record_json(state, relative))[1]
            reviewed = (state["status"] == "completed" and state["final_plan_reviewed"]
                        and all_approved(state["config"]["reviewers"], approvals, state["plan_hash"]))
            accepted = (state["status"] == "accepted" and acceptance is not None
                        and acceptance["version"] == state["plan_hash"])
            if not (reviewed or accepted):
                raise DevFlowError("Only the identical plan approved by both reviewers, or the plan the human "
                                   "accepted at the review limit, can be published.")
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
                    if publication.get("plan_hash") != state["plan_hash"] or "approval.md" not in state["artifacts"]:
                        raise DevFlowError("A publication intent belongs to a different plan hash.")
                    target = within(self.project / publication["path"], self.project)
                else:
                    directory = within(self.project / state["config"]["publish"]["directory"], self.project)
                    request = read_json(run / "request.json")["request"]
                    identifier = re.search(r"(?<![A-Za-z0-9_])[A-Z][A-Z0-9]{0,31}-[0-9]{1,12}(?![A-Za-z0-9_])", request)
                    name = identifier.group() if identifier else "plan-" + run_id
                    # The plan and its approval record take one name; neither file may exist.
                    for candidate in (name, name + "-" + run_id):
                        target = within(directory / (candidate + ".md"), self.project)
                        record = within(directory / (candidate + ".approval.md"), self.project)
                        if not target.exists() and not record.exists():
                            break
                    else:
                        raise DevFlowError("Both publication filenames (or their approval records) already exist; "
                                           "existing files were preserved.")
                    publication = {"status": "publishing", "path": target.relative_to(self.project).as_posix(),
                                   "approval_path": record.relative_to(self.project).as_posix(),
                                   "plan_hash": state["plan_hash"], "intent_at": now()}
                    state["publication"] = publication
                    # The approval record is made once, with the intent, before anything is written
                    # outside the run; a resumed publication writes the same bytes.
                    self._artifact(state, "approval.md", self._approval_record(state, publication, acceptance).encode("utf-8"))
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
                approval_target = within(self.project / publication["approval_path"], self.project)
                approval = self.store.record(state, "approval.md")
                no_symlinks(approval_target)
                if approval_target.exists():
                    if not approval_target.is_file() or approval_target.read_bytes() != approval:
                        raise DevFlowError("The approval record target differs from the recorded approval record; "
                                           "it was not overwritten.")
                else:
                    if "publication.json" in state["artifacts"]:
                        raise DevFlowError("The previously published approval record was removed; "
                                           "it was not silently recreated.")
                    write_new(approval_target, approval)
                no_symlinks(approval_target)
                if approval_target.read_bytes() != approval:
                    raise DevFlowError("The exported approval record changed during publication.")
                # Whether a receipt exists is decided by the committed records, never by a
                # file a stopped save may have left.
                if "publication.json" in state["artifacts"]:
                    receipt = read_json(run / "publication.json")
                    if (receipt.get("path") != publication["path"] or receipt.get("plan_hash") != state["plan_hash"]
                            or receipt.get("approval_path") != publication["approval_path"]):
                        raise DevFlowError("The existing publication receipt does not match this target.")
                else:
                    receipt = {"schema_version": 2, "run_id": run_id, "path": publication["path"],
                               "approval_path": publication["approval_path"], "plan_hash": state["plan_hash"],
                               # The time written in the approval record.
                               "published_at": publication["intent_at"],
                               "approval": "reviewers" if reviewed else "human"}
                    self._artifact(state, "publication.json", json_bytes(receipt))
                publication.update(status="published", published_at=receipt["published_at"])
                publication.pop("error", None)
                state["publication"] = publication
                self._save(state)
            except (DevFlowError, OSError, ValueError) as exc:
                # A state whose save may have failed is never saved again: the
                # error is recorded on the committed state.
                state = self._state(run_id)
                publication = dict(state.get("publication", {}), status="error", error=str(exc))
                state["publication"] = publication
                self._save(state)
                raise DevFlowError(f"Plan reviews completed, but publication failed: {exc}") from exc
        return self.status(run_id)

    def _approval_record(self, state: dict, publication: dict, acceptance: dict | None) -> str:
        """The approval record published beside the plan (fixed English Markdown)."""
        remaining = summary.build(summary_source(self.store, state), now())["remaining_findings"]["count"]
        if state["status"] == "accepted":
            approval = "human"
            approved_by = f"human (AskUserQuestion answer event {', '.join(acceptance['event_ids'])})"
        else:
            approval, approved_by = "reviewers", ", ".join(r["id"] for r in state["config"]["reviewers"])
        lines = ["# DevFlow plan approval record", "",
                 "This file records how the plan beside it was approved. The plan file is the exact approved "
                 "bytes and is not changed by DevFlow.", "",
                 f"- Plan: {PurePosixPath(publication['path']).name}",
                 f"- Approval: {approval}",
                 f"- Approved by: {approved_by}",
                 f"- Approved plan SHA-256: {state['plan_hash']}",
                 f"- Run ID: {state['run_id']}",
                 f"- Approved at: {state['finished_at']}",
                 f"- Published at: {publication['intent_at']}",
                 f"- Reviews: {state['review_count']} (limit {state['review_limit']})",
                 f"- Remaining findings: {remaining}",
                 f"- Last version reviewed: {'yes' if state['final_plan_reviewed'] else 'no'}"]
        if approval == "human":
            lines.append("- Note: accepted by the human at the review limit, not approved by every reviewer.")
        return "\n".join(lines) + "\n"

    def report(self, run_id: str) -> dict:
        state = self._state(run_id)
        self._verify(state)
        value = self.status(run_id)
        value.pop("review_summary_markdown", None)
        try:
            data = summary.build(summary_source(self.store, state), now())
            value.update(review_summary=data, review_summary_error=None)
        except (DevFlowError, OSError, ValueError, KeyError, TypeError) as exc:
            data = None
            value.update(review_summary=None, review_summary_error=f"The review summary cannot be built: {exc}")
        value["human_acceptance"] = human_decisions(state, lambda relative: self.store.record_json(state, relative))[1]
        value["converged"] = state["status"] == "completed"
        # Every warning that applies is kept, in this order, in the one warning string.
        warnings = []
        if state["status"] == "accepted":
            warnings.append(ACCEPTED_WARNING)
        elif not value["converged"]:
            warnings.append("Not a converged plan. Inspect remaining findings, errors, and whether the final revision was reviewed.")
        value["published_plan"] = None
        value["published_plan_matches_at_report"] = None
        value["published_approval"] = None
        value["published_approval_matches_at_report"] = None
        # A publication happened when its receipt is committed, even if a later retry
        # failed and set publication.status to error.
        if "publication.json" in state["artifacts"]:
            receipt = self.store.record_json(state, "publication.json")
            target = self.project / receipt["path"]
            approval = self.project / receipt["approval_path"]
            value["published_plan"] = str(target)
            value["published_approval"] = str(approval)
            value["published_plan_matches_at_report"] = _matches(target, state["plan_hash"])
            value["published_approval_matches_at_report"] = _matches(approval, state["artifacts"]["approval.md"])
            if not (value["published_plan_matches_at_report"] and value["published_approval_matches_at_report"]):
                warnings.append("The plan was published, but its current file or approval record is missing or "
                                "changed. Inspect before implementation.")
        elif state["status"] in {"completed", "accepted"} and state["config"]["publish"]["enabled"]:
            warnings.append("Reviews completed, but no successful publication has been recorded. Run plan publish "
                            "or resolve the reported error.")
        value["warning"] = " ".join(warnings) or None
        value["report_path"] = str(self._run(run_id) / "report.json")
        try:
            check_json_size("report.json", json_bytes(value))
        except DevFlowError:
            # The summary is never cut; the output below still shows it.
            value.update(review_summary=None, review_summary_error=summary.TOO_LARGE)
        atomic_json(self._run(run_id) / "report.json", value)
        if data is not None:
            # Shown, never written: the Markdown is rendered from the data each time.
            value.update(review_summary=data, review_summary_markdown=summary.render(data))
        return value
