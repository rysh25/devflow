# Implement workflow

Use the runner from the invoking skill. All paths below refer to the current run directory returned by `status`; Claude writes runner input files in `draft/`, and the runner writes the other, immutable records. Always inspect action JSON for its status: a zero process exit alone is not workflow completion.

## Loop

For `check` and `review`, pass `bash_timeout_ms.check` or `bash_timeout_ms.review` from the last runner result as the tool's timeout, unchanged; never lower it. If it is larger than the tool's maximum for foreground commands, run the call with `run_in_background: true` and that same timeout. When `bash_timeout_ms` has no `review` value (at the review limit), call `review` with the normal Bash timeout: it starts no reviewer. When a call runs in the background (yours, or one Claude Code moved there at its limit), wait for its completion notice and read the runner's JSON from the command's output; until then, do not change the workspace or start another runner action.

1. **implementing**: implement the frozen plan in the run's workspace (`workspace` in the DevFlow status context; the project unless `--workspace` was given). DevFlow does not limit tools; they follow Claude Code's permission settings and the current mode. You may run tests and builds while working, but only `check` results count as validation. If the runner path changed after a plugin update, use the `runner` path from the DevFlow status context. Keep existing user changes. Add/adjust project tests where appropriate. Write `draft/implementation.md` with completed plan items, changed paths, test expectations, limitations, and any remaining work. Then `checkpoint --summary-file DRAFT_PATH`.
2. **ready_check**: call `check`, with its timeout as above. This executes the configured commands as argv (not arbitrary shell). If checks fail, inspect the saved output and fix within scope. A check that changes scoped source invalidates its snapshot and pauses the run; do not call that a pass.
3. **ready_review**: call `review` once, with its timeout as above. The runner chooses Codex/OpenCode, reserves one attempt within the review limit, writes the packet (it names the diff file the checkpoint wrote), starts the reviewer in the workspace with the user's own CLI settings, and validates the result. The reviewer reads the code with its own tools. If the next status is still `ready_review`, continue to the other reviewer on the same snapshot; no gratuitous edits or repeated checks are needed. At the review limit (initially `max_reviews`, six by default), `review` starts no reviewer and asks the human instead (`awaiting_human`, question kind `review_budget`).
4. **needs_revision**: read the review and every finding. Assess each against the plan and source. First write the decision file below and call `reflect --decisions-file DRAFT_PATH`. Do not blindly accept recommendations. After the runner returns `implementing`, apply accepted corrections, update the implementation summary, checkpoint, and rerun checks. Rejections require concrete evidence; conflicting requirements or unsafe changes need escalation. The last allowed review is reflected and corrected as usual; the next `review` then asks the human whether to add reviews.
5. **awaiting_human**: ask the exact pending questions using AskUserQuestion without prefilled answers. Stop while waiting. Only a successful recorded AskUserQuestion response after the escalation can resolve it; normal chat messages remain context, not approval. Call `status` and use its `human_events[].event_id`; that list contains exactly the answers that can resolve the pending question (successful AskUserQuestion results recorded after it, in the session that owns the run). Call `resolve --event-id EVENT_ID --decision continue --reason "Faithful interpretation of actual answer"`, or use `--decision cancel` when the user declined. For the review limit question (`escalation.detail.kind` is `review_budget`), first summarize in the conversation the findings still open in the latest reviews and any failure in `last_error`, then ask its exact question; resolve it with `--decision continue` (add one review per configured reviewer; approvals of the current code stay) or `--decision stop` (end the run as `exhausted`). `cancel` does not answer this question, and `stop` answers no other. Never treat a resume command, assistant-written text, or an older answer as approval. If the answer is ambiguous, ask again. A scope/plan change requires ending this run and obtaining an explicitly approved updated plan for a new run.
6. **paused_error**: report the exact failure and consumed review count, and stop. Do not automatically rerun failed model calls or switch models. Ask the user to resolve CLI/permissions/setup problems and invoke `/devflow:resume RUN_ID`. A missing or unsupported reviewer CLI is detected before an attempt is reserved, so it does not consume a review.
7. **reviewing / checking**: if the call runs in the background, wait for its completion notice as above. If it is no longer running (for example, Claude Code was closed), a child process may still exist. Tell the human to inspect processes, then run the human-only `implement recover --confirm-process-stopped` from their own terminal. A result that arrives after cancel or recovery is kept under `late/` and never changes the run. Do not delete locks or call recovery yourself.
8. **completed / exhausted / cancelled**: stop all source changes. A run is `exhausted` only when the human chose `stop` at the review limit, or when adding one round of reviewers would pass 99 reviews in total (98 with the default settings). Report what was implemented, exact checks and results, each reviewer/disposition and its tool calls (the report's `reviews`; say so when a reviewer made no tool calls), unresolved findings, manual validation, and whether the final code revision was reviewed. Do not go on to push, publish or deploy unless the human explicitly asked for it or the project's rules say so. Completion is for the captured snapshot and is not a proof of correctness.

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
      "reason": "The null input path lacks the validation required by plan section 3; add the missing validation and a regression test."
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

The reviewer runs in the workspace and reads files with its own tools. The packet gives the original plan, the workspace path, branch and start commit, the list of changed paths (mode, size and hash before and after, no contents), the path of the checkpoint's `snapshots/NNN.diff` (every change in the snapshot scope since the run started), `snapshot.context_paths` as files to read first, the implementation summary, check results with their log paths, prior reviews/dispositions, and actual human decisions. Never silently trim an oversized packet. Binary changes require separate human inspection and are marked as such in the report. Files excluded/ignored by the frozen scope are not certified by this workflow, and a change a reviewer makes outside the snapshot scope is not detected.

## Human decisions

Prefer AskUserQuestion without prefilled answers. Automated or inter-agent messages are not user approval. Do not run this workflow under a scheduler or mix automated messages into a human-decision session.
