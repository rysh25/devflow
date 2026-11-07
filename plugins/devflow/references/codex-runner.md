# Codex runner contract

Use only the actual `host=codex`, `session_id`, `project`, and `runner` delivered by the trusted UserPromptSubmit hook for this operation. If any is absent, stop and report the missing hook context; never infer IDs from environment variables or another session. No Claude placeholder expansion is available here.

Run the bundled runner with concrete values substituted and quoted for the shell:

```text
uv --no-cache run --offline --no-project --no-config --no-python-downloads "RUNNER" --host codex --project "PROJECT" --session "SESSION_ID" COMMAND
```

All global arguments precede the command. Read each JSON result. Use exec_command for long calls and poll its returned session_id with write_stdin until completion; do not replay the command or start another action while it runs. Use Codex's standard permissions. If an operation needs additional permission, explain the measured need and request standard approval; never change user settings or silently retry with broader permissions.

The runner owns records, ownership, review budgets and approvals. Never call hooks yourself, fabricate events, edit persisted records, or supply answers to the question tool. When the workflow needs a human answer, ask in the root conversation with native request_user_input. If it is unavailable, stop that pending action and explain `codex --enable default_mode_request_user_input`; do not replace it with chat answers or an invented tool result.
