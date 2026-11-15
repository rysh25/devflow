---
name: status
description: "Show DevFlow run status without changing anything."
triggers: [user]
---

First read the Devin runner contract, `references/devin-runner.md` in the plugin (the directory above the hook context's `runner` script's `scripts/` directory), and use its hook context and runner command.

Run `status` with the delivered session. For one requested run ID, put `--run RUN_ID` before `status`. Explain the flow, status, next action and any pending question; show `review_summary_markdown` unchanged when it is present instead of repeating it. This operation creates no run, binding or event and releases nothing. Never resume, start reviewers, reset counters or repair records. If the run is unfinished, offer `/devflow:resume RUN_ID`.
