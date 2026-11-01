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
AGENT_ID = r"^devflow-reviewer-[0-9a-f]{16}$"
SENTINEL = "REVIEWER-INSTRUCTIONS-SENTINEL-7f3a"
REVIEW = {"schema_version": 1, "verdict": "pass", "summary": "問題なし", "findings": [], "questions": []}
FIXTURE_REVIEW = {**REVIEW, "summary": "fixture pass"}
HOSTILE_ENV = {"OPENCODE_CONFIG": "/unsafe/config.json", "OPENCODE_CONFIG_CONTENT": '"unsafe"',
               "OPENCODE_PERMISSION": '"allow"', "OPENCODE_SERVER": "http://127.0.0.1:4096",
               "DEVFLOW_TEST_CREDENTIAL": "credential-sentinel"}


def reviewer(kind="codex", **changes):
    """A complete, valid reviewer entry (protocol_version omitted unless given)."""
    return {"id": kind, "kind": kind, "command": [kind],
            "model": "gpt-6.1-sol" if kind == "codex" else "opencode-go/deepseek-v4.1-flash",
            "effort": "xhigh" if kind == "codex" else "high", "timeout_seconds": 10, **changes}


def accept(value):
    """Stand-in for a workflow's validate_review: exactly the review keys."""
    if not isinstance(value, dict) or set(value) != set(REVIEW):
        raise ReviewerError("Review has missing or unknown fields")
    return value


def codex_events(value=REVIEW):
    return "\n".join(json.dumps(item) for item in [
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "a1", "type": "agent_message", "text": json.dumps(value)}},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ])


def opencode_events(value=REVIEW):
    return "\n".join(json.dumps(item) for item in [
        {"type": "step_start", "sessionID": "session-1", "part": {"messageID": "message-1"}},
        {"type": "text", "sessionID": "session-1", "part": {
            "messageID": "message-1", "id": "text-1", "text": json.dumps(value)}},
        {"type": "step_finish", "sessionID": "session-1", "part": {
            "messageID": "message-1", "reason": "stop"}},
    ])


FAKE = r'''
import json
import os
from pathlib import Path
import sys
import time

mode = sys.argv[1]
args = sys.argv[2:]
if "--version" in args:
    print("2.1.0" if mode == "v2-old" else "1.20.0" if mode == "v1" else "2.2.3")
    raise SystemExit(0)
if "--help" in args:
    flags = ["--model", "--file", "--agent", "--format", "--variant", "--standalone", "--json",
             "--ephemeral", "--output-schema", "--strict-config", "--pure",
             "--ignore-user-config", "--ignore-rules"]
    missing = {"old-codex": ["--ignore-user-config", "--ignore-rules"],
               "old-opencode": ["--standalone"]}.get(mode, [])
    print(" ".join(flag for flag in flags if flag not in missing))
    raise SystemExit(0)
# Every dispatched review attempt leaves one line here (cwd is the workspace).
with open("attempts.txt", "a", encoding="utf-8") as handle:
    handle.write(mode + "\n")
if mode == "timeout":
    time.sleep(30)
if mode == "too-large":
    print("x" * 65536)
    raise SystemExit(0)
if mode == "exit-error":
    print("fixture failure", file=sys.stderr)
    raise SystemExit(7)
Path("seen.json").write_text(json.dumps({"args": args, "cwd": os.getcwd(), "env": {
    key: os.environ.get(key) for key in (
        "OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG", "OPENCODE_CONFIG_CONTENT", "OPENCODE_PERMISSION",
        "OPENCODE_SERVER", "DEVFLOW_TEST_CREDENTIAL")}}), encoding="utf-8")
value = {"schema_version": 1, "verdict": "pass", "summary": "fixture pass", "findings": [], "questions": []}
text = ("not json" if mode == "bad-json" else
        json.dumps({"unexpected": True}) if mode == "not-review" else json.dumps(value))
if "--output-last-message" in args:
    Path("stdin.txt").write_text(sys.stdin.read(), encoding="utf-8")
    final_text = json.dumps({**value, "summary": "DIFFERENT"}) if mode == "disagree" else text
    Path(args[args.index("--output-last-message") + 1]).write_text(final_text, encoding="utf-8")
    print(json.dumps({"type": "thread.started", "thread_id": "thread-1"}))
    print(json.dumps({"type": "turn.started"}))
    if mode == "tool":
        print(json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": "echo unsafe"}}))
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}))
    print(json.dumps({"type": "turn.completed", "usage": {}}))
else:
    if "--standalone" not in args:
        # v1 yargs array options consume adjacent positional values. Model that
        # behavior so a prompt after --file cannot accidentally pass this fake.
        attached = []
        for arg in args[args.index("--file") + 1:]:
            if arg.startswith("-"):
                break
            attached.append(arg)
        if len(attached) != 1 or not Path(attached[0]).is_file():
            raise SystemExit(11)
    print(json.dumps({"type": "step_start", "sessionID": "s1", "part": {"messageID": "m1"}}))
    if mode == "tool":
        print(json.dumps({"type": "tool_use", "sessionID": "s1", "part": {"messageID": "m1"}}))
    print(json.dumps({"type": "text", "sessionID": "s1", "part": {"messageID": "m1", "id": "p1", "text": text}}))
    print(json.dumps({"type": "step_finish", "sessionID": "s1", "part": {"messageID": "m1", "reason": "stop"}}))
'''


