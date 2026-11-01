"""The contract every workflow implements; the router and CLI only use this.

A workflow is a subpackage that defines one ``Workflow`` subclass instance and
ships ``defaults.json``, ``reviewer.md`` and ``review.schema.json`` next to its
code. Adding a workflow means adding such a package and one registry line.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any, Mapping

from .errors import GuardError

RESOURCES = ("defaults.json", "reviewer.md", "review.schema.json")


class Workflow:
    #: Public name: the slash command, runner selector and record directory.
    name: str = ""
    #: Every status a run of this workflow may have (includes the terminal ones).
    statuses: frozenset[str] = frozenset()
    #: Statuses in which an external process may be running (``inflight`` set).
    inflight_statuses: frozenset[str] = frozenset()
    #: Workflow-specific top-level state fields; anything else is rejected.
    state_fields: frozenset[str] = frozenset()
    #: Runner actions Claude may call while bound: option -> kind.
    #: Kinds: draft, hash, event, decision, text. A trailing "?" marks optional.
    actions: Mapping[str, Mapping[str, str]] = {}
    #: Runner actions only a human may run from their own terminal.
    human_actions: frozenset[str] = frozenset({"recover"})
    #: Runner option of the ``start`` action that must match the pending
    #: request; ``None`` when the run is created inside the prompt hook.
    start_option: str | None = None
    #: Workflow-specific guidance added to hook context.
    guidance: str = ""

    @property
    def resource_dir(self) -> Path:
        return Path(sys.modules[type(self).__module__].__file__).resolve().parent

    def resource(self, name: str) -> Path:
        if name not in RESOURCES:
            raise ValueError("Unknown workflow resource: " + name)
        return self.resource_dir / name

    # -- starting and transferring runs -------------------------------------------------
    def normalize_args(self, raw: str, project: Path) -> str:
        """Canonical start argument; the guard and the engine compare these."""
        raise NotImplementedError

    def build_start(self, project: Path, pending: dict) -> tuple[dict, dict[str, bytes]]:
        """Return the workflow fields (including ``status``) and initial artifacts."""
        raise NotImplementedError

    def can_transfer(self, state: dict, session: str) -> str | None:
        """Return a refusal reason, or ``None`` when ownership may move."""
        return None

    def on_transfer(self, state: dict) -> None:
        """Adjust workflow fields when a run is resumed (e.g. leave paused_error)."""

    def check_state(self, state: dict) -> None:
        """Reject workflow fields that violate an invariant (raise DevFlowError)."""

    def before_save(self, store: Any, state: dict) -> None:
        """Stage additional artifacts before every commit (optional)."""

    # -- guard ---------------------------------------------------------------------------
    def check_write(self, state: dict, project: Path, path: str, cwd: Path, plugin_root: Path) -> None:
        raise GuardError("This workflow does not permit file writes.")

    def allow_tool(self, state: dict, tool: str, data: dict) -> bool:
        """Permit a workflow-specific tool (e.g. the plan interview Skill)."""
        return False

    def context_fields(self, state: dict, run_dir: Path) -> dict:
        return {}

    # -- presentation and commands --------------------------------------------------------
    def summary(self, state: dict) -> dict:
        """Small listing values for the global status (never the whole state)."""
        return {}

    def load_config(self, project: Path) -> dict:
        raise NotImplementedError

    def doctor(self, project: Path, config: dict) -> dict:
        return {}

    def engine(self, project: Path):
        raise NotImplementedError

    def register_commands(self, commands: argparse._SubParsersAction) -> None:
        raise NotImplementedError

    def run_command(self, args: argparse.Namespace, project: Path, run_id: str | None, session: str | None) -> Any:
        raise NotImplementedError
