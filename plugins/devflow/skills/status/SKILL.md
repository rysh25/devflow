---
name: status
description: Inspect DevFlow runs, their review budgets and saved reports without starting, resuming, or changing source code.
argument-hint: "[run-id]"
disable-model-invocation: true
allowed-tools: Bash(uv run --offline --no-project --no-config --no-python-downloads "${CLAUDE_PLUGIN_ROOT}/scripts/devflow.py" *)
---

# DevFlow status

Optional run ID: $ARGUMENTS

Run the exact bundled command, substituting real values:

```text
uv run --offline --no-project --no-config --no-python-downloads "${CLAUDE_PLUGIN_ROOT}/scripts/devflow.py" --project "${CLAUDE_PROJECT_DIR}" --session "${CLAUDE_SESSION_ID}" status
```

If the user provided exactly one run ID, insert `--run RUN_ID` BEFORE `status`. If arguments are malformed, ask for the exact ID; do not guess a run or substitute one. Explain workflow, current status, attempts used/maximum, pending questions, and published plan when available. A terminal report refers to its recorded snapshot, not a guarantee about later edits. Do not invoke a reviewer, edit source or state, reset a budget, or automatically resume. If a workflow is unfinished, show `/devflow:resume RUN_ID`.
