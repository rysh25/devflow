---
name: plan
description: "Create a DevFlow plan: a human interview, then independent reviews."
triggers: [user]
---

First read the Devin runner contract, `references/devin-runner.md` in the plugin (the directory above the hook context's `runner` script's `scripts/` directory). Follow it for the hook context, the runner command, waiting, approval and questions.

The human typed /devflow:plan. The hook has already created the run and frozen the request and the effective settings, including any `--reviewer AGENT MODEL EFFORT` options; without them the project settings apply. Read `request.json` in the run directory instead of restating the request.

1. Run `plan status`, then `plan doctor`. On Devin, doctor checks the configured interview skill statically only (there is no conversation API check, so do not expect `conversation_api`). Report any problem before the interview.
2. Use the exact configured interview skill (`plan.grill_skill`, `interview_skill` in the hook context) with Devin's skill tool and follow its instructions. Do not replace it with another skill or imitate a skill that is blocked. Ask every interview question with ask_user_question in this conversation; ordinary chat answers are not counted, and a cancelled or skipped question must be asked again.
3. Write only runner input files in the run's `draft/` directory. Then seal the interview, write the plan, and let the runner alternate the reviewers; reflect on every finding. Never change project source, implement, commit or deploy.

Read `references/plan-workflow.md` beside the contract and follow its state loop. At the review limit, show `review_summary_markdown` exactly as written, then ask the runner's exact question with ask_user_question and the options continue, stop, accept and finish, in that order. Resolve only with `status.human_events` (its exact `answer_text` and event IDs). For a completed or human-accepted run, publish and report through the runner, show the summary unchanged, and offer `/devflow:implement PATH` for a separate human turn without copying startup options. An accepted plan is the human's acceptance at the limit, not approval by every reviewer.
