---
name: flow
description: Run DevFlow Plan and, in the same conversation, continue into DevFlow Implement with the published plan.
---

Read the [Codex runner contract](../../references/codex-runner.md) before executing this operation. Use its trusted hook context, invocation, waiting and record-handling rules.

The human explicitly invoked $devflow:flow. The hook has already created a Plan run exactly as for $devflow:plan; the startup options apply to Plan only. Read the bundled codex-skills/plan/SKILL.md beside this skill and follow it as if the human had invoked $devflow:plan. Its rules not to implement and to offer $devflow:implement PATH for a separate human turn are replaced, for this run, by the Flow section of the bundled references/plan-workflow.md, which says when and how to continue into Implement with `implement start --from-plan PLAN_RUN_ID` (add `--workspace "WORKSPACE_PATH"` for another worktree).
