"""Offline tests for the shared reviewer CLIs and the reviewer settings rules.

Covers ``devflow_core.common.reviewers`` (Codex and OpenCode adapters used by
every workflow) and ``devflow_core.common.settings.validate_reviewer(s)``.
Fake local processes only; never contacts model APIs. Each workflow's review
fields are covered by tests/<workflow>/test_review.py.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from devflow_core.common import reviewers, settings
from devflow_core.common.errors import DevFlowError, ReviewerError

MIB = 1024 * 1024
SENTINEL = "REVIEWER-INSTRUCTIONS-SENTINEL-7f3a"
REVIEW = {"schema_version": 1, "verdict": "pass", "summary": "問題なし", "findings": [], "questions": []}
FIXTURE_REVIEW = {**REVIEW, "summary": "fixture pass"}
#: The user's own OpenCode settings in the environment: DevFlow passes them on unchanged.
USER_ENV = {"OPENCODE_CONFIG": "/home/user/opencode.json", "OPENCODE_CONFIG_DIR": "/home/user/.opencode",
            "OPENCODE_CONFIG_CONTENT": '{"model": "x"}', "OPENCODE_PERMISSION": '{"edit": "deny"}',
            "OPENCODE_SERVER": "http://127.0.0.1:4096", "DEVFLOW_TEST_CREDENTIAL": "credential-sentinel"}
NO_TOOLS = {"tool_call_count": 0, "tool_calls": {}, "other_events": {}}


def reviewer(kind="codex", **changes):
    """A complete, valid reviewer entry."""
    return {"id": kind, "kind": kind, "command": [kind],
            "model": "gpt-6.1-sol" if kind == "codex" else "opencode-go/deepseek-v4.1-flash",
            "effort": "xhigh" if kind == "codex" else "high", "timeout_seconds": 10, **changes}


def accept(value):
    """Stand-in for a workflow's validate_review: exactly the review keys."""
    if not isinstance(value, dict) or set(value) != set(REVIEW):
        raise ReviewerError("Review has missing or unknown fields")
    return value


def lines(*events):
    return "\n".join(event if isinstance(event, str) else json.dumps(event) for event in events)


def codex(*items, end=({"type": "turn.completed", "usage": {}},)):
    return lines({"type": "thread.started", "thread_id": "thread-1"}, {"type": "turn.started"}, *items, *end)


def item(kind, item_type, identifier, **fields):
    return {"type": kind, "item": {"id": identifier, "type": item_type, **fields}}


def answer_item(value=REVIEW, identifier="answer"):
    return item("item.completed", "agent_message", identifier, text=json.dumps(value))


def oc(kind, session="main", **part):
    return {"type": kind, "sessionID": session, "part": part}


def opencode(*events, value=REVIEW, reason="stop"):
    """Events, then a last step with the answer that ends with ``reason``."""
    return lines(*events, oc("step_start"), oc("text", id="answer", text=json.dumps(value)),
                 oc("step_finish", reason=reason))


class JsonTests(unittest.TestCase):
    def test_duplicates_constants_and_fences_rejected(self):
        for value in ('{"verdict":"pass","verdict":"pass"}', '{"value": NaN}', '```json\n{}\n```', "not json"):
            with self.subTest(value=value), self.assertRaises(ReviewerError):
                reviewers.json_value(value)

    def test_text_bounds(self):
        self.assertEqual(reviewers.text("ok", "summary"), "ok")
        for value in ("", "   ", None, "a\0b", "x" * 32769):
            with self.subTest(value=value), self.assertRaises(ReviewerError):
                reviewers.text(value, "summary")


