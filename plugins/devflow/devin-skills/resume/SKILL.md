---
name: resume
description: "Resume a DevFlow run selected by its ID."
triggers: [user]
---

First read the Devin runner contract, `references/devin-runner.md` in the plugin (the directory above the hook context's `runner` script's `scripts/` directory). Follow it for the hook context, the runner command, waiting, approval and questions.

The hook has already handled /devflow:resume RUN_ID. Run `status` for that run and confirm that `owner_host` is `devin` and that this session owns it; then read that workflow's reference (`references/plan-workflow.md` or `references/implement-workflow.md`) and continue its state loop. A run that belongs to another host is not resumed here; explain the reason and do not move, rewrite or replace it. The frozen settings and reviewers stay as they are; do not repeat the start. If an unsealed interview belongs to another session, stop and explain that its original session is needed. Never run the human-only `recover` yourself.
