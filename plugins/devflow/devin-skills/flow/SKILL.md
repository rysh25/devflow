---
name: flow
description: "Run DevFlow Plan, then continue into DevFlow Implement with the published plan in the same conversation."
triggers: [user]
---

First read the Devin runner contract, `references/devin-runner.md` in the plugin (the directory above the hook context's `runner` script's `scripts/` directory). Follow it for the hook context, the runner command, waiting, approval and questions.

The human typed /devflow:flow. The hook has already created a Plan run exactly as for /devflow:plan; the startup options apply to Plan only. Read `devin-skills/plan/SKILL.md` in the plugin and follow it as if the human had typed /devflow:plan. Its rules not to implement and to offer `/devflow:implement PATH` for a separate human turn are replaced, for this run, by the Flow section of `references/plan-workflow.md`, which says when and how to continue into Implement with `implement start --from-plan PLAN_RUN_ID` (add `--workspace "WORKSPACE_PATH"` for another worktree).