class CodexEventTests(unittest.TestCase):
    def test_a_completed_turn_with_no_tools(self):
        self.assertEqual(reviewers.parse_codex_events(codex(answer_item())), NO_TOOLS)

    def test_every_tool_and_subagent_is_counted_once_by_its_type(self):
        output = codex(
            item("item.started", "command_execution", "c1", command="cat a.py"),
            item("item.completed", "command_execution", "c1", command="cat a.py", exit_code=0),
            item("item.completed", "command_execution", "c2", command="pytest"),
            item("item.started", "mcp_tool_call", "m1", server="docs", tool="search"),
            item("item.updated", "mcp_tool_call", "m1", server="docs", tool="search"),
            item("item.completed", "mcp_tool_call", "m1", server="docs", tool="search"),
            item("item.completed", "web_search", "w1", query="q"),
            item("item.completed", "collab_tool_call", "s1", tool="spawn"),
            item("item.completed", "a_tool_added_in_a_later_version", "n1"),
            item("item.completed", "reasoning", "r1", text="thinking"),
            answer_item())
        self.assertEqual(reviewers.parse_codex_events(output), {
            "tool_call_count": 6, "other_events": {},
            "tool_calls": {"a_tool_added_in_a_later_version": 1, "collab_tool_call": 1, "command_execution": 2,
                           "mcp_tool_call": 1, "web_search": 1}})

    def test_errors_the_run_recovered_from_and_unknown_events_are_counted_not_failed(self):
        output = codex(
            {"type": "error", "message": "Reconnecting... 1/5"},
            item("item.completed", "error", "e1", message="a tool failed"),
            item("item.completed", "todo_list", "t1", items=[]),
            {"type": "an.event.added.later", "value": 1},
            '{"type": "item.completed", "item": {"id": "x", "id": "y", "type": "web_search"}}',
            "not a json line",
            answer_item())
        activity = reviewers.parse_codex_events(output)
        self.assertEqual(activity["tool_calls"], {"todo_list": 1, "web_search": 1})
        self.assertEqual(activity["other_events"], {"(unreadable line)": 1, "an.event.added.later": 1,
                                                    "error": 1, "error item": 1})

    def test_only_the_main_threads_turn_decides(self):
        other = "thread-2"
        child_failed = codex(answer_item(), end=(
            {"type": "turn.completed", "usage": {}},
            {"type": "thread.started", "thread_id": other},
            item("item.completed", "command_execution", "c9", thread_id=other),
            {"type": "turn.failed", "thread_id": other, "error": {"message": "child failed"}}))
        activity = reviewers.parse_codex_events(child_failed)
        self.assertEqual(activity["other_events"], {"turn.failed in another thread": 1})
        main_failed = codex(answer_item(), end=(
            {"type": "turn.failed", "error": {"message": "main failed"}},
            {"type": "turn.completed", "thread_id": other, "usage": {}}))
        with self.assertRaisesRegex(ReviewerError, "main failed"):
            reviewers.parse_codex_events(main_failed)
        # A failed main turn stays a failure even if a later turn completes.
        failed_then_completed = codex(answer_item(), end=(
            {"type": "turn.failed", "error": {"message": "main failed first"}},
            {"type": "turn.completed", "usage": {}}))
        with self.assertRaisesRegex(ReviewerError, "main failed first"):
            reviewers.parse_codex_events(failed_then_completed)
        named_main = lines({"type": "thread.started", "thread_id": "thread-1"},
                           {"type": "turn.completed", "thread_id": "thread-1", "usage": {}})
        self.assertEqual(reviewers.parse_codex_events(named_main), NO_TOOLS)

    def test_only_an_unfinished_or_failed_turn_fails(self):
        cases = {
            "no turn.completed": codex(answer_item(), end=()),
            "empty output": "",
            "failed last turn": codex(answer_item(), end=({"type": "turn.failed", "error": {"message": "quota"}},)),
        }
        for name, output in cases.items():
            with self.subTest(case=name), self.assertRaisesRegex(ReviewerError, "turn"):
                reviewers.parse_codex_events(output)