class JsonAndEventTests(unittest.TestCase):
    def test_json_duplicates_constants_and_fences_rejected(self):
        for raw in ('{"a":1,"a":2}', '{"x":NaN}', '{"x":-Infinity}', '```json\n{}\n```', ""):
            with self.subTest(raw=raw), self.assertRaises(ReviewerError):
                reviewers.json_value(raw)

    def test_text_bounds(self):
        self.assertEqual(reviewers.text("ok", "label"), "ok")
        for value, limit in (("", 10), ("   ", 10), (None, 10), ("a" * 11, 10), ("a\0b", 10)):
            with self.subTest(value=value), self.assertRaises(ReviewerError):
                reviewers.text(value, "label", limit)

    def test_parsers_return_raw_json_without_review_validation(self):
        self.assertEqual(reviewers.parse_codex_events(codex_events()), REVIEW)
        self.assertEqual(reviewers.parse_opencode_events(opencode_events()), REVIEW)
        for parse, events in ((reviewers.parse_codex_events, codex_events),
                              (reviewers.parse_opencode_events, opencode_events)):
            with self.subTest(parse=parse.__name__):
                self.assertEqual(parse(events({"anything": 1})), {"anything": 1})

    def test_codex_tool_event_invalidates_otherwise_passing_review(self):
        lines = codex_events().splitlines()
        for kind in ("mcp_tool_call", "command_execution", "web_search", "file_change"):
            for event in ("item.started", "item.completed"):
                stream = "\n".join(lines[:2] + [json.dumps({"type": event, "item": {"type": kind}})] + lines[2:])
                with self.subTest(kind=kind, event=event), \
                        self.assertRaisesRegex(ReviewerError, "attempted a tool"):
                    reviewers.parse_codex_events(stream)

    def test_codex_incomplete_multiple_or_post_completion_events_rejected(self):
        lines = codex_events().splitlines()
        second_thread = json.dumps({"type": "thread.started", "thread_id": "thread-2"})
        for label, stream in (
                ("no normal finish", "\n".join(lines[:-1])),
                ("multiple answers", "\n".join(lines[:3] + [lines[2]] + lines[3:])),
                ("multiple turns", "\n".join(lines[:2] + [lines[1]] + lines[2:])),
                ("multiple threads", "\n".join([lines[0], second_thread] + lines[1:])),
                ("item before turn", "\n".join([lines[0], lines[2], lines[1], lines[3]])),
                ("error after completion", codex_events() + '\n{"type":"error"}'),
                ("event is not an object", "[]")):
            with self.subTest(label), self.assertRaises(ReviewerError):
                reviewers.parse_codex_events(stream)

    def test_opencode_tool_error_unknown_and_post_completion_rejected(self):
        events = opencode_events()
        late_text = events.splitlines()[1]
        for label, stream in (
                ("tool before", json.dumps({"type": "tool_use", "part": {}}) + "\n" + events),
                ("error before", json.dumps({"type": "error"}) + "\n" + events),
                ("unknown before", json.dumps({"type": "unknown_event", "part": {}}) + "\n" + events),
                ("error after", events + '\n{"type":"error","error":{"message":"failed"}}'),
                ("text after", events + "\n" + late_text.replace("text-1", "text-2"))):
            with self.subTest(label), self.assertRaises(ReviewerError):
                reviewers.parse_opencode_events(stream)

    def test_opencode_missing_finish_or_truncation(self):
        events = opencode_events()
        for stream in ("\n".join(events.splitlines()[:-1]), events.replace('"stop"', '"length"'),
                       events.replace('"stop"', '"tool-calls"')):
            with self.subTest(stream=stream), self.assertRaises(ReviewerError):
                reviewers.parse_opencode_events(stream)

    def test_opencode_duplicate_part_deduplicated_but_conflict_rejected(self):
        lines = opencode_events().splitlines()
        lines.insert(2, lines[1])
        self.assertEqual(reviewers.parse_opencode_events("\n".join(lines)), REVIEW)
        conflicting = json.loads(lines[2])
        conflicting["part"]["text"] = json.dumps({**REVIEW, "summary": "changed"})
        lines[2] = json.dumps(conflicting)
        with self.assertRaisesRegex(ReviewerError, "Conflicting"):
            reviewers.parse_opencode_events("\n".join(lines))

    def test_opencode_multiple_messages_or_sessions_rejected(self):
        for old, new in (('"messageID": "message-1", "reason"', '"messageID": "message-2", "reason"'),
                         ("session-1", "session-2")):
            lines = opencode_events().splitlines()
            lines[-1] = lines[-1].replace(old, new)
            with self.subTest(new=new), self.assertRaises(ReviewerError):
                reviewers.parse_opencode_events("\n".join(lines))

    def test_opencode_duplicate_event_keys_rejected(self):
        with self.assertRaises(ReviewerError):
            reviewers.parse_opencode_events('{"type":"text","type":"step_start","part":{}}')

    def test_markdown_fences_rejected_not_salvaged(self):
        lines = [json.loads(line) for line in opencode_events().splitlines()]
        lines[1]["part"]["text"] = "```json\n" + json.dumps(REVIEW) + "\n```"
        with self.assertRaises(ReviewerError):
            reviewers.parse_opencode_events("\n".join(json.dumps(e) for e in lines))


