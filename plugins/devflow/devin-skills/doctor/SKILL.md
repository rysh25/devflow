---
name: doctor
description: "Check DevFlow's project settings and offline prerequisites."
triggers: [user]
---

First read the Devin runner contract, `references/devin-runner.md` in the plugin (the directory above the hook context's `runner` script's `scripts/` directory), and use its hook context and runner command.

Run `doctor` for the shared and local project settings and the offline host prerequisites. For a requested run, put `--run RUN_ID` before `doctor` to see its frozen settings; use `implement doctor` for a pending Implement start. Doctor checks files, versions and each reviewer CLI locally; when Devin is a reviewer, it checks whether Devin trusts the reviewer directory it reports as `reviewer_cwd` (the project, or the verified workspace of a selected Implement run). It does not verify sign-in, models, ask_user_question or a real interview. If Devin does not trust that directory, name the reported `reviewer_cwd` and tell the human to start `devin` once in the directory the error names (or a directory above it) and trust it. A worktree an Implement run has not started in yet is checked by `implement start`, since the reviewer starts there. Never skip the trust check. Report every failure and uncertainty as it is. This operation creates no run, binding or event, changes no settings and performs no sign-in; it normally calls no model (the exception is in DevFlow's docs/security.md).

Begin the reply with a line that is exactly `DevFlow <version>`, where `<version>` is the runner JSON's top-level `version` value copied verbatim, with nothing else on that line. If the runner returned no JSON, do not state or guess a version; report why it could not run.
