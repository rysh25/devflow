---
name: plan
description: Create a plan from an explicit user request, interview the human with the configured model-invocable skill, alternate independent reviews up to six attempts, and export only a fully reviewed Markdown plan. Never implement code.
argument-hint: "<request>"
disable-model-invocation: true
allowed-tools: Bash(uv run --offline --no-project --no-config --no-python-downloads "${CLAUDE_PLUGIN_ROOT}/scripts/devflow.py" *)
---

# DevFlow plan

Original user arguments: $ARGUMENTS

Invocation context (expanded by Claude Code, not shell variables to guess):

- Project directory: `${CLAUDE_PROJECT_DIR}`
- Current session ID: `${CLAUDE_SESSION_ID}`
- Installed plugin directory: `${CLAUDE_PLUGIN_ROOT}`

Use these concrete values in every runner command and in the workflow reference's templates. Ordinary Read of a reference file does not expand placeholders. The hook-provided run directory identifies this run's immutable `request.json`.

Do not run shell discovery commands such as `pwd`, `cd`, `which`, `env`, `uv --version`, or `claude --version`. Use the expanded context above, Read/Grep/Glob for files, and the bundled runner's `doctor` for diagnostics. If a required placeholder is still literal, stop and report it; do not guess paths or weaken the guard.

Use this skill only after the human explicitly invokes /devflow:plan. Work in the current main Claude Code conversation. Do not fork the interview or plan revisions into a separate Claude session. Do not implement code, install dependencies, commit, or deploy.

Read `${CLAUDE_PLUGIN_ROOT}/references/plan-workflow.md` completely before acting. Follow its state-driven loop using the bundled runner. The UserPromptSubmit hook must already have captured the original request; do not synthesize events or call the hook yourself.

The configured external interview skill (default `grilling`, setting `plan.grill_skill`) must be installed and callable by Claude in this conversation. Inspect the runner's interview-skill diagnostic before starting the interview. `disable-model-invocation: true` means Claude cannot call that skill; `user-invocable: false` does not prevent model invocation. If absent, blocked, or unresolved, explain the exact status and obtain the user's direction; do not substitute another skill, invoke a wrapper, reproduce a blocked workflow, edit third-party frontmatter, or install anything silently. A static diagnostic is not proof of successful Skill execution.

Pass DevFlow's execution constraints in the interview Skill call's arguments: human interview and fact-finding stay in this main conversation; use Read/Grep/Glob for repository facts and the bundled doctor for environment diagnostics; do not spawn Agent/Task, run arbitrary shell commands, or edit project source. These constraints also apply inside the loaded skill, even when it recommends delegated research. Preserve its questioning method and wait for actual human answers. If essential facts cannot be obtained safely within these tools, stop for human direction. This is a constrained use of an allowed skill, never a fallback for a refused skill.

The runner, not memory or a task list, owns the review count, ordering, approved hash, and human decision records. Keep ordinary requirement-preserving revisions automatic. Escalate new scope, unsafe actions, and decisions requiring the human.

On completed reviews, run `plan publish` and inspect publication status before reporting a formal plan as saved. A sixth-round revision is not automatically approved. Never start implementation from this skill; offer the exact `/devflow:implement PATH` for a separate user turn.
