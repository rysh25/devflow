---
name: plan
description: Start DevFlow Plan with a human interview and independent reviews.
---

Read the [Codex runner contract](../../references/codex-runner.md) before executing this operation. Use its trusted hook context, invocation, waiting and record-handling rules.

The human explicitly invoked $devflow:plan. The hook has already created the run and frozen the request and effective configuration, including optional --reviewer AGENT MODEL EFFORT arguments. Without those arguments, project settings apply unchanged. Read request.json from this run; do not reconstruct it. Before asking interview questions, run plan doctor with the actual --session from this hook. Require conversation_api.checked and conversation_api.ready to be true; report API/permission failures before conducting the interview. This probes initialization, root identity and the first items page without a model call; it cannot guarantee later full capture. Read the bundled references/plan-workflow.md beside the runner's scripts directory and follow its Codex instructions and shared state loop.

Use the exact frozen plan.grill_skill from Codex's native catalog and read that skill's actual instructions, as Codex normally uses skills. Preserve its questioning method, keep questions in this root conversation and write only runner input files to draft/. No fallback skill or imitation of a blocked skill is allowed. Static discovery is not runtime proof. Resolve all initial questions with real native question answers, then seal, plan, alternate reviews and reflect on every finding. Never change project source, implement, commit or deploy.

At a review limit show review_summary_markdown verbatim before asking the runner's exact continue/stop/accept question. Resolve using only status.human_events with exact answer_text and event IDs. On completed or human-accepted runs publish and report through the runner; show its summary unchanged and publication verification separately. Offer $devflow:implement PATH for a separate human turn, without copying startup overrides. Human-accepted means accepted at the limit, not approval by all reviewers.
