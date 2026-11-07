---
name: status
description: Inspect DevFlow run status without changing records.
---

Read the [Codex runner contract](../../references/codex-runner.md) before executing this operation. Use its trusted hook context, invocation, waiting and record-handling rules.

Run status with the delivered actual session. For exactly one requested run ID, insert --run RUN_ID before status. Explain the current flow, status, next action and pending questions. Show review_summary_markdown unchanged when present and avoid repeating it. This operation creates no run, binding or event and does not release pending or completed records. Never resume, invoke reviewers, reset counters or repair data. If unfinished, offer $devflow:resume RUN_ID.
