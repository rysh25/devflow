"""The plan workflow: statuses, guard rules, and how runs start and move."""
from __future__ import annotations

from pathlib import Path
import re

from ..common.errors import DevFlowError, GuardError
from ..common.review import HARD_MAX_REVIEWS
from ..common.guard import draft_path
from ..common.storage import json_bytes
from ..common.workflow import Workflow

STACKED_COMMAND = re.compile(r"(?:\A|\n)\s*/[A-Za-z][A-Za-z0-9_:-]*(?:\s|\Z)")
SKILL_NAME = re.compile(r"[a-zA-Z0-9_.:-]+\Z")


class PlanWorkflow(Workflow):
    name = "plan"
    statuses = frozenset({"interview", "planning", "ready", "reviewing", "awaiting_revision",
                          "awaiting_human", "paused_error", "completed", "exhausted", "cancelled"})
    inflight_statuses = frozenset({"reviewing"})
    state_fields = frozenset({"review_count", "plan_version", "plan_hash", "current_plan", "passes", "config",
                              "transcript_marker", "resolved_escalations", "last_error", "last_review",
                              "final_plan_reviewed", "publication"})
    # File-bearing arguments are drafts, never mutable state paths.
    actions = {
        "seal": {"--decisions-file": "draft", "--context-file": "draft?"},
        "plan": {"--file": "draft"},
        "review": {},
        "apply": {"--plan-file": "draft", "--decisions-file": "draft", "--expected-hash": "hash"},
        "pause": {"--reason-file": "draft"},
        "resolve": {"--answer-file": "draft"},
        "cancel": {},
        "report": {},
        "publish": {},
    }
    guidance = "Write only to the current run's draft directory; do not implement source changes."

    def normalize_args(self, raw: str, project: Path | None = None) -> str:
        request = raw.strip() if isinstance(raw, str) else ""
        if not request:
            raise GuardError("Use /devflow:plan followed by the original planning request.")
        # A slash-starting path with additional '/' is not interpreted as a skill.
        if STACKED_COMMAND.search(request):
            raise GuardError("Start one workflow with one explicit command, not stacked skills.")
        return request

    def build_start(self, project: Path, pending: dict) -> tuple[dict, dict[str, bytes]]:
        from .config import load_config
        from .transcript import transcript_marker
        payload = pending["event"]["payload"]
        prompt = payload["prompt"]
        # Keep the request byte-exact apart from the command itself.
        request = re.sub(r"^/devflow:plan(?:\s+|$)", "", prompt, count=1)
        config = load_config(project, self.resource_dir)
        fields = {
            "status": "interview", "review_count": 0, "plan_version": None, "plan_hash": None,
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
        count = state["review_count"]
        if type(count) is not int or not 0 <= count <= HARD_MAX_REVIEWS:
            raise DevFlowError("Incompatible/corrupt run state; never reset the review count")

    def can_transfer(self, state: dict, session: str) -> str | None:
        if state["status"] == "interview" and state["owner_session"] != session:
            return "An unsealed interview must resume its original Claude session so its transcript remains complete."
        return None

    def on_transfer(self, state: dict) -> None:
        if state["status"] == "paused_error":
            state["status"] = "ready" if state["review_count"] < state["config"]["max_reviews"] else "exhausted"

    def check_write(self, state: dict, project: Path, path: str, cwd: Path, plugin_root: Path) -> None:
        if not draft_path(path, cwd, project / ".devflow" / "runs" / self.name / state["run_id"]):
            raise GuardError("Write only to the current run's draft directory; source and audit files are read-only.")

    def allow_tool(self, state: dict, tool: str, data: dict) -> bool:
        if tool != "Skill":
            return False
        skill = state["config"].get("grill_skill")
        requested = data.get("skill")
        if not isinstance(skill, str) or not SKILL_NAME.fullmatch(skill):
            raise GuardError("Active run has no configured interview skill.")
        if isinstance(requested, str) and requested.lstrip("/") == skill.lstrip("/"):
            return True
        raise GuardError(f"Only the configured interview skill {skill!r} may be invoked during this workflow. "
                         "An allowed name does not override Claude Code's skill permissions.")

    def context_fields(self, state: dict, run_dir: Path) -> dict:
        providers = state["config"]["reviewers"]
        return {"review_count": state["review_count"], "plan_hash": state["plan_hash"],
                "current_plan": str(run_dir / state["current_plan"]) if state["current_plan"] else None,
                "next_reviewer": providers[state["review_count"] % len(providers)]["id"],
                "interview_skill": state["config"]["grill_skill"]}

    def summary(self, state: dict) -> dict:
        return {"review_count": state["review_count"], "publication": state["publication"]}

    def load_config(self, project: Path) -> dict:
        from .config import load_config
        return load_config(project, self.resource_dir)

    def doctor(self, project: Path, config: dict) -> dict:
        from .interview import inspect_interview_skill
        inspection = inspect_interview_skill(project, config["grill_skill"])
        return {"interview_skill": inspection, "ready": inspection["static_ready"],
                "warnings": ([] if inspection["static_ready"] else
                             ["Planning interview skill " + inspection["configured_name"] + ": " + inspection["reason"]])
                            + ["Interview skill callability is not runtime-verified; static metadata cannot "
                               "establish effective permissions or skillOverrides"]}

    def engine(self, project: Path):
        from .engine import Engine
        return Engine(project)

    def register_commands(self, commands) -> None:
        from .commands import register
        register(commands)

    def run_command(self, args, project: Path, run_id: str | None, session: str | None):
        from .commands import run
        return run(args, project, run_id, session)


WORKFLOW = PlanWorkflow()
