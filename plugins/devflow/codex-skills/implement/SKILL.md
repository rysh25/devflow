---
name: implement
description: Start DevFlow Implement from an explicit plan path, with checks and independent reviews.
---

Read the [Codex runner contract](../../references/codex-runner.md) before executing this operation. Use its trusted hook context, invocation, waiting and record-handling rules.

The human explicitly invoked $devflow:implement PATH, optionally with --reviewer AGENT MODEL EFFORT ... -- before PATH. The hook saved a pending start. Run implement doctor to inspect that pending input, then implement start --plan EXACT_PATH (and optional --workspace only when appropriate). Each workspace holds at most one unfinished implementation run; to work beside another implementation run, use a different worktree created outside the project or in a Git-ignored directory, with this conversation started at the project root and the worktree passed with --workspace. Settings are resolved and frozen by start; if it fails, fix the reported prerequisite and retry the same start before another ordinary input discards it.

Read the bundled references/implement-workflow.md beside the runner's scripts directory and follow its state loop. Preserve existing changes, modify only the frozen plan's scope in the recorded workspace, checkpoint, check, then review and reflect. Do not edit the input plan, devflow.config.json, devflow.config.local.json or run records. A passed ad hoc test does not replace the runner's check. Keep requested scope changes and human decisions explicit. At the limit show review_summary_markdown unchanged, ask the native continue/stop/accept question and resolve only a recorded candidate. Never run human-only recovery yourself. Terminal reports describe recorded code; distinguish completed, accepted, exhausted and failed checks.