class CommandTests(unittest.TestCase):
    def test_codex_uses_isolated_fresh_read_only_flags(self):
        argv, env = reviewers.build_command(reviewer(), Path("round 1"), Path("schema.json"))
        self.assertEqual(argv[:7], ["codex", "--strict-config", "exec", "--ignore-user-config",
                                    "--ignore-rules", "--model", "gpt-6.1-sol"])
        for flag in ("--ephemeral", "--json", "--skip-git-repo-check"):
            self.assertIn(flag, argv)
        for setting in ('model_reasoning_effort="xhigh"', 'approval_policy="never"', 'web_search="disabled"',
                        "project_doc_max_bytes=0", "mcp_servers={}", "features.code_mode.enabled=false",
                        "features.shell_tool=false", "features.apps=false", "features.hooks=false",
                        "features.plugins=false"):
            self.assertIn(setting, argv)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertEqual(argv[argv.index("--output-schema") + 1], str(Path("schema.json").resolve()))
        self.assertEqual(argv[argv.index("--output-last-message") + 1],
                         str(Path("round 1").resolve() / "final-response.json"))
        self.assertEqual(argv[-1], "-")
        self.assertEqual(env, {})
        for absent in ("--resume", "--ask-for-approval", "--yolo"):
            self.assertNotIn(absent, argv)

    def test_opencode_v2_is_default_and_uses_variant_selector(self):
        work = Path("round 1")
        omitted = reviewers.build_command(reviewer("opencode"), work, Path("schema.json"))
        explicit = reviewers.build_command(reviewer("opencode", protocol_version=2), work, Path("schema.json"))
        self.assertEqual(omitted, explicit)
        argv, env = omitted
        self.assertEqual(argv[:3], ["opencode", "run", "--standalone"])
        self.assertEqual(argv[argv.index("--model") + 1], "opencode-go/deepseek-v4.1-flash#high")
        self.assertEqual(argv[argv.index("--format") + 1], "json")
        self.assertEqual(Path(argv[argv.index("--file") + 1]), work.resolve() / "workspace" / "review-input.md")
        self.assertRegex(argv[argv.index("--agent") + 1], AGENT_ID)
        self.assertIn("DevFlow", argv[-1])
        for absent in ("--variant", "--pure", "--auto", "--continue"):
            self.assertNotIn(absent, argv)
        self.assertEqual(env, {"OPENCODE_CONFIG_DIR": str(work.resolve() / "workspace"),
                               "OPENCODE_DISABLE_PROJECT_CONFIG": "1", "OPENCODE_CONFIG_PROJECT_DISABLE": "1",
                               "OPENCODE_DISABLE_CLAUDE_CODE": "1"})

    def test_opencode_v1_must_be_explicit_and_denies_all(self):
        work = Path("round 1")
        argv, env = reviewers.build_command(reviewer("opencode", protocol_version=1), work, Path("schema.json"))
        self.assertEqual(argv[:3], ["opencode", "--pure", "run"])
        self.assertEqual(argv[argv.index("--model") + 1], "opencode-go/deepseek-v4.1-flash")
        self.assertEqual(argv[argv.index("--variant") + 1], "high")
        self.assertIn("DevFlow", argv[argv.index("--title") + 1])
        # v1 treats --file as a greedy array; the attachment must come last.
        self.assertEqual(argv[-2], "--file")
        self.assertEqual(Path(argv[-1]), work.resolve() / "workspace" / "review-input.md")
        for absent in ("--standalone", "--auto", "--continue"):
            self.assertNotIn(absent, argv)
        agent = argv[argv.index("--agent") + 1]
        self.assertRegex(agent, AGENT_ID)
        runtime = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(runtime, reviewers.runtime_config("opencode-go/deepseek-v4.1-flash", 1, agent))
        self.assertEqual(runtime["permission"], "deny")
        self.assertEqual(runtime["agent"][agent]["permission"], "deny")
        self.assertEqual(runtime["agent"][agent]["mode"], "primary")
        self.assertEqual(env["OPENCODE_CONFIG_DIR"], str(work.resolve() / "workspace"))
        self.assertEqual(json.loads(env["OPENCODE_PERMISSION"]), "deny")
        for key in ("OPENCODE_DISABLE_PROJECT_CONFIG", "OPENCODE_DISABLE_CLAUDE_CODE", "OPENCODE_PURE",
                    "OPENCODE_DISABLE_EXTERNAL_SKILLS", "OPENCODE_DISABLE_AUTOUPDATE"):
            self.assertEqual(env[key], "true")

    def test_agent_id_is_per_round_for_both_protocols(self):
        for protocol in (1, 2):
            config = reviewer("opencode", protocol_version=protocol)

            def agent(work):
                argv, _ = reviewers.build_command(config, work, Path("schema.json"))
                return argv[argv.index("--agent") + 1]
            with self.subTest(protocol=protocol):
                self.assertRegex(agent(Path("round1")), AGENT_ID)
                self.assertEqual(agent(Path("round1")), agent(Path("round1").resolve()))
                self.assertNotEqual(agent(Path("round1")), agent(Path("round2")))

    def test_v2_runtime_denies_tools_mcp_skills_and_plugins(self):
        config = reviewers.runtime_config("opencode-go/deepseek-v4.1-flash", 2, "my-reviewer")
        self.assertEqual(config["permissions"], [{"action": "*", "resource": "*", "effect": "deny"}])
        self.assertEqual(config["agents"]["my-reviewer"]["permissions"], config["permissions"])
        self.assertEqual(config["agents"]["my-reviewer"]["mode"], "primary")
        self.assertIn("DevFlow", config["agents"]["my-reviewer"]["system"])
        self.assertEqual({p["action"] for p in config["policies"]}, {"tool.use", "mcp.use", "skill.use", "plugin.use"})
        self.assertEqual({p["effect"] for p in config["policies"]}, {"deny"})
        self.assertTrue(config["agents"]["title"]["disabled"])
        self.assertEqual(config["update"], "disable")
        self.assertFalse(config["snapshots"])
        self.assertFalse(config["compaction"]["auto"])
        self.assertFalse(config["warming"])

    def test_v1_runtime_denies_everything(self):
        config = reviewers.runtime_config("opencode-go/deepseek-v4.1-flash", 1, "my-reviewer")
        self.assertEqual(config["permission"], "deny")
        self.assertEqual(config["agent"]["my-reviewer"]["permission"], "deny")
        self.assertEqual(config["agent"]["my-reviewer"]["model"], "opencode-go/deepseek-v4.1-flash")
        self.assertIn("DevFlow", config["agent"]["my-reviewer"]["prompt"])
        self.assertEqual((config["plugin"], config["mcp"]), ([], {}))
        self.assertFalse(config["autoupdate"])
        self.assertEqual(config["share"], "disabled")

    def test_invalid_entries_rejected_as_reviewer_errors(self):
        for change in ({"command": "codex; touch BAD"}, {"command": ["env", "bash", "-c", "codex"]},
                       {"command": ["codex", "--yolo"]},
                       {"model": "gpt-6.1-sol --dangerously-bypass-approvals-and-sandbox"},
                       {"model": "gpt-6.1-sol --yolo"}, {"protocol_version": 99}, {"max_output_bytes": 1023},
                       {"kind": "opencode", "protocol_version": 99}, {"timeout_seconds": 0.2}):
            with self.subTest(change=change), self.assertRaises(ReviewerError):
                reviewers.build_command({**reviewer(), **change}, Path("round"), Path("schema.json"))
        incomplete = reviewer()
        del incomplete["id"]
        with self.assertRaises(ReviewerError):
            reviewers.build_command(incomplete, Path("round"), Path("schema.json"))


