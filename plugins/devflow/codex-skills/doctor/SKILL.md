---
name: doctor
description: Diagnose DevFlow project settings and offline host prerequisites.
---

Read the [Codex runner contract](../../references/codex-runner.md) before executing this operation. Use its trusted hook context, invocation, waiting and record-handling rules.

Run doctor for shared plus local project configuration and offline host prerequisites. This operation does not connect to the conversation API. A caller may explicitly request plan doctor with --session for that probe; Plan performs it before the interview. For a requested run, pass --run RUN_ID before doctor to inspect its frozen configuration. Use implement doctor for a pending Implement start. Static checks do not verify authentication, billing, models, native question availability or successful interview execution. Report each failure and uncertainty faithfully. This operation creates no run, binding or event, changes no settings and performs no login or model call. Diagnose invalid and unsupported records without repairing or releasing them.
