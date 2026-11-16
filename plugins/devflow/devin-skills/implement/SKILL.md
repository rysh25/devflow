---
name: implement
description: "Implement an explicit plan file with DevFlow: checks and independent reviews."
triggers: [user]
---

First read the Devin runner contract, `references/devin-runner.md` in the plugin (the directory above the hook context's `runner` script's `scripts/` directory). Follow it for the hook context, the runner command, waiting, approval and questions.

The human typed /devflow:implement PATH, optionally with `--reviewer AGENT MODEL EFFORT ...` and `--max-reviews N`, then `--` before PATH. The hook saved a pending start. Run `implement doctor` to see that pending input, then `implement start --plan EXACT_PATH`, adding `--workspace` only when another worktree suits the task. One workspace holds at most one unfinished implementation run; to work beside another one, use a different worktree (outside the project or in a Git-ignored directory) and pass it with `--workspace` while this conversation stays at the project root. Start freezes the settings; if it fails, fix the reported prerequisite and run the same start again.

Read `references/implement-workflow.md` beside the contract and follow its state loop: implement only the frozen plan in the recorded workspace, keep existing changes, checkpoint, check, review and reflect. Do not edit the input plan, `devflow.config.json`, `devflow.config.local.json` or run records. Only the runner's `check` counts as validation. Run `check` and `review` once each with exec and wait with get_output as the contract says. When a human decision is needed, ask with ask_user_question. At the review limit, show `review_summary_markdown` unchanged, ask the continue/stop/accept question and resolve only with a recorded answer. Never run the human-only `recover` yourself. In the final report, tell completed, accepted, exhausted and failed checks apart.
