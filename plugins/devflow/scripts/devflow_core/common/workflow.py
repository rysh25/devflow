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

from .environment import review_timeout_ms
from .errors import DevFlowError

RESOURCES = ("defaults.json", "reviewer.md", "review.schema.json")
OPTION_KINDS = frozenset({"draft", "hash", "event", "decision", "text", "int", "confirm"})


@dataclass(frozen=True)
class Option:
    """One ``--flag VALUE`` of a runner action.

    Kinds: draft (a file in the run's draft directory), hash, event, decision
    (continue/cancel/stop/accept), text, int and confirm (a required yes-flag for humans).
    """
    flag: str
    kind: str
    required: bool = True

    def __post_init__(self):
        if self.kind not in OPTION_KINDS:
            raise ValueError(f"Unknown option kind {self.kind!r} for {self.flag}")


@dataclass(frozen=True)
class Command:
    """One runner action: its options and handler."""
    name: str
    help: str
    run: Callable[[Any, argparse.Namespace, str | None, str | None], Any]  # (engine, args, run_id, session)
    options: tuple[Option, ...] = ()
    needs_run: bool = True
    #: False for actions that change nothing (status, doctor) and for ``recover``, which a
    #: human runs from their own terminal after checking that no process remains
    #: (skills tell Claude not to run it; nothing else enforces that). Every
    #: other action changes the run and needs the owning session.
    needs_owner: bool = True


def option_name(flag: str) -> str:
    """The Python name of a runner flag: ``--workspace`` -> ``workspace``."""
    return flag.lstrip("-").replace("-", "_")


class Workflow:
    #: Public name: the slash command, runner selector and record directory.
    name: str = ""
    #: Every status a run of this workflow may have (includes the terminal ones).
    statuses: frozenset[str] = frozenset()
    #: Statuses in which an external process may be running (``inflight`` set).
    inflight_statuses: frozenset[str] = frozenset()
    #: Workflow-specific top-level state fields; anything else is rejected.
    state_fields: frozenset[str] = frozenset()
    #: Every runner action of the workflow; the single source for the CLI
    #: parser and dispatch. ``start`` is added from ``start_option``.
    commands: tuple[Command, ...] = ()
    #: Runner option of the ``start`` action that must match the pending
    #: request; ``None`` when the run is created inside the prompt hook.
    start_option: str | None = None
    #: Further ``start`` options, chosen by Claude rather than the user's
    #: command; never compared with the pending request.
    extra_start_options: tuple[Option, ...] = ()
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
        """Canonical start argument; the start hook and the runner compare these."""
        raise NotImplementedError

    def build_start(self, project: Path, pending: dict, options: dict,
                    scratch: Path) -> tuple[dict, dict[str, bytes | Path]]:
        """Return the workflow fields (including ``status``) and initial artifacts.

        ``options`` holds the values of ``extra_start_options`` (by
        ``option_name``); it is empty when the run starts inside the hook.
        An artifact is bytes, or a file written with ``write_new`` in
        ``scratch`` (a private directory next to the runs), which is moved
        into the run, so large start records never pass through memory.
        """
        raise NotImplementedError

    def start_options(self, args: argparse.Namespace) -> dict:
        """Values of ``extra_start_options`` from parsed runner arguments (absent ones are None)."""
        return {option_name(option.flag): getattr(args, option_name(option.flag), None)
                for option in self.extra_start_options}

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

    # -- hook context ---------------------------------------------------------------------
    def context_fields(self, state: dict, run_dir: Path) -> dict:
        return {}

    # -- presentation and commands --------------------------------------------------------
    def summary(self, state: dict) -> dict:
        """Small values shown in run listings and merged into a run's status."""
        return {}

    def review_summary_source(self, store: Any, state: dict) -> dict | None:
        """This run's reviews in the shared shape of ``common.summary`` (None: the
        workflow has no reviews to summarize)."""
        return None

    def frozen_config(self, state: dict) -> dict | None:
        """The configuration frozen at start (compared with ``config.json``), if any."""
        return None

    # -- long runner actions, run as background Bash commands ------------------------------
    def bash_timeouts(self, state: dict) -> dict[str, int]:
        """Bash timeouts (ms) for the next long actions, from the current state only."""
        next_id = self.summary(state).get("next_reviewer")
        config = self.frozen_config(state) or {}
        for reviewer in config.get("reviewers") or []:
            if reviewer["id"] == next_id:
                return {"review": review_timeout_ms(reviewer["timeout_seconds"])}
        return {}

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
                    parser.add_argument(option.flag, choices=["continue", "cancel", "stop", "accept"], required=option.required)
                else:
                    kind = {"draft": Path, "int": int}.get(option.kind, str)
                    parser.add_argument(option.flag, type=kind, required=option.required)
        if self.start_option:
            start = parsers.add_parser("start", help="Create the run requested by the user's pending command")
            start.add_argument(self.start_option, dest="start_value", required=True)
            for option in self.extra_start_options:
                start.add_argument(option.flag, dest=option_name(option.flag), required=option.required)

    def run_command(self, args: argparse.Namespace, project: Path, run_id: str | None, session: str | None) -> Any:
        command = next(item for item in self.commands if item.name == args.action)
        if command.needs_run and not run_id:
            raise DevFlowError(f"No {self.name} run is bound to this session; use /devflow:{self.name} "
                               "or /devflow:resume RUN_ID (or pass --run)")
        owned = bool(run_id) and command.needs_owner
        if owned and session is None:
            raise DevFlowError(f"{command.name} changes the run; pass --session with the session that owns it "
                               f"(or move the run to this session with /devflow:resume {run_id})")
        engine = self.engine(project, session)
        if owned:
            engine.require_owner(run_id, session)
        return command.run(engine, args, run_id, session)
