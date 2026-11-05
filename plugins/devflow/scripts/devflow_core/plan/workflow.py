"""The plan workflow: statuses and how runs start and move."""
from __future__ import annotations

from pathlib import Path
import re

from ..common.errors import UsageError
from ..common.review import after_failed_review, check_review_budget, check_review_config, review_budget
from ..common.runs import TERMINAL
from ..common.storage import json_bytes, now
from ..common.workflow import Workflow
from .commands import COMMANDS

STACKED_COMMAND = re.compile(r"(?:\A|\n)\s*/[A-Za-z][A-Za-z0-9_:-]*(?:\s|\Z)")


class PlanWorkflow(Workflow):
    commands = COMMANDS
    name = "plan"
    statuses = frozenset({"interview", "planning", "ready", "reviewing", "awaiting_revision",
                          "awaiting_human", "paused_error"}) | TERMINAL
    inflight_statuses = frozenset({"reviewing"})
    state_fields = frozenset({"config", "review_count", "review_limit", "plan_version", "plan_hash", "current_plan",
                              "passes", "transcript_marker", "resolved_escalations", "last_error", "last_review",
                              "final_plan_reviewed", "publication"})
    guidance = ("Planning does not change the project: write runner input files only to the run's draft "
                "directory and leave source and documents unchanged. Ask the human from this conversation.")

    def normalize_args(self, raw: str, project: Path | None = None) -> str:
        request = raw.strip() if isinstance(raw, str) else ""
        if not request:
            raise UsageError("Use /devflow:plan followed by the original planning request.")
        # A slash-starting path with additional '/' is not interpreted as a skill.
        if STACKED_COMMAND.search(request):
            raise UsageError("Start one workflow with one explicit command, not stacked skills.")
        return request

    def build_start(self, project: Path, pending: dict, options: dict,
                    scratch: Path) -> tuple[dict, dict[str, bytes]]:
        # Plan starts inside the prompt hook and has no extra start options; ``options`` is unused.
        from .config import load_config
        from .transcript import transcript_marker
        payload = pending["event"]["payload"]
        prompt = payload["prompt"]
        # Keep the request byte-exact apart from the command itself.
        request = re.sub(r"^/devflow:plan(?:\s+|$)", "", prompt, count=1)
        config = load_config(project, self)
        fields = {
            "status": "interview", "review_count": 0, "review_limit": config["max_reviews"],
            "plan_version": None, "plan_hash": None,
            "current_plan": None, "passes": [], "config": config,
            "transcript_marker": transcript_marker(payload.get("transcript_path")),
            "resolved_escalations": [], "last_error": None, "last_review": None,
            "final_plan_reviewed": False, "escalation": None,
            "publication": {"status": "pending" if config["publish"]["enabled"] else "disabled"},
        }
        artifacts = {
            "request.json": json_bytes({"schema_version": 1, "raw_prompt": prompt, "request": request,
                                        "source": "Claude Code UserPromptSubmit", "session_id": payload["session_id"]}),
            "config.json": json_bytes(config),
        }
        return fields, artifacts

    def check_state(self, state: dict) -> None:
        check_review_config(state["config"])
        check_review_budget(state, state["review_count"])

    def can_transfer(self, state: dict, session: str) -> str | None:
        unsealed = "interview.json" not in state["artifacts"]
        if unsealed and state["status"] not in TERMINAL and state["owner_session"] != session:
            return "An unsealed interview must resume its original Claude session so its transcript remains complete."
        return None

    def on_transfer(self, state: dict) -> None:
        if state["status"] == "paused_error":
            # At the review limit, the next review request asks the human.
            state["status"] = "ready"

    def on_recover(self, store, state: dict) -> None:
        from ..common.reviewers import review_tokens
        number = state["review_count"]
        state["status"] = after_failed_review(number, state, "ready")
        inflight = state["inflight"]
        reviewer = next(r for r in state["config"]["reviewers"] if r["id"] == inflight["reviewer_id"])
        state["last_error"] = {"review_number": number, "provider": inflight["reviewer_id"],
                               "plan_hash": inflight["target"], "message": "Operator confirmed the interrupted "
                               "reviewer process is stopped; attempt remains consumed.",
                               # The human confirmed the process stopped: its output no longer grows.
                               "started_at": inflight["started_at"], "finished_at": now(),
                               "tokens": review_tokens(reviewer, store.run_dir(state["run_id"]) / "review-processes"
                                                       / f"{number:02d}"),
                               "plan_version": state["plan_version"]}
        store.stage(state, f"reviews/{number:02d}/recovery.json", json_bytes(state["last_error"]))

    def context_fields(self, state: dict, run_dir: Path) -> dict:
        budget = self.summary(state)
        return {"review_count": budget["review_count"], "next_reviewer": budget["next_reviewer"],
                "plan_hash": state["plan_hash"],
                "current_plan": str(run_dir / state["current_plan"]) if state["current_plan"] else None,
                "interview_skill": state["config"]["grill_skill"]}

    def summary(self, state: dict) -> dict:
        return {**review_budget(state, state["review_count"]),
                "publication": state["publication"]}

    def review_summary_source(self, store, state: dict) -> dict:
        from .engine import summary_source
        return summary_source(store, state)

    def frozen_config(self, state: dict) -> dict:
        return state["config"]

    def load_config(self, project: Path) -> dict:
        from .config import load_config
        return load_config(project, self)

    def doctor(self, project: Path, config: dict) -> dict:
        from .interview import inspect_interview_skill
        inspection = inspect_interview_skill(project, config["grill_skill"])
        return {"interview_skill": inspection, "ready": inspection["static_ready"],
                "warnings": ([] if inspection["static_ready"] else
                             ["Planning interview skill " + inspection["configured_name"] + ": " + inspection["reason"]])
                            + ["Interview skill callability is not runtime-verified; static metadata cannot "
                               "establish effective permissions or skillOverrides"]}

    def engine(self, project: Path, session: str | None = None):
        from .engine import Engine
        return Engine(project, session=session)


WORKFLOW = PlanWorkflow()
