---
name: resume
description: Resume the DevFlow run explicitly selected by its ID.
---

Read the [Codex runner contract](../../references/codex-runner.md) before executing this operation. Use its trusted hook context, invocation, waiting and record-handling rules.

The hook has already processed the explicit $devflow:resume RUN_ID. Run status for that exact run, verify owner_host is codex and ownership matches this session, then read that flow's shared reference and continue its state loop. A different host is refused with a reason; do not transfer it, rewrite schema or create a replacement. Configurations and reviewer choices stay frozen. Do not replay start. If the original unsealed interview belongs to another session, stop and explain why that original session is needed. Human-only recover is never automatic.
