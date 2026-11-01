"""What every workflow engine shares: its run store, owner checks and frozen inputs.

A workflow engine subclasses ``RunEngine`` and only adds its own transitions.
"""
from __future__ import annotations

from pathlib import Path

from .errors import DevFlowError
from .runs import RUNNER_WAIT, RunStore
from .storage import read_json


class RunEngine:
    def __init__(self, project: Path, workflow, resources: Path | None = None,
                 lock_wait: float = RUNNER_WAIT, session: str | None = None):
        self.project = Path(project).resolve()
        if not self.project.is_dir():
            raise DevFlowError("Project must be an existing directory")
        self.workflow = workflow
        self.resources = Path(resources).resolve() if resources else workflow.resource_dir
        self.store = RunStore(self.project, workflow, lock_wait)
        # When set, every locked change verifies that this session still owns
        # the run: ownership can move while the lock is not held.
        self.session = session

    def _run(self, run_id: str) -> Path:
        return self.store.run_dir(run_id)

    def _lock(self, run_id: str):
        return self.store.lock(run_id)

    def _state(self, run_id: str, session: str | None = None) -> dict:
        """A display read, or (lock held) a change read that checks ownership."""
        if not self.store.holds(run_id):
            return self.store.read(run_id)
        state = self.store.load(run_id)
        owner = session if session is not None else self.session
        if owner is not None:
            self._require_owner(state, owner)
        return state

    @staticmethod
    def _require_owner(state: dict, session: str | None) -> None:
        if not session or state["owner_session"] != session:
            raise DevFlowError("This session does not own the run; use /devflow:resume RUN_ID first.")

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

    def _verify(self, state: dict) -> None:
        self.store.verify_artifacts(state)
        if read_json(self._run(state["run_id"]) / "config.json") != state["config"]:
            raise DevFlowError("Run configuration changed; never replace frozen configuration during a run.")

    def frozen_config(self, run_id: str) -> dict:
        """The run's frozen configuration, read without repairing anything."""
        if (self._run(run_id) / "transaction.json").exists():
            raise DevFlowError("Diagnosis is read-only; complete the pending run transaction with /devflow:resume "
                               "or cancel first.")
        state = self.store.read(run_id)
        if read_json(self._run(run_id) / "config.json") != state["config"]:
            raise DevFlowError("Run configuration changed; never replace frozen configuration during a run.")
        return state["config"]