class OpenCodeEventTests(unittest.TestCase):
    def test_the_answer_is_the_main_sessions_last_step(self):
        value, activity = reviewers.parse_opencode_events(opencode())
        self.assertEqual((value, activity), (REVIEW, NO_TOOLS))

    def test_tools_steps_and_subagent_sessions_are_counted(self):
        output = opencode(
            oc("step_start"), oc("text", id="pre", text="Reading the files first."),
            oc("tool_use", id="t1", tool="read", state={"status": "completed"}),
            oc("tool_use", id="t1", tool="read", state={"status": "completed"}),
            oc("tool_use", id="t2", tool="shell", state={"status": "error"}),
            oc("tool_use", id="t3", tool="mcp_docs_search"),
            oc("tool_use", id="t4", tool="task"),
            oc("step_start", session="sub"), oc("tool_use", session="sub", id="t5", tool="grep"),
            oc("text", session="sub", id="s", text="not the answer"),
            {"type": "error", "sessionID": "sub", "error": {"name": "APIError"}},
            oc("step_finish", session="sub", reason="stop"),
            oc("reasoning", text="thinking"), oc("step_finish", reason="tool-calls"),
            {"type": "an_event_added_later", "sessionID": "main"}, "not a json line")
        value, activity = reviewers.parse_opencode_events(output)
        self.assertEqual(value, REVIEW)
        self.assertEqual(activity, {
            "tool_call_count": 5, "tool_calls": {"grep": 1, "mcp_docs_search": 1, "read": 1, "shell": 1, "task": 1},
            "other_events": {"(unreadable line)": 1, "an_event_added_later": 1, "error in a subagent session": 1}})

    def test_a_later_step_that_finishes_gives_the_answer(self):
        output = lines(oc("step_start"), oc("text", id="a", text='{"draft": true}'), oc("step_finish", reason="stop"),
                       oc("step_start"), oc("text", id="b", text=json.dumps(REVIEW)), oc("step_finish", reason="stop"))
        self.assertEqual(reviewers.parse_opencode_events(output)[0], REVIEW)

    def test_a_text_part_sent_again_counts_once_with_its_last_version(self):
        output = lines(oc("step_start"), oc("text", id="p", text='{"schema_version": 1, '),
                       oc("text", id="p", text=json.dumps(REVIEW)), oc("step_finish", reason="stop"))
        self.assertEqual(reviewers.parse_opencode_events(output)[0], REVIEW)

    def test_text_parts_whose_id_is_not_a_string_are_kept_in_order(self):
        # Each is a part of its own: none is lost, and nothing fails.
        half = json.dumps(REVIEW)[:20], json.dumps(REVIEW)[20:]
        for first, second in (({}, []), (None, None), (7, 7)):
            with self.subTest(ids=(first, second)):
                output = lines(oc("step_start"), oc("text", id=first, text=half[0]), oc("text", id=second, text=half[1]),
                               oc("step_finish", reason="stop"))
                self.assertEqual(reviewers.parse_opencode_events(output)[0], REVIEW)

    def test_only_an_unfinished_answer_or_a_main_session_error_fails(self):
        cases = {
            "error in the main session": (opencode({"type": "error", "sessionID": "main", "error": {"name": "X"}}),
                                          "reported an error"),
            "cut off at the length limit": (opencode(reason="length"), "did not finish"),
            "ended waiting for tools": (opencode(reason="tool-calls"), "did not finish"),
            "no finish": (lines(oc("step_start"), oc("text", id="a", text=json.dumps(REVIEW))), "did not finish"),
            "text after the finish": (lines(opencode(), oc("text", id="late", text="more")), "did not finish"),
            "no text": (lines(oc("step_start"), oc("step_finish", reason="stop")), "without an answer"),
            "a new step after the answer that never finished": (lines(opencode(), oc("step_start")), "no finish"),
            "a new step after the answer cut off": (lines(opencode(), oc("step_start"),
                                                           oc("step_finish", reason="length")), "did not finish"),
            "empty output": ("", "did not finish"),
        }
        for name, (output, message) in cases.items():
            with self.subTest(case=name), self.assertRaisesRegex(ReviewerError, message):
                reviewers.parse_opencode_events(output)

    def test_the_answer_itself_is_strict_json(self):
        for text in ('{"verdict":"pass","verdict":"pass"}', "```json\n{}\n```"):
            output = lines(oc("step_start"), oc("text", id="a", text=text), oc("step_finish", reason="stop"))
            with self.subTest(text=text), self.assertRaises(ReviewerError):
                reviewers.parse_opencode_events(output)


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.work = Path("run", "review-processes", "01").resolve()
        self.cwd = Path("project").resolve()
        self.schema = Path("schema.json").resolve()
        # The executable as configured (the real lookup is tested with the fake CLIs).
        resolve = patch.object(reviewers, "_resolve", lambda prefix: prefix)
        resolve.start()
        self.addCleanup(resolve.stop)

    def test_codex_runs_with_the_users_settings_in_the_directory_under_review(self):
        argv, env = reviewers.build_command(reviewer(), self.work, self.cwd, self.schema)
        self.assertEqual(argv, [
            "codex", "exec", "--json", "--skip-git-repo-check", "--cd", str(self.cwd), "--model", "gpt-6.1-sol",
            "--config", 'model_reasoning_effort="xhigh"', "--config", 'approval_policy="never"',
            "--output-schema", str(self.schema),
            "--output-last-message", str(self.work / "final-response.json"), "-"])
        self.assertEqual(env, {"PWD": str(self.cwd)})

    def test_opencode_runs_with_the_users_settings_in_the_directory_under_review(self):
        argv, env = reviewers.build_command(reviewer("opencode"), self.work, self.cwd, self.schema)
        self.assertEqual(argv, [
            "opencode", "run", "--format", "json", "--standalone", "--model", "opencode-go/deepseek-v4.1-flash#high",
            "--file", str(self.work / "review-input.md"), reviewers.OpenCode.message])
        # Only the working directory: the project's own settings and instructions load as usual.
        self.assertEqual(env, {"PWD": str(self.cwd)})

    def test_each_cli_is_one_class_and_preflight_checks_the_options_it_passes(self):
        self.assertEqual(set(reviewers.CLIS), set(settings.REVIEWER_KINDS))
        self.assertEqual(reviewers.CLIS["codex"].flags(), ["--json", "--skip-git-repo-check", "--cd", "--model",
                                                           "--config", "--output-schema", "--output-last-message"])
        self.assertEqual(reviewers.CLIS["opencode"].flags(), ["--format", "--standalone", "--model", "--file"])
        for kind, cli in reviewers.CLIS.items():
            with self.subTest(kind=kind):
                self.assertEqual(cli.arguments("m", "high", self.work, self.cwd, self.schema)[0], cli.subcommand)

    def test_the_users_command_prefix_is_kept(self):
        for kind in ("codex", "opencode"):
            with self.subTest(kind=kind):
                argv, _ = reviewers.build_command(reviewer(kind, command=["node", "/opt/cli.js", "--profile", "x"]),
                                                  self.work, self.cwd, self.schema)
                self.assertEqual(argv[:4], ["node", "/opt/cli.js", "--profile", "x"])

    def test_invalid_entries_are_reviewer_errors(self):
        incomplete = reviewer()
        del incomplete["id"]
        for config in (incomplete, reviewer(command="codex"), reviewer(model="gpt --flag"),
                       reviewer(max_output_bytes=1023), reviewer(timeout_seconds=0.2), reviewer(web=True)):
            with self.subTest(config=config), self.assertRaises(ReviewerError):
                reviewers.build_command(config, self.work, self.cwd, self.schema)


