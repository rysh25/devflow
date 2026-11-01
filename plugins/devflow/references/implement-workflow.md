# Implement workflow

Use the runner from the invoking skill. All paths below refer to the current run directory returned by `status`; only `draft/` is editable by Claude. The runner writes immutable artifacts. Always inspect action JSON for its status: a zero process exit alone is not workflow completion.

## Loop

1. **implementing**: implement the frozen plan with Read/Edit/Write in the approved scope; search with Grep/Glob where available or one Bash read-only search command at a time (`grep`, `rg`, `find`, `ls`, `cat`, `head`, `tail`, `wc`; no pipes or redirection). If the runner path is denied after a plugin update, use the `runner` path from the DevFlow status context. Keep existing user changes. Add/adjust project tests where appropriate. Write `draft/implementation.md` with completed plan items, changed paths, test expectations, limitations, and any remaining work. Then `checkpoint --summary-file DRAFT_PATH`.
2. **ready_check**: call `check`. This executes the configured commands as argv (not arbitrary shell). If checks fail, inspect the saved output and fix within scope. A check that changes scoped source invalidates its snapshot and pauses the run; do not call that a pass.
3. **ready_review**: call `review` once. The runner chooses Codex/OpenCode, reserves one of at most six attempts, freezes the packet, invokes a read-only/packet-only reviewer, and validates the result. If the next status is still `ready_review`, continue to the other reviewer on the same snapshot; no gratuitous edits or repeated checks are needed.
4. **needs_revision**: read the review and every finding. Assess each against the plan and source. First write the decision file below and call `reflect --decisions-file DRAFT_PATH`. Do not blindly accept recommendations. After the runner returns `implementing`, apply accepted corrections, update the implementation summary, checkpoint, and rerun checks. Rejections require concrete evidence; conflicting requirements or unsafe changes need escalation. The sixth review may be reflected and corrected, but no seventh review is dispatched.
5. **awaiting_human**: ask the exact pending questions using AskUserQuestion without prefilled answers. Stop while waiting. Only a successful recorded AskUserQuestion response after the escalation can resolve it; normal chat messages remain context, not approval. Call `status` and use its `human_events[].event_id`; that list contains exactly the answers that can resolve the pending question (successful AskUserQuestion results recorded after it, in the session that owns the run). Call `resolve --event-id EVENT_ID --decision continue --reason "Faithful interpretation of actual answer"`, or use `--decision cancel` when the user declined. Never treat a resume command, assistant-written text, or an older answer as approval. If the answer is ambiguous, ask again. A scope/plan change requires ending this run and obtaining an explicitly approved updated plan for a new run.
6. **paused_error**: report the exact failure and consumed review count, and stop. Do not automatically rerun failed model calls or switch models. Ask the user to resolve CLI/permissions/setup problems and invoke `/devflow:resume RUN_ID`. A missing or unsupported reviewer CLI is detected before an attempt is reserved, so it does not consume a review.
7. **reviewing / checking** on recovery: a child process may still exist. Tell the human to inspect processes, then run the human-only `implement recover --confirm-process-stopped` from their own terminal. A result that arrives after cancel or recovery is kept under `late/` and never changes the run. Do not delete locks or call recovery yourself.
8. **completed / exhausted / cancelled**: stop all source changes. Report what was implemented, exact checks and results, each reviewer/disposition, unresolved findings, manual validation, and whether the final code revision was reviewed. Do not continue to commit, push, or deploy. Completion is for the captured snapshot and is not a proof of correctness.

## Finding dispositions

Write this shape to `draft/decisions.json`; include exactly one entry for every finding ID from the latest review. Allowed actions: `accept`, `reject`, `already_addressed`, `escalate`. If a review contains only human questions and no findings, use an empty decisions array after the human resolution.

```json
{
  "review_number": 1,
  "summary": "Assessed the findings against the frozen plan and current source.",
  "decisions": [
    {
      "finding_id": "F1",
      "action": "accept",
      "reason": "The null input path lacks the validation required by plan section 3; add a guard and a regression test."
    }
  ]
}
```

## Escalate an implementation decision

Write `draft/questions.json`, then call `escalate --questions-file DRAFT_PATH` before asking the user:

```json
{
  "reason": "The plan and existing public API impose conflicting requirements.",
  "questions": ["May the public API change, or must backward compatibility be preserved?"]
}
```

Escalate requirements changes, destructive operations, dependencies/new services/costs, security/privacy decisions, conflicting reviewers, inaccessible tests, and unexplained failures. Keep ordinary evidence-based code fixes automatic.

## Review coverage

The packet includes the original plan, complete baseline-to-candidate text changes, full changed text files, explicit context files, a scoped file manifest, implementation summary, check results, prior reviews/dispositions, and actual human decisions. Reviewers cannot browse the project for missing context. Configure `snapshot.context_paths` before a new run when unchanged supporting files are necessary. Never silently trim an oversized packet. Binary changes require separate human inspection and are marked as such in the report. Files excluded/ignored by the frozen scope are not certified by this workflow.

## Human decisions

Prefer AskUserQuestion without prefilled answers. Automated or inter-agent messages are not user approval. Do not run this workflow under a scheduler or mix automated messages into a human-decision session.