class ReviewerSettingsTests(unittest.TestCase):
    """The shared reviewer rules (common.settings), applied to every workflow."""

    def assertRejected(self, entry, message=None):
        with self.assertRaisesRegex(DevFlowError, message or ""):
            settings.validate_reviewer(entry)

    def reviewers(self, count, kinds=("codex", "opencode")):
        return [reviewer(kinds[index % len(kinds)], id=f"r{index}") for index in range(count)]

    def test_complete_entries_accepted(self):
        for entry in (reviewer(), reviewer("opencode"),
                      reviewer(command=["/opt/bash/codex"]), reviewer(command=["node", "C:\\tools\\codex.js"]),
                      reviewer(command=[sys.executable, "/tmp/fake reviewer.py", "pass"])):
            with self.subTest(entry=entry):
                self.assertIs(settings.validate_reviewer(entry), entry)

    def test_required_and_unknown_fields(self):
        for field in sorted(settings.REVIEWER_REQUIRED):
            entry = reviewer()
            del entry[field]
            with self.subTest(missing=field):
                self.assertRejected(entry, "fields")
        self.assertRejected({**reviewer(), "api_key": "secret"}, "fields")
        self.assertRejected(["codex"], "fields")

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

    def test_shell_anywhere_in_argv_rejected(self):
        for command in (["bash", "-c", "codex"], ["env", "bash", "-c", "x"], ["xargs", "sh"],
                        ["C:\\x\\bash.exe"], ["/bin/sh", "-c", "codex"], ["codex", "--wrapper", "/usr/bin/zsh"],
                        ["PowerShell.EXE", "-Command", "codex"], ["pwsh"], ["cmd.exe", "/c", "codex"],
                        ["C:\\Windows\\System32\\CMD.BAT"], ["fish"], ["dash"]):
            with self.subTest(command=command):
                self.assertRejected(reviewer(command=command), "never through a shell")
                self.assertRejected(reviewer("opencode", command=command), "never through a shell")

    def test_safety_bypass_flags_rejected(self):
        for command in (["codex", "--yolo"], ["codex", "--dangerously-bypass-approvals-and-sandbox"],
                        ["opencode", "--dangerously-skip-permissions"]):
            with self.subTest(command=command):
                self.assertRejected(reviewer(command=command), "safety controls")

    def test_model_and_effort_cannot_inject_flags(self):
        for change in ({"model": "gpt-6.1-sol --dangerously-bypass-approvals-and-sandbox"},
                       {"model": "-m"}, {"effort": "high;rm"}, {"effort": ""}, {"model": None}):
            with self.subTest(change=change):
                self.assertRejected({**reviewer(), **change}, "Invalid reviewer")

    def test_timeout_bounds(self):
        for timeout in (5, 3600):
            settings.validate_reviewer(reviewer(timeout_seconds=timeout))
        for timeout in (4, 3601, 5.0, True, "10"):
            with self.subTest(timeout=timeout):
                self.assertRejected(reviewer(timeout_seconds=timeout), "timeout_seconds")

    def test_protocol_version_one_two_or_omitted(self):
        for changes in ({"protocol_version": 1}, {"protocol_version": 2}, {}):
            with self.subTest(changes=changes):
                settings.validate_reviewer(reviewer("opencode", **changes))
        for protocol in (0, 3, 99, True, 2.0, "2", None):
            with self.subTest(protocol=protocol):
                self.assertRejected(reviewer("opencode", protocol_version=protocol), "protocol_version")
        self.assertRejected(reviewer(protocol_version=2), "OpenCode setting")

    def test_max_output_bytes_boundaries(self):
        self.assertEqual(settings.DEFAULT_OUTPUT_BYTES, 4 * MIB)
        self.assertEqual(reviewers.MAX_OUTPUT_BYTES, 4 * MIB)
        self.assertEqual(settings.MAX_OUTPUT_BYTES, 16 * MIB)
        for limit in (1024, 16 * MIB):
            with self.subTest(limit=limit):
                settings.validate_reviewer(reviewer(max_output_bytes=limit))
                reviewers.build_command(reviewer(max_output_bytes=limit), Path("round"), Path("schema.json"))
        for limit in (1023, 16 * MIB + 1, 0, True, 1024.0, "4096"):
            with self.subTest(limit=limit):
                self.assertRejected(reviewer(max_output_bytes=limit), "max_output_bytes")
                with self.assertRaises(ReviewerError):
                    reviewers.build_command(reviewer(max_output_bytes=limit), Path("round"), Path("schema.json"))

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
        entries[2]["command"] = ["xargs", "sh"]
        with self.assertRaisesRegex(DevFlowError, "shell"):
            settings.validate_reviewers(entries, 6)


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="devflow reviewers ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.packet = self.base / "packet.md"
        self.packet.write_text("Original request: 日本語\n$(touch BAD)\nDo not execute this text.\n", encoding="utf-8")
        self.fake = self.base / "fake reviewer.py"
        self.fake.write_text(FAKE, encoding="utf-8")
        self.schema = self.base / "review schema.json"
        self.schema.write_text(json.dumps({"type": "object"}), encoding="utf-8")
        self.instructions = self.base / "reviewer instructions.md"
        self.instructions.write_text("# Reviewer instructions\n" + SENTINEL + "\n", encoding="utf-8")
        self.output = self.base / "round 1"

    @property
    def workspace(self):
        return self.output / "workspace"

    def config(self, kind="codex", mode="pass", **changes):
        return reviewer(kind, command=[sys.executable, str(self.fake), mode], **changes)

    def run_fixture(self, kind="codex", mode="pass", validate=accept, probe=None, **changes):
        return reviewers.run_review(self.config(kind, mode, **changes), self.packet, self.output,
                                    self.schema, self.instructions, validate, probe)

    def seen(self):
        return json.loads((self.workspace / "seen.json").read_text(encoding="utf-8"))

    def meta(self):
        return json.loads((self.output / "invocation.json").read_text(encoding="utf-8"))

    def attempts(self):
        path = self.workspace / "attempts.txt"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def test_preflight_is_local_and_creates_nothing(self):
        result = reviewers.preflight(self.config())
        self.assertEqual(result["kind"], "codex")
        self.assertEqual(result["requested_model"], "gpt-6.1-sol")
        self.assertEqual(result["requested_effort"], "xhigh")
        self.assertIsNone(result["protocol_version"])
        self.assertFalse(result["effective_model_attested"])
        self.assertFalse(self.output.exists())
        self.assertFalse((self.base / "attempts.txt").exists())
        self.assertFalse((Path.cwd() / "attempts.txt").exists())

    def test_preflight_reports_protocol_one_two_or_omitted(self):
        for changes, mode, expected in (({"protocol_version": 1}, "v1", 1),
                                        ({"protocol_version": 2}, "pass", 2), ({}, "pass", 2)):
            with self.subTest(changes=changes):
                result = reviewers.preflight(self.config("opencode", mode, **changes))
                self.assertEqual(result["protocol_version"], expected)
                self.assertEqual(result["cli_version"], "1.20.0" if expected == 1 else "2.2.3")

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
        for config in (incomplete, {**self.config(), "command": ["env", "bash", "-c", "codex"]}):
            with self.subTest(config=config), self.assertRaises(ReviewerError):
                reviewers.run_review(config, self.packet, self.output, self.schema, self.instructions, accept)
            self.assertFalse(self.output.exists())

    def test_codex_process(self):
        result = self.run_fixture()
        self.assertEqual(result, FIXTURE_REVIEW)
        self.assertEqual(self.attempts(), ["pass"])
        self.assertTrue((self.output / "stdout.jsonl").is_file())
        self.assertTrue((self.output / "stderr.log").is_file())
        meta = self.meta()
        self.assertFalse(meta["effective_model_attested"])
        self.assertEqual(meta["requested_model"], "gpt-6.1-sol")
        self.assertEqual(meta["requested_effort"], "xhigh")
        self.assertEqual(meta["cli_version"], "2.2.3")
        self.assertEqual(Path(meta["cwd"]), self.workspace.resolve())
        self.assertFalse(meta["shell"])
        self.assertEqual(meta["prefix_arg_count"], 2)
        self.assertEqual(meta["timeout_seconds"], 10)
        self.assertEqual(meta["max_output_bytes"], 4 * MIB)
        self.assertEqual(meta["packet_sha256"], hashlib.sha256(self.packet.read_bytes()).hexdigest())
        prompt = (self.workspace / "stdin.txt").read_text(encoding="utf-8")
        self.assertEqual(prompt, (self.workspace / "review-input.md").read_text(encoding="utf-8"))
        self.assertIn("日本語", prompt)
        self.assertIn("$(touch BAD)", prompt)
        self.assertEqual(list(self.base.rglob("BAD")), [])

    def test_instructions_are_included_exactly_once(self):
        packet = self.packet.read_text(encoding="utf-8")
        schema = self.schema.read_text(encoding="utf-8")
        for kind, mode, changes in (("codex", "pass", {}), ("opencode", "pass", {}),
                                    ("opencode", "v1", {"protocol_version": 1})):
            self.output = self.base / f"{kind} {mode}"
            with self.subTest(kind=kind, mode=mode):
                self.run_fixture(kind, mode, **changes)
                prompt = (self.workspace / "review-input.md").read_text(encoding="utf-8")
                self.assertEqual(prompt.count(SENTINEL), 1)
                self.assertTrue(prompt.startswith(self.instructions.read_text(encoding="utf-8")))
                self.assertEqual(prompt.count("## Required output schema"), 1)
                self.assertEqual(prompt.count("## Untrusted review packet"), 1)
                self.assertLess(prompt.index(SENTINEL), prompt.index("## Required output schema"))
                self.assertLess(prompt.index("## Required output schema"), prompt.index(schema))
                self.assertLess(prompt.index(schema), prompt.index("## Untrusted review packet"))
                self.assertTrue(prompt.endswith("## Untrusted review packet\n" + packet))
                if kind == "codex":
                    self.assertEqual((self.workspace / "stdin.txt").read_text(encoding="utf-8"), prompt)
                else:
                    args = self.seen()["args"]
                    self.assertEqual(Path(args[args.index("--file") + 1]),
                                     (self.workspace / "review-input.md").resolve())
                    self.assertFalse(any(SENTINEL in arg for arg in args))

    def test_opencode_v2_process_and_isolation(self):
        with patch.dict(os.environ, HOSTILE_ENV):
            result = self.run_fixture("opencode")
        self.assertEqual(result, FIXTURE_REVIEW)
        seen = self.seen()
        self.assertIn("--standalone", seen["args"])
        self.assertEqual(Path(seen["cwd"]), self.workspace.resolve())
        env = seen["env"]
        for key in ("OPENCODE_CONFIG", "OPENCODE_CONFIG_CONTENT", "OPENCODE_PERMISSION", "OPENCODE_SERVER"):
            self.assertIsNone(env[key], key)
        self.assertEqual(env["DEVFLOW_TEST_CREDENTIAL"], "credential-sentinel")
        config_file = self.workspace / "opencode.json"
        self.assertEqual(Path(env["OPENCODE_CONFIG_DIR"]), config_file.parent.resolve())
        runtime = json.loads(config_file.read_text(encoding="utf-8"))
        self.assertEqual(runtime["permissions"], [{"action": "*", "resource": "*", "effect": "deny"}])
        agent = seen["args"][seen["args"].index("--agent") + 1]
        self.assertRegex(agent, AGENT_ID)
        self.assertEqual(runtime["agents"][agent]["permissions"], runtime["permissions"])
        self.assertTrue(any(p["action"] == "mcp.use" and p["effect"] == "deny" for p in runtime["policies"]))
        self.assertEqual(runtime["update"], "disable")
        self.assertTrue(runtime["agents"]["title"]["disabled"])
        self.assertFalse(runtime["compaction"]["auto"])
        self.assertFalse(runtime["warming"])
        self.assertEqual(self.meta()["protocol_version"], 2)
        self.assertNotIn("credential-sentinel", (self.output / "invocation.json").read_text(encoding="utf-8"))

    def test_opencode_v1_process_and_isolation(self):
        with patch.dict(os.environ, HOSTILE_ENV):
            result = self.run_fixture("opencode", "v1", protocol_version=1)
        self.assertEqual(result, FIXTURE_REVIEW)
        seen = self.seen()
        self.assertIn("--pure", seen["args"])
        env = seen["env"]
        self.assertIsNone(env["OPENCODE_CONFIG"])
        self.assertIsNone(env["OPENCODE_SERVER"])
        self.assertEqual(json.loads(env["OPENCODE_PERMISSION"]), "deny")
        self.assertEqual(env["DEVFLOW_TEST_CREDENTIAL"], "credential-sentinel")
        self.assertEqual(Path(env["OPENCODE_CONFIG_DIR"]), self.workspace.resolve())
        agent = seen["args"][seen["args"].index("--agent") + 1]
        self.assertRegex(agent, AGENT_ID)
        runtime = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(runtime["permission"], "deny")
        self.assertEqual(runtime["agent"][agent]["permission"], "deny")
        self.assertEqual(json.loads((self.workspace / "opencode.json").read_text(encoding="utf-8")), runtime)
        self.assertEqual(self.meta()["protocol_version"], 1)

    def test_wrong_protocol_or_old_cli_refuses_dispatch(self):
        for changes, mode, message in (({"protocol_version": 1}, "pass", "configured v1"),
                                       ({}, "v1", "configured v2"),
                                       ({"protocol_version": 2}, "v1", "configured v2"),
                                       ({}, "v2-old", r"at least 2\.2\.3")):
            self.output = self.base / f"{mode} {changes}"
            with self.subTest(changes=changes, mode=mode):
                with self.assertRaisesRegex(ReviewerError, message):
                    self.run_fixture("opencode", mode, **changes)
                self.assertEqual(self.attempts(), [])
                self.assertFalse((self.output / "invocation.json").exists())

    def test_old_cli_refuses_weaker_fallback(self):
        for kind, mode in (("codex", "old-codex"), ("opencode", "old-opencode")):
            self.output = self.base / mode
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(ReviewerError, "lacks required review flags"):
                    self.run_fixture(kind, mode)
                self.assertEqual(self.attempts(), [])

    def test_supplied_probe_is_not_repeated(self):
        probe = reviewers.preflight(self.config())
        with patch.object(reviewers, "preflight", side_effect=AssertionError("preflight repeated")):
            self.assertEqual(self.run_fixture(probe=probe), FIXTURE_REVIEW)
        self.assertEqual(self.meta()["cli_version"], probe["cli_version"])

    def test_tool_attempt_rejected(self):
        for kind in ("codex", "opencode"):
            self.output = self.base / f"tool {kind}"
            with self.subTest(kind=kind), self.assertRaisesRegex(ReviewerError, "tool"):
                self.run_fixture(kind, "tool")

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

    def test_codex_final_file_and_events_must_agree(self):
        with self.assertRaisesRegex(ReviewerError, "disagree"):
            self.run_fixture("codex", "disagree")

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
        self.assertEqual(self.run_fixture(max_output_bytes=16 * MIB), FIXTURE_REVIEW)
        self.assertEqual(self.meta()["max_output_bytes"], 16 * MIB)

    def test_no_replay_or_overwrite(self):
        self.run_fixture()
        with self.assertRaisesRegex(ReviewerError, "already exists"):
            self.run_fixture()
        self.assertEqual(self.attempts(), ["pass"])

    def test_maximum_configured_packet_has_adapter_headroom(self):
        self.packet.write_bytes(b"x" * (16 * MIB))
        self.assertEqual(self.run_fixture(), FIXTURE_REVIEW)

    def test_prompt_overhead_is_also_bounded(self):
        with patch.object(reviewers, "MAX_PACKET_BYTES", self.packet.stat().st_size + 1):
            with self.assertRaisesRegex(ReviewerError, "Combined review prompt exceeds byte limit"):
                self.run_fixture()
        self.assertFalse((self.workspace / "review-input.md").exists())
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
