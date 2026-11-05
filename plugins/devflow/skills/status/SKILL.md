---
name: status
description: Inspect DevFlow runs, their review budgets and saved reports without starting, resuming, or changing source code.
argument-hint: "[run-id]"
disable-model-invocation: true
allowed-tools: Bash(uv run --offline --no-project --no-config --no-python-downloads "${CLAUDE_PLUGIN_ROOT}/scripts/devflow.py" *) PowerShell(uv run --offline --no-project --no-config --no-python-downloads "${CLAUDE_PLUGIN_ROOT}/scripts/devflow.py" *)
---

# DevFlow status

Optional run ID: $ARGUMENTS

Run the exact bundled command, substituting real values:

```text
uv run --offline --no-project --no-config --no-python-downloads "${CLAUDE_PLUGIN_ROOT}/scripts/devflow.py" --project "${CLAUDE_PROJECT_DIR}" --session "${CLAUDE_SESSION_ID}" status
```

If the user provided exactly one run ID, insert `--run RUN_ID` BEFORE `status`. Without it, the result says whether this session is `idle`, `pending_start`, `bound`, `stale` or `unsupported` (bound to a run from an older DevFlow that cannot continue; its `message` says which, and the next prompt releases it) (with `run` and `owner_matches`) and lists every run with a short summary; for a run's details, run status again with `--run` for that run. If arguments are malformed, ask for the exact ID; do not guess a run or substitute one. Explain workflow, current status, attempts used/maximum, pending questions, and published plan when available. When the result has `review_summary_markdown` (at the review limit question and after a run ends), show it unchanged; if `review_summary_error` is set instead, say so. A terminal report refers to its recorded snapshot, not a guarantee about later edits. Do not invoke a reviewer, edit source or state, reset a budget, or automatically resume. If a workflow is unfinished, show `/devflow:resume RUN_ID`.
