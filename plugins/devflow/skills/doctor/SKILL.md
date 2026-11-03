---
name: doctor
description: Check DevFlow prerequisites and project settings offline without invoking a model, installing tools, or changing the project.
disable-model-invocation: true
allowed-tools: Bash(uv run --offline --no-project --no-config --no-python-downloads "${CLAUDE_PLUGIN_ROOT}/scripts/devflow.py" *) PowerShell(uv run --offline --no-project --no-config --no-python-downloads "${CLAUDE_PLUGIN_ROOT}/scripts/devflow.py" *)
---

# DevFlow diagnostics

Run:

```text
uv run --offline --no-project --no-config --no-python-downloads "${CLAUDE_PLUGIN_ROOT}/scripts/devflow.py" --project "${CLAUDE_PROJECT_DIR}" --session "${CLAUDE_SESSION_ID}" doctor
```

Explain missing executables, invalid settings, reviewer CLIs that lack a flag DevFlow passes, and missing checks. Inspect `workflows.plan.interview_skill` separately from `workflows.implement`: Plan needs the configured model-invocable interview skill (default `grilling`); Implement does not. Explain blocked, missing, ambiguous, invalid, and unverified results using the reported locations and reason. Do not equate a file's existence with permission to invoke it, or an unverified plugin skill with a known-missing skill. Static readiness never proves that the Skill tool can run successfully.

Claude Code must support hook exec-form and the `agent_id` of subagent tool calls in hook input; the documented minimum is 2.1.316. Python 3.11 or later is supported, not pinned to 3.11. uv and a compatible interpreter must already be installed in the same environment as Claude Code. Do not automatically install, authenticate, download Python, change model IDs, edit third-party skill metadata, weaken permissions, or call a paid model to probe availability. Offline diagnostics cannot verify credentials, model access, actual effort honored by providers, or real hook/skill execution. If the runner cannot start, report the exact environment blocker rather than pretending doctor ran.
