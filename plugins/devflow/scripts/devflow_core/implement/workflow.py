"""The implement workflow: statuses and how runs start and move."""
from __future__ import annotations

import os
from pathlib import Path

from ..common.errors import DevFlowError, UsageError
from ..common.environment import bash_timeout_ms
from ..common.review import after_failed_review, check_review_budget, check_review_config, review_budget
from ..common.runs import TERMINAL
from ..common.sessions import prefilled_question_events
from ..common.storage import digest, json_bytes, no_symlinks, now
from ..common.workflow import Option, Workflow
from .commands import COMMANDS


def _checks_timeout_ms(config: dict) -> int:
    """All checks run in one runner call, one after another."""
    from .checks import validate_checks
    return bash_timeout_ms(sum(check["timeout_seconds"] for check in validate_checks(config["checks"])))


def plan_argument(argument: str) -> str:
    """The plan path exactly as the user typed it (quotes and '@' removed)."""
    if not isinstance(argument, str) or not argument or any(ord(ch) < 32 for ch in argument):
        raise UsageError("Use /devflow:implement followed by one plan-file path.")
    if argument[0] in {"\"", "'"}:
        if len(argument) < 3 or argument[-1] != argument[0]:
            raise UsageError("The plan path has unmatched quotes.")
        argument = argument[1:-1]
    argument = argument.removeprefix("@")
    if not argument or argument.startswith("--"):
        raise UsageError("Supply a plan file, not runner options or stacked skills.")
    return argument


