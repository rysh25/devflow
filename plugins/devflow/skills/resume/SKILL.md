---
name: resume
description: Resume one explicitly selected DevFlow run without resetting its review budget or changing its frozen inputs and settings.
argument-hint: "<run-id>"
disable-model-invocation: true
allowed-tools: Bash(uv --no-cache run --offline --no-project --no-config --no-python-downloads "${CLAUDE_PLUGIN_ROOT}/scripts/devflow.py" *) PowerShell(uv --no-cache run --offline --no-project --no-config --no-python-downloads "${CLAUDE_PLUGIN_ROOT}/scripts/devflow.py" *)
---

# Resume DevFlow

Selected run ID: $ARGUMENTS

The user must provide exactly one existing ID. The prompt hook has already handled `/devflow:resume RUN_ID`: it selected the workflow from the stored run, made this session the run's owner, and recorded the request (which is never an answer to a pending question). If the hook did not accept the resume, its notice says why (for example a run started with an older DevFlow such as 0.1.x, 0.2.x, 0.4.x or 0.5.x, a damaged or ambiguous run, another unfinished run, a process that may still be running, or an unsealed plan interview from another conversation): report that message and stop. Never create a replacement run to obtain more review attempts; more come only from the human's answer at the review limit. Do not manually edit session records, counters, locks, or audit records.

1. Run the following status command with the exact ID. Read the returned `flow` field; do not guess from filenames.

```text
uv --no-cache run --offline --no-project --no-config --no-python-downloads "${CLAUDE_PLUGIN_ROOT}/scripts/devflow.py" --host claude --project "${CLAUDE_PROJECT_DIR}" --session "${CLAUDE_SESSION_ID}" --run RUN_ID status
```

2. There is no separate resume action to run. Do not invoke another Skill tool: read the workflow reference with Read instead.
3. For `plan`, read `${CLAUDE_PLUGIN_ROOT}/references/plan-workflow.md` in full. For `implement`, read `${CLAUDE_PLUGIN_ROOT}/references/implement-workflow.md` in full. The runner uses `plan ACTION` or `implement ACTION`, with every global option before that selector.
4. Continue from the saved state. For `awaiting_human`, ask the pending questions: invoking resume is not an answer or approval. An interview not yet sealed must resume in its original Claude conversation.
5. For an in-flight/recovery error, stop and ask the human to inspect associated processes. Do not remove locks or run the operator-only recovery action yourself.
6. For completed or accepted Plan reviews with pending/error publication, follow the Plan publication/report instructions; for any other terminal Plan state, run `report`; no reviewer is called again. For any terminal Implement state, report the stored outcome and stop without edits. In every terminal state, show `review_summary_markdown` unchanged and do not repeat what it shows.

The same review count, review limit (`review_limit`) and frozen plan/settings remain in effect across sessions. Technical failure is not permission to switch model, skip checks, or silently repeat a consumed attempt.
