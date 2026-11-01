---
name: resume
description: Resume one explicitly selected DevFlow run without resetting its review budget or changing its frozen inputs and settings.
argument-hint: "<run-id>"
disable-model-invocation: true
allowed-tools: Bash(uv run --offline --no-project --no-config --no-python-downloads "${CLAUDE_PLUGIN_ROOT}/scripts/devflow.py" *)
---

# Resume DevFlow

Selected run ID: $ARGUMENTS

The user must provide exactly one existing ID. The unified hook selects its workflow from the stored run, not from the conversation. Never create a replacement run to obtain more review attempts. Do not manually edit session bindings, counters, locks, or audit records.

1. Run the following status command with the exact ID. Read the returned `flow` field; do not guess from filenames.

```text
uv run --offline --no-project --no-config --no-python-downloads "${CLAUDE_PLUGIN_ROOT}/scripts/devflow.py" --project "${CLAUDE_PROJECT_DIR}" --session "${CLAUDE_SESSION_ID}" --run RUN_ID status
```

2. Run the same prefix/global selectors followed by `plan resume` or `implement resume`, according to that field. Do not invoke another Skill tool: read its workflow reference with Read instead.
3. For `plan`, read `${CLAUDE_PLUGIN_ROOT}/references/plan-workflow.md` in full. For `implement`, read `${CLAUDE_PLUGIN_ROOT}/references/implement-workflow.md` in full. The runner uses `plan ACTION` or `implement ACTION`, with every global option before that selector.
4. Continue from the saved state. For `awaiting_human`, ask the pending questions: invoking resume is not an answer or approval. An interview not yet sealed must resume in its original Claude conversation.
5. For an in-flight/recovery error, stop and ask the human to inspect associated processes. Do not remove locks or run the operator-only recovery action yourself.
6. For completed Plan reviews with pending/error publication, follow the Plan publication/report instructions; no reviewer is called again. For any terminal Implement state, report the stored outcome and stop without edits.

The same six-attempt ceiling and frozen plan/settings remain in effect across sessions. Technical failure is not permission to switch model, skip checks, or silently repeat a consumed attempt.