class ReviewerSettingsTests(unittest.TestCase):
    """The shared reviewer rules (common.settings), applied to every workflow."""

    def assertRejected(self, entry, message=None):
        with self.assertRaisesRegex(DevFlowError, message or ""):
            settings.validate_reviewer(entry)

    def reviewers(self, count, kinds=("codex", "opencode")):
        return [reviewer(kinds[index % len(kinds)], id=f"r{index}") for index in range(count)]

    def test_complete_entries_accepted(self):
        for entry in (reviewer(), reviewer("opencode"), reviewer(max_output_bytes=4096),
                      reviewer(command=["/opt/bash/codex"]), reviewer(command=["node", "C:\\tools\\codex.js"]),
                      reviewer(command=[sys.executable, "/tmp/fake reviewer.py", "pass"])):
            with self.subTest(entry=entry):
                self.assertIs(settings.validate_reviewer(entry), entry)

    def test_the_users_choice_of_how_to_run_the_cli_is_accepted(self):
        for command in (["bash", "-c", "codex \"$@\"", "codex"], ["cmd.exe", "/c", "codex"], ["pwsh", "codex.ps1"],
                        ["codex", "--yolo"], ["codex", "--dangerously-bypass-approvals-and-sandbox"],
                        ["opencode", "--dangerously-skip-permissions"], ["env", "CODEX_HOME=/x", "codex"]):
            for kind in ("codex", "opencode"):
                with self.subTest(command=command, kind=kind):
                    entry = reviewer(kind, command=command)
                    self.assertIs(settings.validate_reviewer(entry), entry)

    def test_required_and_unknown_fields_are_named(self):
        for field in sorted(settings.REVIEWER_REQUIRED):
            entry = reviewer()
            del entry[field]
            with self.subTest(missing=field):
                self.assertRejected(entry, f"missing: {field}")
        for field in ("api_key", "web", "protocol_version"):
            with self.subTest(unknown=field):
                self.assertRejected({**reviewer("opencode"), field: 2}, rf"unknown: {field}\).*README")
        self.assertRejected(["codex"], "object")

    def test_ids_and_kinds(self):
        settings.validate_reviewer(reviewer(id="codex-2_b"))
        for identifier in ("Codex", "1codex", "", "a" * 65, 7):
            with self.subTest(identifier=identifier):
                self.assertRejected(reviewer(id=identifier), "IDs")
        self.assertRejected(reviewer(kind="claude"), "kind")

    def test_command_must_be_an_argv_array(self):
        for command in ("codex; touch BAD", [], [""], ["co\0dex"], [1], ["codex"] * 129):
            with self.subTest(command=command):
                self.assertRejected(reviewer(command=command), "argv array")

    def test_model_and_effort_are_names_that_keep_devflows_own_arguments_intact(self):
        # Any provider's model name is accepted ...
        # A single quote is safe inside Codex's double-quoted effort="E".
        for change in ({"model": "anthropic/claude@2026+beta"}, {"model": "org:team/model.v2_1"},
                       {"effort": "x-high"}, {"effort": "high;low"}, {"effort": "it's"}):
            with self.subTest(change=change):
                settings.validate_reviewer({**reviewer(), **change})
        # ... except what would break the arguments DevFlow builds from them: an option,
        # a space, a double quote or backslash (Codex's effort="E"), or "#" (OpenCode's M#E).
        for change in ({"model": "gpt-6.1-sol --flag"}, {"model": "-m"}, {"effort": 'high"'}, {"effort": "a\\b"},
                       {"model": "m#variant"}, {"effort": ""}, {"model": None}):
            with self.subTest(change=change):
                self.assertRejected({**reviewer(), **change}, "Invalid reviewer")

    def test_timeout_bounds(self):
        for timeout in (5, 3600):
            settings.validate_reviewer(reviewer(timeout_seconds=timeout))
        for timeout in (4, 3601, 5.0, True, "10"):
            with self.subTest(timeout=timeout):
                self.assertRejected(reviewer(timeout_seconds=timeout), "timeout_seconds")

    def test_max_output_bytes_boundaries(self):
        self.assertEqual(settings.MAX_OUTPUT_BYTES, 16 * MIB)
        self.assertEqual(reviewers.MAX_OUTPUT_BYTES, 16 * MIB)
        for limit in (1024, 16 * MIB):
            with self.subTest(limit=limit):
                settings.validate_reviewer(reviewer(max_output_bytes=limit))
        for limit in (1023, 16 * MIB + 1, 0, True, 1024.0, "4096"):
            with self.subTest(limit=limit):
                self.assertRejected(reviewer(max_output_bytes=limit), "max_output_bytes")

    def test_reviewer_count_against_max_reviews(self):
        for count, accepted in ((1, False), (2, True), (3, True), (6, True), (7, False)):
            with self.subTest(count=count):
                if accepted:
                    self.assertEqual(len(settings.validate_reviewers(self.reviewers(count), 6)), count)
                else:
                    with self.assertRaisesRegex(DevFlowError, "two reviewers up to max_reviews"):
                        settings.validate_reviewers(self.reviewers(count), 6)
        with self.assertRaises(DevFlowError):
            settings.validate_reviewers(self.reviewers(3), 2)
        with self.assertRaises(DevFlowError):
            settings.validate_reviewers({"codex": reviewer()}, 6)

    def test_duplicate_ids_rejected(self):
        entries = [reviewer(id="same"), reviewer("opencode", id="same")]
        with self.assertRaisesRegex(DevFlowError, "unique"):
            settings.validate_reviewers(entries, 6)

    def test_at_least_two_kinds_required(self):
        for kind in ("codex", "opencode"):
            with self.subTest(kind=kind), self.assertRaisesRegex(DevFlowError, "two different reviewer kinds"):
                settings.validate_reviewers(self.reviewers(3, (kind,)), 6)
        entries = self.reviewers(3, ("codex", "codex", "opencode"))
        result = settings.validate_reviewers(entries, 6)
        self.assertEqual(result, entries)
        result[0]["command"].append("changed")
        self.assertEqual(entries[0]["command"], ["codex"])

    def test_every_entry_is_checked(self):
        entries = self.reviewers(3)
        entries[2]["command"] = []
        with self.assertRaisesRegex(DevFlowError, "argv array"):
            settings.validate_reviewers(entries, 6)