class ImplementWorkflow(Workflow):
    commands = COMMANDS
    name = "implement"
    statuses = frozenset({"implementing", "needs_revision", "ready_check", "checking", "ready_review",
                          "reviewing", "awaiting_human", "paused_error", "completed", "exhausted", "cancelled"})
    inflight_statuses = frozenset({"checking", "reviewing"})
    state_fields = frozenset({"config", "plan_source", "source_digest", "attempts", "review_limit", "approvals",
                              "revision", "checkpoint_count", "check_attempts", "check_dispatch_count", "check_history",
                              "escalations", "resolutions", "reflections", "candidate", "validation",
                              "binary_attestation", "last_error", "baseline_id", "report_path", "resume_status",
                              "workspace"})
    start_option = "--plan"
    extra_start_options = (Option("--workspace", "text", required=False),)
    guidance = ("DevFlow records and verifies this run. Write runner input files "
                "to the run's draft directory, preserve pre-existing changes, and use the runner's checks "
                "as the validation evidence.")

    def normalize_args(self, raw: str, project: Path | None = None) -> str:
        argument = plan_argument(raw)
        path = Path(argument)
        if not path.is_absolute():
            if project is None:
                raise UsageError("A relative plan path needs its project directory.")
            path = Path(project) / path
        return os.path.normpath(str(path))

    def build_start(self, project: Path, pending: dict, options: dict) -> tuple[dict, dict[str, bytes]]:
        from . import snapshot
        from .config import load_config
        from .engine import _plan_bytes
        from .workspace import resolve_workspace
        plan = Path(pending["args"])
        no_symlinks(plan)
        config = load_config(project, self)
        if plan.suffix.lower() not in {".md", ".markdown"}:
            raise DevFlowError("The input plan must be a UTF-8 Markdown file")
        # The workspace is decided once, before anything is created, and frozen.
        workspace = resolve_workspace(project, options.get("workspace"), config["snapshot"]["mode"])
        data = _plan_bytes(plan, config["max_plan_bytes"])
        baseline = snapshot.capture(Path(workspace["path"]), config)
        if _plan_bytes(plan, config["max_plan_bytes"]) != data:
            raise DevFlowError("Input plan changed during startup; no run was created. Confirm the plan and invoke implement again")
        fields = {
            "status": "implementing", "plan_source": str(plan.resolve()), "source_digest": digest(data),
            "config": config, "attempts": [], "review_limit": config["max_reviews"], "approvals": {},
            "revision": 0, "checkpoint_count": 0,
            "check_attempts": 0, "check_dispatch_count": 0, "check_history": [], "escalations": [],
            "resolutions": [], "reflections": [], "candidate": None, "validation": None,
            "binary_attestation": None, "last_error": None, "baseline_id": baseline["snapshot_id"],
            "report_path": None, "resume_status": None, "escalation": None, "workspace": workspace,
        }
        artifacts = {
            "plan.md": data, "config.json": json_bytes(config), "baseline.json": json_bytes(baseline),
            "request.json": json_bytes({"intent": {"action": "start", "args": pending["args"],
                                                   "workspace": options.get("workspace")},
                                        "event": pending["event"]}),
        }
        if baseline.get("git", {}) and baseline["git"].get("dirty") and not config["allow_dirty"]:
            escalation = {"id": "E001", "reason": "Existing changes require explicit acknowledgement",
                          "questions": ["The worktree has existing changes. Preserve them and continue from this captured baseline?"],
                          "return_status": "implementing", "detail": {"kind": "dirty_worktree", "snapshot_id": None}}
            artifacts["escalations/001.json"] = json_bytes({**escalation, "boundary": 1, "created_at": now()})
            fields.update(status="awaiting_human", escalation=escalation, escalations=["escalations/001.json"])
        return fields, artifacts

    def check_state(self, state: dict) -> None:
        from .workspace import check_frozen
        check_review_config(state["config"])
        check_frozen(state["workspace"])
        if not isinstance(state["attempts"], list):
            raise DevFlowError("Incompatible/corrupt run state; never reset the review count")
        check_review_budget(state, len(state["attempts"]))

    def on_recover(self, store, state: dict) -> None:
        if state["status"] == "checking":
            # A check never consumes a review, so it always returns to implementing,
            # which (as everywhere) clears approvals.
            state.update(status="paused_error", resume_status="implementing", approvals={})
        else:
            # An interrupted review is a reviewer failure: other approvals stay.
            state["attempts"][-1].update(status="failed", error="Human-confirmed interrupted dispatch; outcome unknown",
                                         finished_at=now())
            state.update(status=after_failed_review(len(state["attempts"]), state, "ready_review"),
                         resume_status="ready_review")
        state.update(last_error="Recovered interrupted operation after human process confirmation")

    def on_transfer(self, state: dict) -> None:
        if state["status"] == "paused_error":
            # At the review limit, the next review request asks the human.
            state["status"] = state.get("resume_status") or "implementing"
            state["resume_status"] = None

    def before_save(self, store, state: dict) -> None:
        if state["status"] in {"completed", "exhausted", "cancelled"}:
            from .engine import terminal_report
            relative = f"reports/final-{state['state_revision'] + 1:06}.json"
            # Ending a run must not depend on every event file being readable;
            # a damaged one is reported instead of blocking cancel or recover.
            try:
                observed = prefilled_question_events(store.events(state))
                observed_error = None
            except (DevFlowError, OSError, ValueError) as exc:
                observed, observed_error = None, str(exc)
            store.stage(state, relative, json_bytes(terminal_report(store.project, state, observed, observed_error)))
            state["report_path"] = relative

    def context_fields(self, state: dict, run_dir: Path) -> dict:
        budget = self.summary(state)
        workspace = state["workspace"]
        return {"review_count": budget["review_count"], "next_reviewer": budget["next_reviewer"],
                "candidate_snapshot_id": (state.get("candidate") or {}).get("snapshot_id"),
                "project": state["project"],
                "workspace": {"path": workspace["path"], "identity_verified": workspace["identity"] is not None}}

    def summary(self, state: dict) -> dict:
        return review_budget(state, len(state["attempts"]))

    def frozen_config(self, state: dict) -> dict:
        return state["config"]

    def load_config(self, project: Path) -> dict:
        from .config import load_config
        return load_config(project, self)

    def doctor(self, project: Path, config: dict) -> dict:
        return {"warnings": ([] if config["checks"] else
                             ["No implementation checks configured; every revised snapshot requires actual human validation"])}

    def bash_timeouts(self, state: dict) -> dict[str, int]:
        value = super().bash_timeouts(state)
        if state["config"]["checks"] and state["status"] not in TERMINAL:
            value["check"] = _checks_timeout_ms(state["config"])
        return value

    def engine(self, project: Path, session: str | None = None):
        from .engine import Engine
        return Engine(project, session=session)


WORKFLOW = ImplementWorkflow()
