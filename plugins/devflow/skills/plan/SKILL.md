---
name: plan
description: Create a plan from an explicit user request, interview the human with the configured model-invocable skill, alternate independent reviews up to max_reviews attempts (more only with the human's approval at the limit), and export a fully reviewed or human-accepted Markdown plan with its approval record. Never implement code.
argument-hint: "[--reviewer AGENT MODEL EFFORT ... --] <request>"
disable-model-invocation: true
allowed-tools: Bash(uv --no-cache run --offline --no-project --no-config --no-python-downloads "${CLAUDE_PLUGIN_ROOT}/scripts/devflow.py" *) PowerShell(uv --no-cache run --offline --no-project --no-config --no-python-downloads "${CLAUDE_PLUGIN_ROOT}/scripts/devflow.py" *)
---

# DevFlow plan

Original user arguments: $ARGUMENTS

Always pass `--host claude` to the runner.

Invocation context (expanded by Claude Code, not shell variables to guess):

- Project directory: `${CLAUDE_PROJECT_DIR}`
- Current session ID: `${CLAUDE_SESSION_ID}`
- Installed plugin directory: `${CLAUDE_PLUGIN_ROOT}`

Use these concrete values in every runner command and in the workflow reference's templates. Ordinary Read of a reference file does not expand placeholders. The hook-provided run directory identifies this run's immutable `request.json`.

Use the expanded context above for paths and the session. Research with any tools your Claude Code settings allow: reading and searching files, the web, Bash, skills, subagents and MCP tools. DevFlow does not limit them. Planning does not change the project: the only files you write are runner input files in the run's draft directory, and source and documents stay unchanged. Ask the human only from this main conversation; an answer given inside a subagent is not recorded. Run the bundled runner as a single command, and use its `doctor` for diagnostics. If a required placeholder is still literal, stop and report it; do not guess paths.

Use this skill only after the human explicitly invokes /devflow:plan. Hold the interview and plan revisions in the current main Claude Code conversation. Do not implement code, install dependencies, commit, or deploy.

Read `${CLAUDE_PLUGIN_ROOT}/references/plan-workflow.md` completely before acting. Follow its state-driven loop using the bundled runner. The UserPromptSubmit hook must already have captured the original request; do not synthesize events or call the hook yourself.

The configured external interview skill (default `grilling`, setting `plan.grill_skill`) must be installed and callable by Claude in this conversation. Inspect the runner's interview-skill diagnostic before starting the interview. `disable-model-invocation: true` means Claude cannot call that skill; `user-invocable: false` does not prevent model invocation. If absent, blocked, or unresolved, explain the exact status and obtain the user's direction; do not substitute another skill, invoke a wrapper, reproduce a blocked workflow, edit third-party frontmatter, or install anything silently. A static diagnostic is not proof of successful Skill execution.

Pass two points in the interview Skill call's arguments: questions to the human stay in this main conversation, and planning does not change the project (only the draft directory is written). Fact-finding may use any tool, including subagents the skill suggests. Preserve its questioning method and wait for actual human answers. This is a use of the configured skill, never a fallback for a refused skill.

The runner, not memory or a task list, owns the review count, ordering, approved hash, and human decision records. Keep ordinary requirement-preserving revisions automatic. Escalate new scope, unsafe actions, and decisions requiring the human.

On completed reviews, or a plan the human accepted at the review limit (`accepted`), run `plan publish` and inspect publication status before reporting a formal plan as saved. A revision made after the last allowed review is not automatically approved, and an accepted plan is the human's acceptance, not approval by every reviewer. In every terminal state, run `plan report` and show its `review_summary_markdown` unchanged; then report only what the summary does not show. Never start implementation from this skill; offer the exact `/devflow:implement PATH` for a separate user turn, and for an accepted plan say that the human accepted it and give its approval record's path.

Startup reviewer arguments replace the complete reviewer list for this run. Without them the effective project settings apply unchanged. The hook parses options; never invent or replay the start event. Plan handoff commands do not inherit these arguments.
