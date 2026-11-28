"""The implement workflow: statuses and how runs start and move."""
from __future__ import annotations

import os
from pathlib import Path

from ..common import summary
from ..common.errors import DevFlowError, UsageError
from ..common.environment import bash_timeout_ms
from ..common.review import (after_failed_review, code_span, check_limit_auto_names, check_review_budget,
                             check_review_config, review_budget)
from ..common.runs import TERMINAL
from ..common.sessions import prefilled_question_events
from ..common.settings import SettingsDocument
from ..common.operations import CHAINED, public_command
from ..common.startup import apply_startup, parse_start
from ..common.storage import check_json_size, digest, json_bytes, no_symlinks, now
from ..common.workflow import Option, Workflow
from .commands import COMMANDS


def _checks_timeout_ms(config: dict) -> int:
    """All checks run in one runner call, one after another."""
    from .checks import validate_checks
    return bash_timeout_ms(sum(check["timeout_seconds"] for check in validate_checks(config["checks"])))


def plan_argument(argument: str) -> str:
    """The plan path exactly as the user typed it (quotes and '@' removed)."""
    argument = argument.strip() if isinstance(argument, str) else argument
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
                          "reviewing", "awaiting_human", "paused_error"}) | TERMINAL
    inflight_statuses = frozenset({"checking", "reviewing"})
    state_fields = frozenset({"config", "plan_source", "source_digest", "attempts", "review_limit", "approvals",
                              "revision", "checkpoint_count", "check_attempts", "check_dispatch_count", "check_history",
                              "escalations", "resolutions", "reflections", "candidate", "validation",
                              "binary_attestation", "last_error", "baseline_id", "report_path", "resume_status",
                              "workspace"})
    start_option = "--plan"
    from_plan_option = "--from-plan"
    extra_start_options = (Option("--workspace", "text", required=False),)
    occupies_workspace = True
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

    def build_start(self, project: Path, pending: dict, options: dict,
                    scratch: Path, *, project_config: dict) -> tuple[dict, dict[str, bytes | Path]]:
        from . import snapshot
        from .config import validate_config
        from ..common.reviewers import preflight
        from .engine import _plan_bytes
        from .workspace import resolve_workspace, verify_workspace
        plan = Path(pending["args"])
        no_symlinks(plan)
        payload = pending["event"]["payload"]
        chain = options.get("chain")
        if chain:
            # Chained from a flow's Plan run: the human's flow command held Plan's startup
            # options, so Implement uses the project settings only.
            config = validate_config(project_config)
        else:
            startup = parse_start(public_command(payload["prompt"], payload["host"])[1])
            config = validate_config(apply_startup(project_config, startup))
        if plan.suffix.lower() not in {".md", ".markdown"}:
            raise DevFlowError("The input plan must be a UTF-8 Markdown file")
        # The workspace is decided once, before anything is created, and frozen.
        workspace = resolve_workspace(project, options.get("workspace"), config["snapshot"]["mode"])
        data = _plan_bytes(plan, config["max_plan_bytes"])
        if chain and digest(data) != chain["plan_hash"]:
            # Compared on the bytes the run keeps, so a later rewrite cannot slip in.
            raise DevFlowError(f"The published plan no longer matches Plan run {chain['plan_run_id']}'s approved hash")
        # Each reviewer is checked in the workspace before any work starts, so a reviewer that
        # cannot run there (for example Devin in a directory it does not trust) stops the start
        # and creates no run. The results are not kept: each review checks again.
        for provider in config["reviewers"]:
            preflight(provider, cwd=Path(workspace["path"]))
        # The workspace must still be the one decided above after waiting for the checks.
        verify_workspace(project, workspace, config["snapshot"]["mode"])
        # Only the text files Git does not hold are copied, once, into scratch/baseline.
        copies = scratch / "baseline"
        baseline = snapshot.capture(Path(workspace["path"]), config, copies=copies)
        baseline_bytes = json_bytes(baseline)
        try:
            check_json_size("baseline.json", baseline_bytes)
        except DevFlowError as exc:
            raise DevFlowError(f"{exc}. The snapshot scope has {baseline['file_count']} files; narrow "
                               "snapshot.include or snapshot.exclude and start again") from exc
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
        intent = {"action": "start", "args": pending["args"], "workspace": options.get("workspace")}
        if chain:
            intent.update(action=CHAINED, **{key: chain[key] for key in
                                              ("plan_run_id", "plan_hash", "plan_path", "approval_path")})
        artifacts = {
            "plan.md": data, "config.json": json_bytes(config), "baseline.json": baseline_bytes,
            "request.json": json_bytes({"intent": intent, "event": pending["event"]}),
        }
        for record in baseline["files"].values():
            copy = (record.get("source") or {}).get("copy")
            if copy:
                artifacts[copy] = copies / Path(copy).name
        return fields, artifacts

    def existing_changes(self, store, state: dict) -> tuple[list[str], list[str]] | None:
        """The uncommitted changes touching the snapshot scope at start, from ``baseline.json``:
        each as Git shows it (``SOURCE -> PATH`` for a rename or copy), and as code spans for
        text a human reads. None in files mode (no Git status) or when there were none."""
        status = (store.record_json(state, "baseline.json").get("git") or {}).get("status")
        if not status:
            return None
        paths = [f"{entry['source']} -> {entry['path']}" if entry["source"] else entry["path"] for entry in status]
        shown = [f"{code_span(entry['source'])} -> {code_span(entry['path'])}" if entry["source"]
                 else code_span(entry["path"]) for entry in status]
        return paths, shown

    def start_fields(self, store, state: dict) -> dict:
        changes = self.existing_changes(store, state)
        if not changes:
            return {}
        paths, shown = changes
        return {"existing_changes": {"count": len(paths), "paths": paths, "warning": (
            f"The workspace had {len(paths)} uncommitted changes touching the snapshot scope at start: "
            f"{', '.join(shown)}. They are part of the baseline: reviews do not cover them and they sit "
            "beside the implementation's changes. DevFlow never discards or commits them.")}}

    def workspace(self, state: dict) -> str:
        """The workspace frozen at start (the project or a worktree of its repository)."""
        return state["workspace"]["path"]

    def check_state(self, state: dict) -> None:
        from .workspace import check_frozen
        check_review_config(state["config"])
        check_frozen(state["workspace"])
        if not isinstance(state["attempts"], list):
            raise DevFlowError("Incompatible/corrupt run state; never reset the review count")
        check_review_budget(state, len(state["attempts"]))
        check_limit_auto_names(state, self.escalation_id_pattern)

    #: Implement question IDs are ``E{index:03}`` (``engine._escalate``), four digits from the 1000th.
    escalation_id_pattern = "E[0-9]{3,}"

    def limit_auto_question(self, state: dict, escalation_id: str) -> str:
        """``E004`` is ``escalations/004.json``, which the run must list."""
        path = f"escalations/{int(escalation_id[1:]):03}.json"
        if path not in state["escalations"]:
            raise DevFlowError(f"No recorded question {escalation_id}")
        return path

    def on_recover(self, store, state: dict) -> None:
        if state["status"] == "checking":
            # A check never consumes a review, so it always returns to implementing,
            # which (as everywhere) clears approvals.
            state.update(status="paused_error", resume_status="implementing", approvals={})
        else:
            # An interrupted review is a reviewer failure: other approvals stay.
            from ..common.reviewers import review_tokens
            attempt = state["attempts"][-1]
            reviewer = next(r for r in state["config"]["reviewers"] if r["id"] == attempt["reviewer_id"])
            # The human confirmed the process stopped: its output no longer grows.
            tokens = review_tokens(reviewer, store.run_dir(state["run_id"]) / "review-processes"
                                   / f"{attempt['number']:02}")
            attempt.update(status="failed", error="Human-confirmed interrupted dispatch; outcome unknown",
                           finished_at=now(), tokens=tokens)
            state.update(status=after_failed_review(len(state["attempts"]), state, "ready_review"),
                         resume_status="ready_review")
        state.update(last_error="Recovered interrupted operation after human process confirmation")

    def on_transfer(self, state: dict) -> None:
        if state["status"] == "paused_error":
            # At the review limit, the next review request asks the human.
            state["status"] = state.get("resume_status") or "implementing"
            state["resume_status"] = None

    def before_save(self, store, state: dict) -> None:
        if state["status"] in TERMINAL:
            # An auto_accepted run keeps the report finalized with its acceptance:
            # later saves (an explicit resume) reuse it and never recapture the workspace.
            if state["status"] == "auto_accepted" and state["report_path"]:
                return
            from .engine import _auto_acceptance_entry, terminal_report
            relative = f"reports/final-{state['state_revision'] + 1:06}.json"
            # Ending a run must not depend on every event file being readable;
            # a damaged one is reported instead of blocking cancel or recover.
            try:
                observed = prefilled_question_events(store.events(state))
                observed_error = None
            except (DevFlowError, OSError, ValueError) as exc:
                observed, observed_error = None, str(exc)
            verified = None
            if state["status"] == "auto_accepted":
                # The acceptance and its report share the snapshot _accept_current
                # verified in limit-auto; a second capture could disagree about
                # the target. The full entry check — the decision record, the
                # frozen config record, candidate and reviewed facts — runs before
                # the terminal state commits: a verification failure propagates
                # and leaves the prior state in effect.
                evidence = _auto_acceptance_entry(state, store)
                verified = {"snapshot_id": evidence["version"], "reviewed": evidence["version_reviewed"]}
            report = terminal_report(store, state, observed, observed_error, verified)
            data = json_bytes(report)
            try:
                check_json_size(relative, data)
            except DevFlowError:
                # The run's end and its acceptance are saved; the summary is never cut,
                # and status still shows it, rebuilt from the records.
                report.update(review_summary=None, review_summary_error=summary.TOO_LARGE)
                data = json_bytes(report)
            store.stage(state, relative, data)
            state["report_path"] = relative

    def review_summary_source(self, store, state: dict) -> dict:
        from .engine import summary_source
        return summary_source(store, state)

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

    def load_config(self, documents: tuple[SettingsDocument, ...]) -> dict:
        from .config import load_config
        return load_config(documents, self)

    def doctor(self, project: Path, config: dict, *, host: str, frozen: bool = False) -> dict:
        # Diagnostics over current project settings (a pending start included) report
        # invalid snapshot syntax surviving in the resolved config; a selected run's
        # doctor keeps its own frozen-config comparison.
        if not frozen:
            from .snapshot import validate_patterns
            validate_patterns(config["snapshot"])
        return {"warnings": ([] if config["checks"] else
                             ["No implementation checks configured; every revised snapshot requires actual human validation"])}

    def bash_timeouts(self, state: dict) -> dict[str, int]:
        value = super().bash_timeouts(state)
        if state["config"]["checks"] and state["status"] not in TERMINAL:
            value["check"] = _checks_timeout_ms(state["config"])
        return value

    def engine(self, project: Path, session: str | None = None, *, host: str):
        from .engine import Engine
        return Engine(project, session=session, host=host)


WORKFLOW = ImplementWorkflow()
