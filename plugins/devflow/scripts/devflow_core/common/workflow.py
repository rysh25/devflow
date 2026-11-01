"""The contract every workflow implements; the router and CLI only use this.

A workflow is a subpackage that defines one ``Workflow`` subclass instance and
ships ``defaults.json``, ``reviewer.md`` and ``review.schema.json`` next to its
code. Adding a workflow means adding such a package and one registry line.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any, Callable

from .errors import DevFlowError, GuardError

RESOURCES = ("defaults.json", "reviewer.md", "review.schema.json")
#: Option kinds Claude may pass to a runner action (each is checked by the guard).
CLAUDE_KINDS = frozenset({"draft", "hash", "event", "decision", "text"})
#: Option kinds only a human uses (actions the guard never permits).
HUMAN_KINDS = frozenset({"int", "confirm"})
READ_ONLY_COMMANDS = frozenset({"status", "doctor"})


@dataclass(frozen=True)
class Option:
    """One ``--flag VALUE`` of a runner action.

    Kinds: draft (a file in the run's draft directory), hash, event, decision
    (continue/cancel), text, int and confirm (a required yes-flag for humans).
    """
    flag: str
    kind: str
    required: bool = True

    def __post_init__(self):
        if self.kind not in CLAUDE_KINDS | HUMAN_KINDS:
            raise ValueError(f"Unknown option kind {self.kind!r} for {self.flag}")


@dataclass(frozen=True)
class Command:
    """One runner action: its options, handler and who may call it."""
    name: str
    help: str
    run: Callable[[Any, argparse.Namespace, str | None, str | None], Any]  # (engine, args, run_id, session)
    options: tuple[Option, ...] = ()
    claude: bool = True       # False: only a human, from their own terminal (the guard refuses it).
    needs_run: bool = True

    def __post_init__(self):
        if self.claude and any(option.kind not in CLAUDE_KINDS for option in self.options):
            raise ValueError(f"Action {self.name!r} available to Claude has an option the guard cannot check")


class Workflow:
    #: Public name: the slash command, runner selector and record directory.
    name: str = ""
    #: Every status a run of this workflow may have (includes the terminal ones).
    statuses: frozenset[str] = frozenset()
    #: Statuses in which an external process may be running (``inflight`` set).
    inflight_statuses: frozenset[str] = frozenset()
    #: Workflow-specific top-level state fields; anything else is rejected.
    state_fields: frozenset[str] = frozenset()
    #: Every runner action of the workflow; the single source for the guard,
    #: the CLI parser and dispatch. ``start`` is added from ``start_option``.
    commands: tuple[Command, ...] = ()
    #: Runner option of the ``start`` action that must match the pending
    #: request; ``None`` when the run is created inside the prompt hook.
    start_option: str | None = None
    #: Workflow-specific guidance added to hook context.
    guidance: str = ""

    @property
    def actions(self) -> dict[str, dict[str, str]]:
        """Runner actions Claude may call while bound: option -> kind ("?" marks optional)."""
        return {command.name: {option.flag: option.kind + ("" if option.required else "?") for option in command.options}
                for command in self.commands if command.claude}

    @property
    def human_actions(self) -> frozenset[str]:
        return frozenset(command.name for command in self.commands if not command.claude)

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

    def on_recover(self, store: Any, state: dict) -> None:
        """Set the status after a human-confirmed interruption (inflight is cleared for you)."""
        raise NotImplementedError

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
        """Small values shown in run listings and merged into a run's status."""
        return {}

    def frozen_config(self, state: dict) -> dict | None:
        """The configuration frozen at start (compared with ``config.json``), if any."""
        return None

    def load_config(self, project: Path) -> dict:
        raise NotImplementedError

    def doctor(self, project: Path, config: dict) -> dict:
        return {}

    def engine(self, project: Path, session: str | None = None):
        raise NotImplementedError

    def register_commands(self, parsers: argparse._SubParsersAction) -> None:
        for command in self.commands:
            parser = parsers.add_parser(command.name, help=command.help)
            for option in command.options:
                if option.kind == "confirm":
                    parser.add_argument(option.flag, action="store_true", required=option.required)
                elif option.kind == "decision":
                    parser.add_argument(option.flag, choices=["continue", "cancel"], required=option.required)
                else:
                    kind = {"draft": Path, "int": int}.get(option.kind, str)
                    parser.add_argument(option.flag, type=kind, required=option.required)
        if self.start_option:
            start = parsers.add_parser("start", help="Create the run requested by the user's pending command")
            start.add_argument(self.start_option, dest="start_value", required=True)

    def run_command(self, args: argparse.Namespace, project: Path, run_id: str | None, session: str | None) -> Any:
        command = next(item for item in self.commands if item.name == args.action)
        if command.needs_run and not run_id:
            raise DevFlowError(f"No {self.name} run is bound to this session; use /devflow:{self.name} "
                               "or /devflow:resume RUN_ID (or pass --run)")
        engine = self.engine(project, session)
        if run_id and session is not None and command.name not in READ_ONLY_COMMANDS:
            engine.require_owner(run_id, session)
        return command.run(engine, args, run_id, session)
