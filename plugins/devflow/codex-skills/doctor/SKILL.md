---
name: doctor
description: Diagnose DevFlow project settings and offline host prerequisites.
---

Read the [Codex runner contract](../../references/codex-runner.md) before executing this operation. Use its trusted hook context, invocation, waiting and record-handling rules.

Run doctor for shared plus local project configuration and offline host prerequisites. This operation does not connect to the conversation API. A caller may explicitly request plan doctor with --session for that probe; Plan performs it before the interview. For a requested run, pass --run RUN_ID before doctor to inspect its frozen configuration. Use implement doctor for a pending Implement start. Doctor also checks each reviewer CLI locally in the directory it reports as `reviewer_cwd` (the project, or the verified workspace of a selected Implement run), including whether Devin trusts it when Devin is a reviewer; name that reported directory when explaining a refusal. These checks do not verify authentication, billing, models, native question availability or successful interview execution. Report each failure and uncertainty faithfully. This operation creates no run, binding or event, changes no settings and performs no login; it normally calls no model (the exception is in DevFlow's docs/security.md). Diagnose invalid and unsupported records without repairing or releasing them.

Begin the reply with a line that is exactly `DevFlow <version>`, where `<version>` is the runner JSON's top-level `version` value copied verbatim, with nothing else on that line. If the runner returned no JSON, do not state or guess a version; report why it could not run.
