"""Offline adapter tests. Fake local processes only, with no model calls."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "devflow" / "components" / "buildflow"
sys.path.insert(0, str(PLUGIN / "scripts"))
from buildflow_core import adapters  # noqa: E402
from buildflow_core.storage import BuildFlowError  # noqa: E402

PASS = {"schema_version": 1, "snapshot_id": "snapshot-abc123", "verdict": "pass",
        "summary": "問題なし", "findings": [], "questions": []}
FINDING = {"id": "F1", "severity": "medium", "category": "correctness", "file": "src/main.py",
           "line_start": 2, "line_end": 4, "evidence": "No empty-input handling",
           "recommendation": "Handle empty input", "requires_human": False}


def codex_events(value=PASS):
    return "\n".join(json.dumps(item) for item in [
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "a1", "type": "agent_message", "text": json.dumps(value)}},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ])


def opencode_events(value=PASS):
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
    print("--model --file --agent --format --variant --standalone --json --ephemeral --output-schema --strict-config --pure" +
          ("" if mode == "old-codex" else " --ignore-user-config --ignore-rules"))
    raise SystemExit(0)
if mode == "timeout":
    time.sleep(10)
if mode == "too-large":
    print("x" * 65536)
    raise SystemExit(0)
if mode == "exit-error":
    print("fixture failure", file=sys.stderr)
    raise SystemExit(7)
Path("seen.json").write_text(json.dumps({"args": args, "cwd": os.getcwd(),
    "config_dir": os.environ.get("OPENCODE_CONFIG_DIR"),
    "config": os.environ.get("OPENCODE_CONFIG"),
    "content": os.environ.get("OPENCODE_CONFIG_CONTENT"),
    "permission": os.environ.get("OPENCODE_PERMISSION")}), encoding="utf-8")
value = {"schema_version": 1, "snapshot_id": "snapshot-abc123", "verdict": "pass",
         "summary": "fixture pass", "findings": [], "questions": []}
text = "not json" if mode == "bad-json" else json.dumps(value)
if "--output-last-message" in args:
    Path("stdin.txt").write_text(sys.stdin.read(), encoding="utf-8")
    final_value = {**value, "summary": "DIFFERENT"} if mode == "disagree" else value
    final_text = "not json" if mode == "bad-json" else json.dumps(final_value)
    Path(args[args.index("--output-last-message") + 1]).write_text(final_text, encoding="utf-8")
    print(json.dumps({"type": "thread.started", "thread_id": "thread-1"}))
    print(json.dumps({"type": "turn.started"}))
    if mode == "tool":
        print(json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": "echo unsafe"}}))
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}))
    print(json.dumps({"type": "turn.completed", "usage": {}}))
else:
    if "--standalone" not in args:
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


class ValidationTests(unittest.TestCase):
    def test_pass_and_no_mutation(self):
        value = copy.deepcopy(PASS)
        self.assertEqual(adapters.validate_review(value), PASS)
        self.assertEqual(value, PASS)

    def test_snapshot_is_required(self):
        value = {k: v for k, v in PASS.items() if k != "snapshot_id"}
        with self.assertRaises(BuildFlowError):
            adapters.validate_review(value)

    def test_unknown_fields(self):
        with self.assertRaises(BuildFlowError):
            adapters.validate_review({**PASS, "execute": "rm -rf ."})

    def test_boolean_version_rejected(self):
        with self.assertRaises(BuildFlowError):
            adapters.validate_review({**PASS, "schema_version": True})

    def test_bad_snapshot_identifiers(self):
        for snapshot in (None, "", "abc/../../x", "a b"):
            with self.subTest(snapshot=snapshot), self.assertRaises(BuildFlowError):
                adapters.validate_review({**PASS, "snapshot_id": snapshot})

    def test_pass_cannot_hide_findings_or_questions(self):
        for change in ({"findings": [FINDING]}, {"questions": ["What scope?"]}):
            with self.subTest(change=change), self.assertRaises(BuildFlowError):
                adapters.validate_review({**PASS, **change})

    def test_changes_need_findings(self):
        with self.assertRaises(BuildFlowError):
            adapters.validate_review({**PASS, "verdict": "changes_required"})
        adapters.validate_review({**PASS, "verdict": "changes_required", "findings": [FINDING]})

    def test_human_required_needs_question(self):
        with self.assertRaises(BuildFlowError):
            adapters.validate_review({**PASS, "verdict": "human_required"})
        adapters.validate_review({**PASS, "verdict": "human_required", "questions": ["Which public API?"]})

    def test_human_flag_cannot_be_automatic(self):
        with self.assertRaises(BuildFlowError):
            adapters.validate_review({**PASS, "verdict": "changes_required",
                                     "findings": [{**FINDING, "requires_human": True}]})

    def test_duplicate_finding_id(self):
        with self.assertRaises(BuildFlowError):
            adapters.validate_review({**PASS, "verdict": "changes_required", "findings": [FINDING, FINDING]})

    def test_invalid_line_ranges(self):
        for start, end in ((-1, 0), (0, 1), (5, 2), (True, 2), (1, "2")):
            with self.subTest(start=start, end=end), self.assertRaises(BuildFlowError):
                adapters.validate_review({**PASS, "verdict": "changes_required",
                                         "findings": [{**FINDING, "line_start": start, "line_end": end}]})

    def test_cross_cutting_finding(self):
        adapters.validate_review({**PASS, "verdict": "changes_required",
                                 "findings": [{**FINDING, "file": "*", "line_start": 0, "line_end": 0}]})

    def test_paths_must_be_relative(self):
        for path in ("/tmp/a", "../a", "C:\\secret", "src/../secret", "src\\a"):
            with self.subTest(path=path), self.assertRaises(BuildFlowError):
                adapters.validate_review({**PASS, "verdict": "changes_required",
                                         "findings": [{**FINDING, "file": path}]})

    def test_json_duplicates_constants_and_fences_rejected(self):
        for raw in ('{"a":1,"a":2}', '{"x":NaN}', '```json\n{}\n```'):
            with self.subTest(raw=raw), self.assertRaises(BuildFlowError):
                adapters._json(raw)


class EventTests(unittest.TestCase):
    def test_codex_pass(self):
        self.assertEqual(adapters.parse_codex_events(codex_events()), PASS)

    def test_opencode_pass(self):
        self.assertEqual(adapters.parse_opencode_events(opencode_events()), PASS)

    def test_codex_tool_rejected(self):
        lines = codex_events().splitlines()
        lines.insert(2, json.dumps({"type": "item.completed", "item": {"type": "mcp_tool_call"}}))
        with self.assertRaisesRegex(BuildFlowError, "tool"):
            adapters.parse_codex_events("\n".join(lines))

    def test_codex_no_normal_finish(self):
        with self.assertRaises(BuildFlowError):
            adapters.parse_codex_events("\n".join(codex_events().splitlines()[:-1]))

    def test_codex_multiple_answers(self):
        lines = codex_events().splitlines()
        lines.insert(-1, lines[-2])
        with self.assertRaises(BuildFlowError):
            adapters.parse_codex_events("\n".join(lines))

    def test_codex_error_after_answer(self):
        with self.assertRaises(BuildFlowError):
            adapters.parse_codex_events(codex_events() + '\n{"type":"error"}')

    def test_opencode_tool_error_unknown_rejected(self):
        for kind in ("tool_use", "error", "unknown"):
            with self.subTest(kind=kind), self.assertRaises(BuildFlowError):
                adapters.parse_opencode_events(json.dumps({"type": kind}) + "\n" + opencode_events())

    def test_opencode_missing_finish_or_truncation(self):
        for text in ("\n".join(opencode_events().splitlines()[:-1]), opencode_events().replace('"stop"', '"length"')):
            with self.subTest(text=text), self.assertRaises(BuildFlowError):
                adapters.parse_opencode_events(text)

    def test_opencode_duplicate_part_deduplicated(self):
        lines = opencode_events().splitlines()
        lines.insert(2, lines[1])
        self.assertEqual(adapters.parse_opencode_events("\n".join(lines)), PASS)

    def test_opencode_multiple_sessions_rejected(self):
        lines = opencode_events().splitlines()
        lines[-1] = lines[-1].replace("session-1", "session-2")
        with self.assertRaises(BuildFlowError):
            adapters.parse_opencode_events("\n".join(lines))


class CommandTests(unittest.TestCase):
    @staticmethod
    def config(kind="codex"):
        return {"kind": kind, "id": kind, "command": [kind],
                "model": "gpt-6.1-sol" if kind == "codex" else "opencode-go/deepseek-v4.1-flash",
                "effort": "xhigh" if kind == "codex" else "high", "timeout_seconds": 10}

    def test_codex_uses_isolated_fresh_read_only_flags(self):
        argv, env = adapters.build_command(self.config(), Path("round 1"), Path("schema.json"))
        for flag in ("--strict-config", "--ignore-user-config", "--ignore-rules", "--ephemeral", "--json", "--output-schema"):
            self.assertIn(flag, argv)
        self.assertIn('model_reasoning_effort="xhigh"', argv)
        self.assertIn("features.apps=false", argv)
        self.assertIn("features.hooks=false", argv)
        self.assertIn("features.plugins=false", argv)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertEqual(argv[-1], "-")
        self.assertEqual(env, {})
        self.assertNotIn("--resume", argv)

    def test_v2_is_default_and_uses_variant_selector(self):
        argv, env = adapters.build_command(self.config("opencode"), Path("round 1"), Path("schema.json"))
        self.assertIn("--standalone", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "opencode-go/deepseek-v4.1-flash#high")
        self.assertNotIn("--variant", argv)
        self.assertIn("OPENCODE_CONFIG_DIR", env)

    def test_v1_must_be_explicit(self):
        argv, env = adapters.build_command({**self.config("opencode"), "protocol_version": 1}, Path("r"), Path("s"))
        self.assertIn("--pure", argv)
        self.assertEqual(argv[argv.index("--variant") + 1], "high")
        self.assertEqual(json.loads(env["OPENCODE_CONFIG_CONTENT"])["permission"], "deny")

    def test_v2_permissions_and_policy(self):
        config = adapters.runtime_config("opencode-go/deepseek-v4.1-flash", 2, "my-reviewer")
        self.assertEqual(config["permissions"], [{"action": "*", "resource": "*", "effect": "deny"}])
        self.assertEqual(config["agents"]["my-reviewer"]["permissions"], config["permissions"])
        self.assertEqual({p["action"] for p in config["policies"]}, {"tool.use", "mcp.use", "skill.use", "plugin.use"})
        self.assertFalse(config["compaction"]["auto"])
        self.assertFalse(config["warming"])

    def test_shell_string_model_flags_and_bypass_rejected(self):
        for change in ({"command": "codex; touch BAD"}, {"command": ["bash", "-c", "codex"]},
                       {"model": "gpt-6.1-sol --yolo"}, {"command": ["codex", "--yolo"]}):
            with self.subTest(change=change), self.assertRaises(BuildFlowError):
                adapters.build_command({**self.config(), **change}, Path("r"), Path("s"))

    def test_cmd_shim_rejected(self):
        with patch.object(adapters.shutil, "which", return_value="C:/bin/codex.cmd"):
            with self.assertRaises(BuildFlowError):
                adapters._resolve(["codex"])

    def test_bad_output_limit_rejected(self):
        for limit in (0, True, 1023, 16*1024*1024 + 1):
            with self.subTest(limit=limit), self.assertRaises(BuildFlowError):
                adapters.build_command({**self.config(), "max_output_bytes": limit}, Path("r"), Path("s"))


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="buildflow adapters ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.packet = self.base / "packet.md"
        self.packet.write_text("snapshot_id: snapshot-abc123\n日本語\n$(touch BAD)\n", encoding="utf-8")
        self.fake = self.base / "fake reviewer.py"
        self.fake.write_text(FAKE, encoding="utf-8")
        self.output = self.base / "round 1"

    def config(self, kind="codex", mode="pass", protocol=2, timeout=10):
        return {**CommandTests.config(kind), "command": [sys.executable, str(self.fake), mode],
                "protocol_version": protocol, "timeout_seconds": timeout}

    def run_fixture(self, kind="codex", mode="pass", protocol=2, timeout=10):
        return adapters.run_review(self.config(kind, mode, protocol, timeout), self.packet,
                                   self.output, PLUGIN / "schemas" / "review.schema.json",
                                   PLUGIN / "prompts" / "reviewer.md")

    def test_preflight_is_unpaid_and_does_not_create_workdir(self):
        result = adapters.preflight(self.config())
        self.assertEqual(result["requested_model"], "gpt-6.1-sol")
        self.assertFalse(result["effective_model_attested"])
        self.assertFalse(self.output.exists())
        self.assertFalse((self.base / "seen.json").exists())

    def test_codex_process(self):
        result = self.run_fixture()
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(result["snapshot_id"], "snapshot-abc123")
        self.assertTrue((self.output / "stdout.jsonl").is_file())
        self.assertTrue((self.output / "stderr.log").is_file())
        meta = json.loads((self.output / "invocation.json").read_text())
        self.assertFalse(meta["effective_model_attested"])
        self.assertEqual(meta["requested_effort"], "xhigh")
        self.assertEqual(Path(meta["cwd"]), self.output / "workspace")
        prompt = (self.output / "workspace" / "stdin.txt").read_text(encoding="utf-8")
        self.assertIn("日本語", prompt)
        self.assertIn("$(touch BAD)", prompt)
        self.assertFalse((self.output / "workspace" / "BAD").exists())

    def test_opencode_v2_process_and_isolation(self):
        with patch.dict(adapters.os.environ, {"OPENCODE_CONFIG": "/unsafe/config.json", "OPENCODE_CONFIG_CONTENT": "unsafe", "OPENCODE_PERMISSION": "allow"}):
            result = self.run_fixture("opencode")
        self.assertEqual(result["verdict"], "pass")
        seen = json.loads((self.output / "workspace" / "seen.json").read_text())
        self.assertIsNone(seen["config"])
        self.assertIsNone(seen["content"])
        self.assertIsNone(seen["permission"])
        runtime = json.loads((self.output / "workspace" / "opencode.json").read_text())
        agent_id = seen["args"][seen["args"].index("--agent") + 1]
        self.assertIn(agent_id, runtime["agents"])
        self.assertEqual(Path(seen["config_dir"]), self.output / "workspace")

    def test_opencode_v1_process(self):
        self.assertEqual(self.run_fixture("opencode", "v1", 1)["verdict"], "pass")

    def test_wrong_protocol_refuses_dispatch(self):
        with self.assertRaisesRegex(BuildFlowError, "configured v1"):
            self.run_fixture("opencode", "pass", 1)
        self.assertFalse((self.output / "workspace" / "seen.json").exists())

    def test_old_v2_refuses_dispatch(self):
        with self.assertRaisesRegex(BuildFlowError, "2.2.3"):
            self.run_fixture("opencode", "v2-old")

    def test_old_codex_refuses_weaker_fallback(self):
        with self.assertRaisesRegex(BuildFlowError, "required review flags"):
            self.run_fixture("codex", "old-codex")
        self.assertFalse((self.output / "workspace" / "seen.json").exists())

    def test_bad_response_retains_raw_logs(self):
        with self.assertRaises(BuildFlowError):
            self.run_fixture("codex", "bad-json")
        self.assertTrue((self.output / "final-response.json").is_file())
        self.assertTrue((self.output / "stdout.jsonl").is_file())

    def test_file_event_disagreement_rejected(self):
        with self.assertRaisesRegex(BuildFlowError, "disagree"):
            self.run_fixture("codex", "disagree")

    def test_tool_attempt_rejected(self):
        with self.assertRaisesRegex(BuildFlowError, "tool"):
            self.run_fixture("codex", "tool")

    def test_nonzero_exit(self):
        with self.assertRaisesRegex(BuildFlowError, "exited with 7"):
            self.run_fixture("codex", "exit-error")
        self.assertIn("fixture failure", (self.output / "stderr.log").read_text())

    def test_timeout_does_not_retry(self):
        with self.assertRaisesRegex(BuildFlowError, "timed out"):
            self.run_fixture("codex", "timeout", timeout=0.1)
        self.assertTrue((self.output / "invocation.json").is_file())

    def test_output_size_bounded(self):
        with patch.object(adapters, "MAX_OUTPUT_BYTES", 1024):
            with self.assertRaisesRegex(BuildFlowError, "size limit"):
                self.run_fixture("codex", "too-large")
        self.assertLessEqual((self.output / "stdout.jsonl").stat().st_size, 1024)

    def test_no_replay_or_overwrite(self):
        self.run_fixture()
        with self.assertRaisesRegex(BuildFlowError, "already exists"):
            self.run_fixture()

    def test_per_reviewer_output_limit(self):
        config = {**self.config(mode="too-large"), "max_output_bytes": 1024}
        with self.assertRaisesRegex(BuildFlowError, "size limit"):
            adapters.run_review(config, self.packet, self.output,
                                PLUGIN / "schemas" / "review.schema.json", PLUGIN / "prompts" / "reviewer.md")
        self.assertLessEqual((self.output / "stdout.jsonl").stat().st_size, 1024)

    def test_oversized_packet_rejected_before_dispatch(self):
        with patch.object(adapters, "MAX_PACKET_BYTES", 2):
            with self.assertRaisesRegex(BuildFlowError, "byte limit"):
                self.run_fixture()
        self.assertFalse((self.output / "workspace" / "seen.json").exists())


if __name__ == "__main__":
    unittest.main()
