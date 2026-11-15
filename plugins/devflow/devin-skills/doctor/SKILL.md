---
name: doctor
description: "Check DevFlow's project settings and offline prerequisites."
triggers: [user]
---

First read the Devin runner contract, `references/devin-runner.md` in the plugin (the directory above the hook context's `runner` script's `scripts/` directory), and use its hook context and runner command.

Run `doctor` for the shared and local project settings and the offline host prerequisites. For a requested run, put `--run RUN_ID` before `doctor` to see its frozen settings; use `implement doctor` for a pending Implement start. Doctor checks files and versions only: it does not verify sign-in, models, workspace trust, ask_user_question or a real interview. If Devin has not trusted this project yet, tell the human to start `devin` in the project once and trust it; never skip the trust check. Report every failure and uncertainty as it is. This operation creates no run, binding or event, changes no settings and performs no sign-in or model call.
