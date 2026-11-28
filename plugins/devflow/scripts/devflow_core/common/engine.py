"""What every workflow engine shares: storage, ownership, status and external processes.

A workflow engine subclasses ``RunEngine`` and supplies only its transitions:
``_dispatch`` runs the reserve -> external process -> claim -> finish lifecycle,
``status`` builds the common view and asks ``_status_details`` for the rest.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from . import summary
from .errors import DevFlowError
from .review import (LIMIT_AUTO_KEY, REVIEW_BUDGET, auto_continue_fits, check_limit_auto, check_limit_auto_record,
                     decide_at_limit, limit_auto_allows, limit_auto_decisions, limit_auto_final, limit_auto_name,
                     limit_auto_record, limit_auto_records)
from .runs import RUNNER_WAIT, TERMINAL, RunStore
from .sessions import approval_evidence, prefilled_question_events
from .storage import json_bytes, now, read_json, writable_text


class RunEngine:
    def __init__(self, project: Path, workflow, resources: Path | None = None,
                 lock_wait: float = RUNNER_WAIT, session: str | None = None, *, host: str):
        self.project = Path(project).resolve()
        if not self.project.is_dir():
            raise DevFlowError("Project must be an existing directory")
        self.workflow = workflow
        self.resources = Path(resources).resolve() if resources else workflow.resource_dir
        self.store = RunStore(self.project, workflow, lock_wait, host=host)
        # When set, every locked change verifies that this session still owns
        # the run: ownership can move while the lock is not held.
        self.session = session
        self.host = host

    def _run(self, run_id: str) -> Path:
        return self.store.run_dir(run_id)

    def _lock(self, run_id: str):
        return self.store.lock(run_id)

    def _state(self, run_id: str, session: str | None = None) -> dict:
        """A display read, or (lock held) a change read that checks ownership."""
        if not self.store.holds(run_id):
            return self.store.read(run_id)
        state = self.store.load(run_id)
        if state["owner_host"] != self.host:
            raise DevFlowError("Cross-host mutation is unsupported; use the run's original host")
        owner = session if session is not None else self.session
        if owner is not None:
            self._require_owner(state, owner)
        return state

    def _require_owner(self, state: dict, session: str | None) -> None:
        if not session or (state["owner_host"], state["owner_session"]) != (self.host, session):
            raise DevFlowError("This host/session does not own the run; cross-host mutation is unsupported. "
                               "Use /devflow:resume RUN_ID in the original host first.")

    def require_owner(self, run_id: str, session: str | None) -> dict:
        state = self.store.read(run_id)
        self._require_owner(state, session)
        return state

    def _save(self, state: dict) -> None:
        self.store.save(state)

    def _artifact(self, state: dict, relative: str, data: bytes) -> Path:
        return self.store.stage(state, relative, data)

    def _events(self, state: dict) -> list[dict]:
        return self.store.events(state)

    def _check_frozen_config(self, state: dict) -> dict | None:
        config = self.workflow.frozen_config(state)
        if config is not None:
            stored = read_json(self._run(state["run_id"]) / "config.json")
            # A written true equals a frozen 1 (and 6.0 equals 6) in a dict
            # comparison: check the keys' types on both sides before comparing.
            check_limit_auto(config)
            check_limit_auto(stored)
            if stored != config:
                raise DevFlowError("Run configuration changed; never replace frozen configuration during a run.")
        return config

    def _verify(self, state: dict) -> None:
        self.store.verify_artifacts(state)
        self._check_frozen_config(state)

    # -- automatic decisions at the review limit -------------------------------------------
    def _limit_auto_request(self, state: dict, escalation_id: str, decision: str) -> tuple[dict, str, bool]:
        """The checks of ``limit-auto`` that read no records, in order; returns the
        question, the decision to apply and whether review_limit_auto_final may accept.

        The open question must be this review limit question, no human answer may
        be recorded after it (a human answer always comes first, whatever question
        it answers), the frozen setting must allow the decision, and an automatic
        continuation must stay within review_limit_auto_extra_reviews. Once another
        round would pass it, ``continue_accept`` with review_limit_auto_final
        ``accept`` makes the run eligible, and a continue becomes accept (the flow
        then records it as record 2).
        """
        escalation = state["escalation"] or {}
        if (state["status"] != "awaiting_human" or (escalation.get("detail") or {}).get("kind") != REVIEW_BUDGET
                or escalation.get("id") != escalation_id):
            raise DevFlowError("There is no pending review limit question with this ID; read status again")
        if approval_evidence(state, self._events(state))[0]:
            raise DevFlowError("A human answer is recorded after this question; ask the review limit question as usual")
        config = state["config"]
        if decision not in ("continue", "accept"):
            raise DevFlowError("limit-auto takes --decision continue or accept; ask the human for any other answer")
        if not limit_auto_allows(config, decision):
            raise DevFlowError(f'The run\'s review_limit_auto is "{config["review_limit_auto"]}"; it does not '
                               f"{decision} automatically. Ask the human the review limit question.")
        final_eligible = (not auto_continue_fits(state) and config[LIMIT_AUTO_KEY] == "continue_accept"
                          and limit_auto_final(config) == "accept")
        if decision == "continue" and not auto_continue_fits(state):
            if final_eligible:
                return escalation, "accept", True
            limit = state["review_limit"] + len(config["reviewers"])
            raise DevFlowError(f"Continuing automatically would make the limit {limit}, "
                               f"{limit - config['max_reviews']} over max_reviews {config['max_reviews']}, beyond "
                               f"review_limit_auto_extra_reviews {config['review_limit_auto_extra_reviews']}. "
                               "Ask the human the review limit question.")
        return escalation, decision, final_eligible

    def _apply_limit_auto(self, state: dict, escalation: dict, decision: str, recommendation: str,
                          version: str, version_reviewed: bool, session: str, recommended: str | None = None) -> None:
        """Record the automatic decision and apply it as the human's answer would be
        applied, without recording a human answer (the caller saves once).
        ``recommended`` marks an accept by review_limit_auto_final (record 2)."""
        record = limit_auto_record(state, escalation, decision, recommendation, version, version_reviewed, session,
                                   recommended)
        name = limit_auto_name(escalation["id"], decision)
        # The record passes the same check that presents it later, before anything changes.
        check_limit_auto_record(state, record, name, lambda relative: self.store.record_json(state, relative),
                                self.workflow)
        self._artifact(state, name, json_bytes(record))
        state["escalation"] = None
        decide_at_limit(state, decision, escalation["return_status"], accepted_status="auto_accepted")

    def _check_auto_acceptance(self, state: dict, entries: list[tuple[str, dict]]) -> None:
        """Verify an automatic accept record against the flow's accepted version (optional)."""

    def read_frozen_config(self, run_id: str) -> dict | None:
        """The run's frozen configuration (None if its workflow has none); changes nothing."""
        return self._check_frozen_config(self.store.read(run_id))

    # -- external processes -----------------------------------------------------------------
    def _dispatch(self, run_id: str, status: str, kind: str,
                  prepare: Callable[[dict], tuple[int, str | None, str | None, Any] | None],
                  execute: Callable[[Any], Any],
                  finish: Callable[[dict, dict, Any, Any, BaseException | None], None]) -> dict:
        """Reserve under the lock, run outside it, then finish the still-current reservation.

        ``prepare(state)`` returns ``(number, target, reviewer_id, work)``, or
        ``None`` when it handled the request without a process (the state is
        saved). Every failure of ``execute(work)`` is recorded, never retried.
        A result for a reservation that cancel or recover replaced is kept
        under ``late/`` and the run is left unchanged.
        """
        with self._lock(run_id):
            state = self._state(run_id)
            prepared = prepare(state)
            if prepared is not None:
                self._before_dispatch(state)
            if prepared is None:
                self._save(state)
                return self.status(run_id)
            number, target, reviewer_id, work = prepared
            reservation = self.store.reserve(state, status, kind, number, target, reviewer_id)
        try:
            result, error = execute(work), None
        except Exception as exc:  # noqa: BLE001 - recorded on the run, never retried
            result, error = None, exc
        with self._lock(run_id):
            state = self.store.claim(run_id, reservation, status,
                                     {"result": result, "error": None if error is None else writable_text(str(error))})
            finish(state, reservation, work, result, error)
            self._save(state)
        return self.status(run_id)

    def _before_dispatch(self, state: dict) -> None:
        """Workflow-specific changes made with every reservation (optional)."""

    def recover(self, run_id: str, confirmed: bool = False, expect: Callable[[dict], None] | None = None) -> dict:
        """Human-only: end an interrupted process after confirming it stopped."""
        if not confirmed:
            raise DevFlowError("Human must confirm all associated processes stopped")
        self.store.recover(run_id, expect)
        return self.status(run_id)

    # -- status ---------------------------------------------------------------------------
    def status(self, run_id: str) -> dict:
        state = self._state(run_id)
        result = {key: value for key, value in state.items() if key not in {"artifacts", "event_ids"}}
        run = self._run(run_id)
        result.update(run_dir=str(run), draft_dir=str(run / "draft"), **self.workflow.summary(state),
                      bash_timeout_ms=self.workflow.bash_timeouts(state))
        # Status only displays: unreadable event records are reported,
        # not raised. Acting on an event (resolve) still reads them strictly.
        try:
            events = self._events(state)
            candidates, excluded = approval_evidence(state, events)
            result.update(human_events=candidates, excluded_human_events=excluded,
                          prefilled_questions=len(prefilled_question_events(events)), events_error=None)
        except (DevFlowError, OSError, ValueError) as exc:
            result.update(human_events=[], excluded_human_events=[], prefilled_questions=None,
                          events_error=f"Human event records cannot be read: {exc}")
        # Status only displays: an automatic decision record that fails verification is reported.
        try:
            entries = limit_auto_records(state, self.store)
            self._check_auto_acceptance(state, entries)
            result.update(limit_auto_decisions=limit_auto_decisions(entries), limit_auto_decisions_error=None)
        except (DevFlowError, OSError, ValueError, KeyError, TypeError) as exc:
            result.update(limit_auto_decisions=None,
                          limit_auto_decisions_error=f"The automatic decision records cannot be verified: {exc}")
        self._review_summary(state, result)
        self._status_details(state, result)
        return result

    def _review_summary(self, state: dict, result: dict) -> None:
        """The review summary, while the review limit question waits and after the run ends.
        Status only displays: a summary that cannot be built is reported, not raised."""
        escalation = state["escalation"] or {}
        if state["status"] not in TERMINAL and not (
                state["status"] == "awaiting_human" and escalation.get("detail", {}).get("kind") == REVIEW_BUDGET):
            return
        try:
            source = self.workflow.review_summary_source(self.store, state)
            if source is None:
                return
            data = summary.build(source, now())
            result.update(review_summary=data, review_summary_markdown=summary.render(data), review_summary_error=None)
        except (DevFlowError, OSError, ValueError, KeyError, TypeError) as exc:
            result.update(review_summary=None, review_summary_markdown=None,
                          review_summary_error=f"The review summary cannot be built: {exc}")

    def _status_details(self, state: dict, result: dict) -> None:
        """Workflow-specific additions to ``status`` (optional)."""
