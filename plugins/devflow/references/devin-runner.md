# Devin runner contract

Devin does not expand plugin placeholders in skills. The DevFlow prompt hook delivers this operation's context instead: `host` (always `devin`), `session_id`, `project` and `runner`. Use only those values. If any of them is missing, stop and tell the human that the DevFlow hook did not run; do not take a session ID or path from environment variables, from another session or from a guess.

Run the runner as one command, with the values substituted and quoted for the shell, and with every global argument before the command:

```text
uv --no-cache run --offline --no-project --no-config --no-python-downloads "RUNNER" --host devin --project "PROJECT" --session "SESSION_ID" COMMAND
```

Every result is one JSON object; read its status rather than relying on the exit code.

**Waiting.** For `review` and `check`, run the command once with exec, passing the result's `bash_timeout_ms` value for that action as exec's `timeout`. When exec moves the command to the background and returns a shell ID, call get_output with that ID (and a timeout) until the command has finished, then read its JSON. Never run the command a second time and never start another runner action while it is running. The same waiting applies to `doctor` and `start`, which check each reviewer CLI locally and may take a while.

**Approval.** Devin's own permission settings and mode apply. When Devin asks to approve the runner command, the human approves it with Devin's standard approval. Never change the human's settings, never pass a flag that skips workspace trust or permissions, and never retry with broader permissions.

**Questions.** Ask the human with ask_user_question, in this main conversation (subagents cannot ask). DevFlow counts only answers given through ask_user_question: an answer typed as ordinary chat text is not recorded as an answer. A question the human cancels or skips is not an answer; ask it again. If ask_user_question is unavailable, stop and explain why; never write an answer yourself.

**Records.** The runner owns the records, the run's ownership, the review count and every approval. Never run the hook yourself, invent events, edit records under `.devflow/`, or fill in answers for the human.