class OutputLimitTests(unittest.TestCase):
    def test_each_stream_has_its_own_limit_so_stderr_logs_do_not_use_up_stdout(self):
        code = "import sys; sys.stdout.write('o' * 3000); sys.stdout.flush(); sys.stderr.write('e' * 3000)"
        exit_code, stdout, stderr = reviewers._capture([sys.executable, "-c", code], limit=4096)
        self.assertEqual((exit_code, len(stdout), len(stderr)), (0, 3000, 3000))

    def test_a_single_stream_over_its_limit_is_a_failure(self):
        with self.assertRaisesRegex(ReviewerError, "exceeded the size limit"):
            reviewers._capture([sys.executable, "-c", "import sys; sys.stderr.write('e' * 9000)"], limit=4096)


FAKE = r'''
import json
import os
from pathlib import Path
import sys
import time

mode = sys.argv[1]
args = sys.argv[2:]
if "--version" in args:
    print("opencode 3.0.1" if mode == "pass" else "9.9.9")
    raise SystemExit(0)
if "--help" in args:
    flags = ["--json", "--output-schema", "--output-last-message", "--skip-git-repo-check", "--cd", "--model",
             "--config", "--format", "--file", "--standalone"]
    missing = {"old-codex": ["--output-schema"], "old-opencode": ["--standalone"]}.get(mode, [])
    print(" ".join(flag for flag in flags if flag not in missing))
    raise SystemExit(0)
codex = "--output-last-message" in args
records = Path(args[args.index("--output-last-message" if codex else "--file") + 1]).parent
# Every dispatched review attempt leaves one line in the review's own directory.
with open(records / "attempts.txt", "a", encoding="utf-8") as handle:
    handle.write(mode + "\n")
if mode == "timeout":
    time.sleep(30)
if mode == "too-large":
    print("x" * 65536)
    raise SystemExit(0)
if mode == "exit-error":
    print("fixture failure", file=sys.stderr)
    raise SystemExit(7)
stdin = sys.stdin.read()
(records / "seen.json").write_text(json.dumps({"args": args, "cwd": os.getcwd(), "pwd": os.environ.get("PWD"),
    "stdin": stdin, "env": {key: os.environ.get(key) for key in (
        "OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG_CONTENT", "OPENCODE_PERMISSION",
        "OPENCODE_SERVER", "OPENCODE_CONFIG_PROJECT_DISABLE", "OPENCODE_DISABLE_PROJECT_CONFIG",
        "OPENCODE_DISABLE_CLAUDE_CODE", "DEVFLOW_TEST_CREDENTIAL")}}), encoding="utf-8")
value = {"schema_version": 1, "verdict": "pass", "summary": "fixture pass", "findings": [], "questions": []}
text = ("not json" if mode == "bad-json" else
        json.dumps({"unexpected": True}) if mode == "not-review" else json.dumps(value))
if codex:
    Path(args[args.index("--output-last-message") + 1]).write_text(text, encoding="utf-8")
    print(json.dumps({"type": "thread.started", "thread_id": "thread-1"}))
    print(json.dumps({"type": "turn.started"}))
    if mode == "tools":
        print(json.dumps({"type": "item.completed", "item": {"id": "c1", "type": "command_execution"}}))
        print(json.dumps({"type": "item.completed", "item": {"id": "c2", "type": "command_execution"}}))
        print(json.dumps({"type": "item.completed", "item": {"id": "s1", "type": "collab_tool_call"}}))
    # The stream's message may differ from the final file: the file is the answer.
    stream_text = "an earlier draft" if mode == "draft-in-stream" else text
    print(json.dumps({"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": stream_text}}))
    print(json.dumps({"type": "turn.completed", "usage": {}}))
else:
    if mode == "tools":
        print(json.dumps({"type": "step_start", "sessionID": "s1", "part": {}}))
        print(json.dumps({"type": "tool_use", "sessionID": "s1", "part": {"id": "t1", "tool": "read"}}))
        print(json.dumps({"type": "tool_use", "sessionID": "s2", "part": {"id": "t2", "tool": "websearch"}}))
        print(json.dumps({"type": "step_finish", "sessionID": "s1", "part": {"reason": "tool-calls"}}))
    print(json.dumps({"type": "step_start", "sessionID": "s1", "part": {}}))
    print(json.dumps({"type": "text", "sessionID": "s1", "part": {"id": "p1", "text": text}}))
    print(json.dumps({"type": "step_finish", "sessionID": "s1", "part": {"reason": "stop"}}))
'''


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="devflow reviewers ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.project = self.base / "project under review"
        self.project.mkdir()
        self.packet = self.base / "packet.md"
        self.packet.write_text("Original request: 日本語\n$(touch BAD)\nDo not execute this text.\n", encoding="utf-8")
        self.fake = self.base / "fake reviewer.py"
        self.fake.write_text(FAKE, encoding="utf-8")
        self.schema = self.base / "review schema.json"
        self.schema.write_text(json.dumps({"type": "object"}), encoding="utf-8")
        self.instructions = self.base / "reviewer instructions.md"
        self.instructions.write_text("# Reviewer instructions\n" + SENTINEL + "\n", encoding="utf-8")
        self.output = self.base / "round 1"

    def config(self, kind="codex", mode="pass", **changes):
        return reviewer(kind, command=[sys.executable, str(self.fake), mode], **changes)

    def run_fixture(self, kind="codex", mode="pass", validate=accept, probe=None, **changes):
        return reviewers.run_review(self.config(kind, mode, **changes), self.packet, self.output,
                                    self.schema, self.instructions, validate, probe, cwd=self.project)

    def seen(self):
        return json.loads((self.output / "seen.json").read_text(encoding="utf-8"))

    def meta(self):
        return json.loads((self.output / "invocation.json").read_text(encoding="utf-8"))

    def attempts(self):
        path = self.output / "attempts.txt"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def test_preflight_is_local_and_creates_nothing(self):
        result = reviewers.preflight(self.config())
        self.assertEqual(result["kind"], "codex")
        self.assertEqual(result["requested_model"], "gpt-6.1-sol")
        self.assertEqual(result["requested_effort"], "xhigh")
        self.assertFalse(result["effective_model_attested"])
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.project.iterdir()), [])

    def test_preflight_accepts_any_version_that_has_the_flags(self):
        for kind in ("codex", "opencode"):
            with self.subTest(kind=kind):
                result = reviewers.preflight(self.config(kind))
                self.assertEqual(result["cli_version"], "opencode 3.0.1")
                # Newer than the checked version: a warning, never a refusal.
                self.assertEqual(len(result["version_warnings"]), 1)
                self.assertIn("is newer than", result["version_warnings"][0])

    def test_a_cli_without_a_flag_devflow_passes_stops_before_the_attempt(self):
        for kind, mode, flag in (("codex", "old-codex", "--output-schema"), ("opencode", "old-opencode", "--standalone")):
            self.output = self.base / mode
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(ReviewerError, f"lacks flags DevFlow passes: {flag}"):
                    self.run_fixture(kind, mode)
                self.assertEqual(self.attempts(), [])
                self.assertFalse((self.output / "review-input.md").exists())

    def test_missing_executable_and_cmd_shims_rejected(self):
        with self.assertRaisesRegex(ReviewerError, "not found"):
            reviewers.preflight(reviewer(command=["devflow-missing-reviewer-cli"]))
        for shim in ("C:/bin/codex.cmd", "C:/bin/codex.BAT"):
            with self.subTest(shim=shim), patch.object(reviewers.shutil, "which", return_value=shim), \
                    self.assertRaisesRegex(ReviewerError, r"\.cmd/\.bat"):
                reviewers.preflight(reviewer())

    def test_invalid_config_rejected_before_any_file(self):
        incomplete = self.config()
        del incomplete["id"]
        for config in (incomplete, {**self.config(), "command": []}):
            with self.subTest(config=config), self.assertRaises(ReviewerError):
                reviewers.run_review(config, self.packet, self.output, self.schema, self.instructions, accept,
                                     cwd=self.project)
            self.assertFalse(self.output.exists())

    def test_codex_process(self):
        result = self.run_fixture()
        self.assertEqual(result, {"review": FIXTURE_REVIEW, "activity": NO_TOOLS})
        self.assertEqual(self.attempts(), ["pass"])
        seen = self.seen()
        self.assertEqual(Path(seen["cwd"]), self.project)
        self.assertEqual(Path(seen["pwd"]), self.project)
        # The prompt arrives on standard input, which is then closed.
        prompt = (self.output / "review-input.md").read_text(encoding="utf-8")
        self.assertEqual(seen["stdin"], prompt)
        self.assertIn("日本語", prompt)
        self.assertIn("$(touch BAD)", prompt)
        self.assertEqual(list(self.base.rglob("BAD")), [])
        self.assertTrue((self.output / "stdout.jsonl").is_file())
        self.assertTrue((self.output / "stderr.log").is_file())
        meta = self.meta()
        self.assertEqual(set(meta), {"kind", "executable", "cli_version", "version_warnings", "requested_model",
                                     "requested_effort",
                                     "effective_model_attested", "cwd", "prefix_arg_count", "timeout_seconds",
                                     "max_output_bytes", "packet_sha256", "shell", "settings"})
        self.assertFalse(meta["effective_model_attested"])
        self.assertEqual(Path(meta["cwd"]), self.project)
        self.assertFalse(meta["shell"])
        self.assertEqual(meta["settings"], "the user's own CLI settings")
        self.assertEqual(meta["prefix_arg_count"], 2)
        self.assertEqual(meta["timeout_seconds"], 10)
        self.assertEqual(meta["max_output_bytes"], 16 * MIB)
        self.assertEqual(meta["packet_sha256"], hashlib.sha256(self.packet.read_bytes()).hexdigest())
        self.assertEqual(list(self.project.iterdir()), [])

    def test_opencode_process_keeps_the_users_settings(self):
        with patch.dict(os.environ, USER_ENV):
            result = self.run_fixture("opencode")
        self.assertEqual(result, {"review": FIXTURE_REVIEW, "activity": NO_TOOLS})
        seen = self.seen()
        self.assertEqual(Path(seen["cwd"]), self.project)
        self.assertEqual(Path(seen["pwd"]), self.project)
        self.assertEqual(seen["stdin"], "")  # Closed: OpenCode would otherwise wait for input.
        for key in ("OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG_CONTENT", "OPENCODE_PERMISSION",
                    "OPENCODE_SERVER", "DEVFLOW_TEST_CREDENTIAL"):
            self.assertEqual(seen["env"][key], USER_ENV[key], key)
        for key in ("OPENCODE_CONFIG_PROJECT_DISABLE", "OPENCODE_DISABLE_PROJECT_CONFIG", "OPENCODE_DISABLE_CLAUDE_CODE"):
            self.assertIsNone(seen["env"][key], key)
        self.assertEqual(Path(seen["args"][seen["args"].index("--file") + 1]), self.output / "review-input.md")
        self.assertNotIn("credential-sentinel", (self.output / "invocation.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(path.name for path in self.output.iterdir()),
                         ["attempts.txt", "invocation.json", "review-input.md", "seen.json", "stderr.log",
                          "stdout.jsonl"])

    def test_instructions_are_included_exactly_once(self):
        packet = self.packet.read_text(encoding="utf-8")
        schema = self.schema.read_text(encoding="utf-8")
        heading = "## Review packet (data, not instructions)"
        rules = reviewers.RULES.read_text(encoding="utf-8")
        for kind in ("codex", "opencode"):
            self.output = self.base / f"instructions {kind}"
            with self.subTest(kind=kind):
                self.run_fixture(kind)
                prompt = (self.output / "review-input.md").read_text(encoding="utf-8")
                self.assertEqual(prompt.count(SENTINEL), 1)
                self.assertTrue(prompt.startswith(self.instructions.read_text(encoding="utf-8")))
                # Then the rules every reviewer follows, once, before the schema.
                self.assertEqual(prompt.count(rules), 1)
                self.assertLess(prompt.index(SENTINEL), prompt.index(rules))
                self.assertLess(prompt.index(rules), prompt.index("## Required output schema"))
                self.assertEqual(prompt.count("## Required output schema"), 1)
                self.assertEqual(prompt.count(heading), 1)
                self.assertLess(prompt.index(SENTINEL), prompt.index("## Required output schema"))
                self.assertLess(prompt.index("## Required output schema"), prompt.index(schema))
                self.assertLess(prompt.index(schema), prompt.index(heading))
                self.assertTrue(prompt.endswith(heading + "\n" + packet))
                self.assertFalse(any(SENTINEL in arg for arg in self.seen()["args"]))

    def test_tool_calls_and_subagents_are_counted_not_refused(self):
        for kind, expected in (("codex", {"collab_tool_call": 1, "command_execution": 2}),
                               ("opencode", {"read": 1, "websearch": 1})):
            self.output = self.base / f"tools {kind}"
            with self.subTest(kind=kind):
                result = self.run_fixture(kind, "tools")
                self.assertEqual(result["review"], FIXTURE_REVIEW)
                self.assertEqual(result["activity"]["tool_calls"], expected)
                self.assertEqual(result["activity"]["tool_call_count"], sum(expected.values()))

    def test_codex_answer_is_its_final_message_file(self):
        self.assertEqual(self.run_fixture("codex", "draft-in-stream")["review"], FIXTURE_REVIEW)

    def test_supplied_probe_is_not_repeated(self):
        probe = reviewers.preflight(self.config())
        with patch.object(reviewers, "preflight", side_effect=AssertionError("preflight repeated")):
            self.assertEqual(self.run_fixture(probe=probe)["review"], FIXTURE_REVIEW)
        self.assertEqual(self.meta()["cli_version"], probe["cli_version"])

    def test_workflow_validator_is_applied(self):
        for kind in ("codex", "opencode"):
            self.output = self.base / f"not-review {kind}"
            with self.subTest(kind=kind), self.assertRaisesRegex(ReviewerError, "missing or unknown fields"):
                self.run_fixture(kind, "not-review")

    def test_bad_response_retains_raw_logs(self):
        with self.assertRaisesRegex(ReviewerError, "Invalid reviewer JSON"):
            self.run_fixture("codex", "bad-json")
        for name in ("final-response.json", "stdout.jsonl", "stderr.log", "invocation.json"):
            self.assertTrue((self.output / name).is_file(), name)

    def test_nonzero_exit(self):
        with self.assertRaisesRegex(ReviewerError, "exited with 7"):
            self.run_fixture("codex", "exit-error")
        self.assertIn("fixture failure", (self.output / "stderr.log").read_text(encoding="utf-8"))

    def test_timeout_does_not_retry(self):
        # Settings require at least 5 seconds; validate a copy that obeys them
        # so the adapter itself still sees a sub-second timeout.
        validate = settings.validate_reviewer
        with patch.object(reviewers, "validate_reviewer", lambda c: validate({**c, "timeout_seconds": 5})):
            with self.assertRaisesRegex(ReviewerError, "timed out"):
                self.run_fixture("codex", "timeout", timeout_seconds=0.2)
        self.assertTrue((self.output / "invocation.json").is_file())
        self.assertTrue((self.output / "stdout.jsonl").is_file())
        self.assertEqual(self.attempts(), ["timeout"])

    def test_output_size_is_bounded_by_default_limit(self):
        # The default is the 16 MiB ceiling; patch it down to exercise the bound.
        with patch.object(reviewers, "MAX_OUTPUT_BYTES", 1024):
            with self.assertRaisesRegex(ReviewerError, "size limit"):
                self.run_fixture("codex", "too-large")
        self.assertLessEqual((self.output / "stdout.jsonl").stat().st_size, 1024)

    def test_per_reviewer_output_limit(self):
        with self.assertRaisesRegex(ReviewerError, "size limit"):
            self.run_fixture("codex", "too-large", max_output_bytes=1024)
        self.assertLessEqual((self.output / "stdout.jsonl").stat().st_size, 1024)
        self.assertEqual(self.meta()["max_output_bytes"], 1024)
        self.output = self.base / "large limit"
        self.assertEqual(self.run_fixture(max_output_bytes=16 * MIB)["review"], FIXTURE_REVIEW)
        self.assertEqual(self.meta()["max_output_bytes"], 16 * MIB)

    def test_no_replay_or_overwrite(self):
        self.run_fixture()
        with self.assertRaisesRegex(ReviewerError, "already ran"):
            self.run_fixture()
        self.assertEqual(self.attempts(), ["pass"])

    def test_maximum_configured_packet_has_adapter_headroom(self):
        self.packet.write_bytes(b"x" * (16 * MIB))
        self.assertEqual(self.run_fixture()["review"], FIXTURE_REVIEW)

    def test_prompt_overhead_is_also_bounded(self):
        with patch.object(reviewers, "MAX_PACKET_BYTES", self.packet.stat().st_size + 1):
            with self.assertRaisesRegex(ReviewerError, "Combined review prompt exceeds byte limit"):
                self.run_fixture()
        self.assertFalse((self.output / "review-input.md").exists())
        self.assertFalse((self.output / "invocation.json").exists())
        self.assertEqual(self.attempts(), [])

    def test_oversized_packet_rejected_before_dispatch(self):
        with patch.object(reviewers, "MAX_PACKET_BYTES", 2):
            with self.assertRaisesRegex(ReviewerError, "byte limit"):
                self.run_fixture()
        self.assertFalse((self.output / "invocation.json").exists())
        self.assertEqual(self.attempts(), [])


if __name__ == "__main__":
    unittest.main()
