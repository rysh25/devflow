---
name: flow
description: Run DevFlow Plan from an explicit user request and, in the same conversation, continue into DevFlow Implement with the published plan when Plan ends with an approved, published plan. Stops where the human is needed or where a chained start is not possible.
argument-hint: "[--max-reviews N] [--reviewer AGENT MODEL EFFORT ...] -- <request>"
disable-model-invocation: true
allowed-tools: Bash(uv --no-cache run --offline --no-project --no-config --no-python-downloads "${CLAUDE_PLUGIN_ROOT}/scripts/devflow.py" *) PowerShell(uv --no-cache run --offline --no-project --no-config --no-python-downloads "${CLAUDE_PLUGIN_ROOT}/scripts/devflow.py" *)
---

# DevFlow flow

Original user arguments: $ARGUMENTS

Always pass `--host claude` to the runner.

Invocation context (expanded by Claude Code, not shell variables to guess):

- Project directory: `${CLAUDE_PROJECT_DIR}`
- Current session ID: `${CLAUDE_SESSION_ID}`
- Installed plugin directory: `${CLAUDE_PLUGIN_ROOT}`

The human typed /devflow:flow. The hook has already created a Plan run exactly as for /devflow:plan; the startup options apply to Plan only. Read `${CLAUDE_PLUGIN_ROOT}/skills/plan/SKILL.md` and follow it as if the human had typed /devflow:plan, replacing each `${...}` in that file with the expanded values above. Its rules not to start implementation and to offer `/devflow:implement PATH` for a separate turn are replaced, for this run, by the Flow section of `${CLAUDE_PLUGIN_ROOT}/references/plan-workflow.md`, which says when and how to continue into Implement.

Chained start templates (the Flow section says when to call one; run it alone, never joined with other commands):

```text
uv --no-cache run --offline --no-project --no-config --no-python-downloads "${CLAUDE_PLUGIN_ROOT}/scripts/devflow.py" --host claude --project "${CLAUDE_PROJECT_DIR}" --session "${CLAUDE_SESSION_ID}" implement start --from-plan PLAN_RUN_ID
uv --no-cache run --offline --no-project --no-config --no-python-downloads "${CLAUDE_PLUGIN_ROOT}/scripts/devflow.py" --host claude --project "${CLAUDE_PROJECT_DIR}" --session "${CLAUDE_SESSION_ID}" implement start --from-plan PLAN_RUN_ID --workspace "WORKSPACE_PATH"
```
