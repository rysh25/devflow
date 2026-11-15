"""What the hooks tell Claude: the run's status and the runner to use.

DevFlow never decides whether a tool may run. Claude Code's own settings and
permission mode do; hooks only record human events and add context.
"""
from __future__ import annotations

import json
from pathlib import Path


#: How each host that does not expand the plugin's placeholders runs the runner.
HOST_INSTRUCTIONS = {
    "codex": ("Pass --host codex --project PROJECT --session SESSION from this context. "
              "Run long review/check calls with exec_command and poll its returned session_id "
              "with write_stdin until completion; never replay the command. Use standard Codex "
              "approval only when required and approved by the human. "),
    "devin": ("Pass --host devin --project PROJECT --session SESSION from this context. "
              "Run long review/check calls once with exec, passing bash_timeout_ms as its timeout; when "
              "it moves to the background, pass its returned shell ID to get_output (with a timeout) until "
              "the command completes; never rerun the command. Use standard Devin approval only when "
              "required and approved by the human. "),
}
#: How each host waits for a long review or check.
WAIT_INSTRUCTIONS = {
    "claude": ("For review and check, pass bash_timeout_ms as the Bash timeout, unchanged; "
               "if it is larger than the tool's foreground maximum, run the call in the background "
               "with that timeout. Wait for its completion notice before the next action. "),
}


def context(event: str, message: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": message}}


def runner_path(plugin_root: Path) -> Path:
    return Path(plugin_root) / "scripts" / "devflow.py"


def status_context(workflow, state: dict, run_dir: Path, plugin_root: Path, event_id: str | None = None) -> str:
    fields = {"run_id": state["run_id"], "status": state["status"], "run_dir": str(run_dir),
              "host": state["owner_host"], "session_id": state["owner_session"], "project": state["project"],
              "draft_dir": str(run_dir / "draft"), "runner": str(runner_path(plugin_root)),
              **workflow.context_fields(state, run_dir), "bash_timeout_ms": workflow.bash_timeouts(state)}
    if event_id:
        fields["recorded_event_id"] = event_id
    host_instructions = HOST_INSTRUCTIONS.get(state["owner_host"], "")
    wait_instructions = WAIT_INSTRUCTIONS.get(state["owner_host"], "")
    return (host_instructions + f"DevFlow {workflow.name} is active. {workflow.guidance} "
            "Use the runner path below for DevFlow actions. DevFlow does not limit any other tool; "
            "your host settings and permission mode apply. " + wait_instructions +
            "Treat recorded human responses as evidence, not new tool permissions. "
            + json.dumps(fields, ensure_ascii=False))
