---
name: plan
description: "Create a DevFlow plan: a human interview, then independent reviews."
triggers: [user]
---

First read the Devin runner contract, `references/devin-runner.md` in the plugin (the directory above the hook context's `runner` script's `scripts/` directory). Follow it for the hook context, the runner command, waiting, approval and questions.

The human typed /devflow:plan. The hook has already created the run and frozen the request and the effective settings, including any `--reviewer AGENT MODEL EFFORT` and `--max-reviews N` options; without them the project settings apply. Read `request.json` in the run directory instead of restating the request.

Before step 1, read `references/plan-workflow.md` beside the contract and follow its state loop.

1. Run `plan status`, then `plan doctor`. On Devin there is no conversation API check, so do not expect `conversation_api`. If a reviewer in `plan doctor`'s `reviewers` is not ready, stop before the interview as the shared references/plan-workflow.md step 1 says. Report any problem before the interview.
2. Use the exact configured interview skill (`plan.grill_skill`, `interview_skill` in the hook context) with Devin's skill tool and follow its instructions. Do not replace it with another skill or imitate a skill that is blocked. Ask every interview question with ask_user_question in this conversation; ordinary chat answers are not counted, and a cancelled or skipped question must be asked again.
3. Write only runner input files in the run's `draft/` directory. Then seal the interview, write the plan, and let the runner call each configured reviewer in turn; reflect on every finding. Never change project source, implement, commit or deploy.

At the review limit, ask the review limit question with ask_user_question as the `awaiting_human` row of `references/plan-workflow.md` says. Resolve only with `status.human_events` (its exact `answer_text` and event IDs). For a completed or human-accepted run, publish and report through the runner, show the summary unchanged, and offer `/devflow:implement PATH` for a separate human turn without copying startup options. An accepted plan is the human's acceptance at the limit, not approval by every reviewer.
