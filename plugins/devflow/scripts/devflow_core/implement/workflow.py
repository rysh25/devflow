"""The implement workflow: statuses, guard rules, and how runs start and move."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

from ..common.errors import DevFlowError, GuardError
from ..common.guard import as_path, draft_path, name_key, safe_relative, same_path
from ..common.environment import bash_timeout_ms
from ..common.review import HARD_MAX_REVIEWS, check_review_config, review_budget
from ..common.runs import TERMINAL
from ..common.storage import digest, json_bytes, no_symlinks, now
from ..common.workflow import Workflow
from .commands import COMMANDS

EDIT_STATUSES = frozenset({"implementing", "needs_revision"})
PROTECTED_COMPONENTS = frozenset({".git", ".devflow", ".claude", ".codex", ".opencode", ".agents"})
PROTECTED_NAMES = frozenset({"devflow.config.json", "claude.md", "agents.md", ".mcp.json",
                             "opencode.json", "opencode.jsonc", ".gitignore", ".gitattributes"})


def _checks_timeout_ms(config: dict) -> int:
    """All checks run in one runner call, one after another."""
    from .checks import validate_checks
    return bash_timeout_ms(sum(check["timeout_seconds"] for check in validate_checks(config["checks"])))


def plan_argument(argument: str) -> str:
    """The plan path exactly as the user typed it (quotes and '@' removed)."""
    if not isinstance(argument, str) or not argument or any(ord(ch) < 32 for ch in argument):
        raise GuardError("Use /devflow:implement followed by one plan-file path.")
    if argument[0] in {"\"", "'"}:
        if len(argument) < 3 or argument[-1] != argument[0]:
            raise GuardError("The plan path has unmatched quotes.")
        argument = argument[1:-1]
    argument = argument.removeprefix("@")
    if not argument or argument.startswith("--"):
        raise GuardError("Supply a plan file, not runner options or stacked skills.")
    return argument


class ImplementWorkflow(Workflow):
    commands = COMMANDS
    name = "implement"
    statuses = frozenset({"implementing", "needs_revision", "ready_check", "checking", "ready_review",
                          "reviewing", "awaiting_human", "paused_error", "completed", "exhausted", "cancelled"})
    inflight_statuses = frozenset({"checking", "reviewing"})
    state_fields = frozenset({"config", "plan_source", "source_digest", "attempts", "approvals", "revision",
                              "checkpoint_count", "check_attempts", "check_dispatch_count", "check_history",
                              "escalations", "resolutions", "reflections", "candidate", "validation",
                              "binary_attestation", "last_error", "baseline_id", "report_path", "resume_status"})
    start_option = "--plan"
    guidance = ("Edit project source only while the run permits it and write runner inputs to the run's "
                "draft directory; preserve pre-existing changes.")

    def normalize_args(self, raw: str, project: Path | None = None) -> str:
        argument = plan_argument(raw)
        path = Path(argument)
        if not path.is_absolute():
            if project is None:
                raise GuardError("A relative plan path needs its project directory.")
            path = Path(project) / path
        return os.path.normpath(str(path))

    def build_start(self, project: Path, pending: dict) -> tuple[dict, dict[str, bytes]]:
        from . import snapshot
        from .config import load_config
        from .engine import _plan_bytes
        plan = Path(pending["args"])
        no_symlinks(plan)
        config = load_config(project, self)
        if plan.suffix.lower() not in {".md", ".markdown"}:
            raise DevFlowError("The input plan must be a UTF-8 Markdown file")
        data = _plan_bytes(plan, config["max_plan_bytes"])
        baseline = snapshot.capture(project, config)
        if _plan_bytes(plan, config["max_plan_bytes"]) != data:
            raise DevFlowError("Input plan changed during startup; no run was created. Confirm the plan and invoke implement again")
        fields = {
            "status": "implementing", "plan_source": str(plan.resolve()), "source_digest": digest(data),
            "config": config, "attempts": [], "approvals": {}, "revision": 0, "checkpoint_count": 0,
            "check_attempts": 0, "check_dispatch_count": 0, "check_history": [], "escalations": [],
            "resolutions": [], "reflections": [], "candidate": None, "validation": None,
            "binary_attestation": None, "last_error": None, "baseline_id": baseline["snapshot_id"],
            "report_path": None, "resume_status": None, "escalation": None,
        }
        artifacts = {
            "plan.md": data, "config.json": json_bytes(config), "baseline.json": json_bytes(baseline),
            "request.json": json_bytes({"intent": {"action": "start", "args": pending["args"]},
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
        check_review_config(state["config"])
        if not isinstance(state["attempts"], list) or len(state["attempts"]) > HARD_MAX_REVIEWS:
            raise DevFlowError("Incompatible/corrupt run state; never reset the review count")

    def on_recover(self, store, state: dict) -> None:
        if state["status"] == "checking":
            # A check never consumes a review, so it always returns to implementing,
            # which (as everywhere) clears approvals.
            state.update(status="paused_error", resume_status="implementing", approvals={})
        else:
            # An interrupted review is a reviewer failure: other approvals stay.
            state["attempts"][-1].update(status="failed", error="Human-confirmed interrupted dispatch; outcome unknown",
                                         finished_at=now())
            state.update(status="paused_error" if len(state["attempts"]) < state["config"]["max_reviews"] else "exhausted",
                         resume_status="ready_review")
        state.update(last_error="Recovered interrupted operation after human process confirmation")

    def on_transfer(self, state: dict) -> None:
        if state["status"] == "paused_error":
            state["status"] = state.get("resume_status") or "implementing"
            state["resume_status"] = None
            if len(state["attempts"]) >= state["config"]["max_reviews"]:
                state["status"] = "exhausted"

    def before_save(self, store, state: dict) -> None:
        if state["status"] in {"completed", "exhausted", "cancelled"}:
            from .engine import terminal_report
            relative = f"reports/final-{state['state_revision'] + 1:06}.json"
            store.stage(state, relative, json_bytes(terminal_report(store.project, state)))
            state["report_path"] = relative

    def check_write(self, state: dict, project: Path, path: str, cwd: Path, plugin_root: Path) -> None:
        run_dir = project / ".devflow" / "runs" / self.name / state["run_id"]
        if draft_path(path, cwd, run_dir):
            return
        if state["status"] not in EDIT_STATUSES:
            raise GuardError("Source edits are permitted only during implementing or needs_revision.")
        candidate, relative = safe_relative(path, cwd, project)
        parts = [name_key(part) for part in Path(relative).parts]
        if any(part in PROTECTED_COMPONENTS for part in parts) or parts[-1] in PROTECTED_NAMES:
            raise GuardError("Workflow, Git, agent-instruction and permission configuration files are protected.")
        if same_path(candidate, as_path(state["plan_source"], project)):
            raise GuardError("The original plan is read-only.")
        from .snapshot import path_in_scope
        if not path_in_scope(relative, state["config"]):
            raise GuardError("The target is outside the frozen snapshot include/exclude scope.")
        if state["config"]["snapshot"]["mode"] == "git":
            env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
            result = subprocess.run(
                ["git", "-c", "core.fsmonitor=false", "-C", str(project), "check-ignore", "-q", "--", relative],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                shell=False, timeout=10, check=False, env=env)
            if result.returncode == 0:
                raise GuardError("Git-ignored files are not in the reviewed source scope.")
            if result.returncode != 1:
                raise GuardError("Cannot establish whether this write is in the Git snapshot scope.")

    def context_fields(self, state: dict, run_dir: Path) -> dict:
        budget = self.summary(state)
        return {"review_count": budget["review_count"], "next_reviewer": budget["next_reviewer"],
                "candidate_snapshot_id": (state.get("candidate") or {}).get("snapshot_id")}

    def summary(self, state: dict) -> dict:
        return review_budget(len(state["attempts"]), state["config"], state["status"])

    def frozen_config(self, state: dict) -> dict:
        return state["config"]

    def load_config(self, project: Path) -> dict:
        from .config import load_config
        return load_config(project, self)

    def doctor(self, project: Path, config: dict) -> dict:
        return {"warnings": ([] if config["checks"] else
                             ["No implementation checks configured; every revised snapshot requires actual human validation"])
                + self.timeout_warnings(config)}

    def bash_needs(self, config: dict) -> dict[str, int]:
        value = super().bash_needs(config)
        if config["checks"]:
            value["check"] = _checks_timeout_ms(config)
        return value

    def bash_timeouts(self, state: dict) -> dict[str, int]:
        value = super().bash_timeouts(state)
        if state["config"]["checks"] and state["status"] not in TERMINAL:
            value["check"] = _checks_timeout_ms(state["config"])
        return value

    def engine(self, project: Path, session: str | None = None):
        from .engine import Engine
        return Engine(project, session=session)


WORKFLOW = ImplementWorkflow()
