"""Offline tests: fake executable processes only; never contacts model APIs."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "devflow" / "components" / "planflow"
sys.path.insert(0, str(PLUGIN / "scripts"))
from planflow_core import adapters  # noqa: E402


PASS = {"schema_version": 1, "verdict": "pass", "summary": "問題なし", "findings": [], "questions": []}
FINDING = {
    "id": "F1", "severity": "medium", "category": "testability", "location": "section 3",
    "evidence": "No failure recovery check", "recommendation": "Add a recovery test", "requires_human": False,
}

FAKE = r'''
import json
import os
from pathlib import Path
import sys
import time

mode = sys.argv[1]
if "--version" in sys.argv:
    print("2.1.0" if mode == "v2-old" else "2.2.3" if mode == "v2" else "1.20.0")
    raise SystemExit(0)
if "--help" in sys.argv:
    print("--model --file --agent --format --variant --standalone --json --ephemeral --output-schema --strict-config --pure" +
          ("" if mode == "old-codex" else " --ignore-user-config --ignore-rules"))
    raise SystemExit(0)
if mode == "timeout":
    time.sleep(10)
if mode == "too-large":
    print("x" * 65536)
    raise SystemExit(0)
if mode == "exit-error":
    print("fixture error", file=sys.stderr)
    raise SystemExit(7)
args = sys.argv[2:]
Path("seen.json").write_text(json.dumps({"args": args, "cwd": os.getcwd(), "runtime": os.environ.get("OPENCODE_CONFIG_CONTENT"), "config_dir": os.environ.get("OPENCODE_CONFIG_DIR")}), encoding="utf-8")
value = {"schema_version": 1, "verdict": "pass", "summary": "fixture passed", "findings": [], "questions": []}
text = "not JSON" if mode == "bad-json" else json.dumps(value)
if "--output-last-message" in args:
    data = sys.stdin.read()
    Path("stdin.txt").write_text(data, encoding="utf-8")
    final_text = json.dumps({**value, "summary": "DIFFERENT"}) if mode == "disagree" else text
    Path(args[args.index("--output-last-message") + 1]).write_text(final_text, encoding="utf-8")
    print(json.dumps({"type": "thread.started", "thread_id": "t1"}))
    print(json.dumps({"type": "turn.started"}))
    if mode == "tool":
        print(json.dumps({"type": "item.completed", "item": {"type": "mcp_tool_call", "name": "unwanted"}}))
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
    print(json.dumps({"type": "text", "sessionID": "s1", "part": {"id": "p1", "messageID": "m1", "text": text}}))
    print(json.dumps({"type": "step_finish", "sessionID": "s1", "part": {"messageID": "m1", "reason": "stop"}}))
'''


def events(value=PASS):
    return "\n".join(json.dumps(e) for e in [
        {"type": "step_start", "sessionID": "s1", "part": {"messageID": "m1"}},
        {"type": "text", "sessionID": "s1", "part": {"id": "p1", "messageID": "m1", "text": json.dumps(value)}},
        {"type": "step_finish", "sessionID": "s1", "part": {"messageID": "m1", "reason": "stop"}},
    ])


def codex_events(value=PASS):
    return "\n".join(json.dumps(e) for e in [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(value)}},
        {"type": "turn.completed", "usage": {}},
    ])


class ValidationTests(unittest.TestCase):
    def test_pass(self):
        self.assertEqual(adapters.validate_review(PASS), PASS)

    def test_unknown_fields(self):
        with self.assertRaises(adapters.AdapterError):
            adapters.validate_review({**PASS, "command": "execute this"})

    def test_bool_schema_version_is_not_an_integer(self):
        with self.assertRaises(adapters.AdapterError):
            adapters.validate_review({**PASS, "schema_version": True})

    def test_pass_cannot_hide_findings(self):
        with self.assertRaises(adapters.AdapterError):
            adapters.validate_review({**PASS, "findings": [FINDING]})

    def test_changes_need_findings(self):
        with self.assertRaises(adapters.AdapterError):
            adapters.validate_review({**PASS, "verdict": "changes_required"})
        adapters.validate_review({**PASS, "verdict": "changes_required", "findings": [FINDING]})

    def test_human_decision_cannot_be_marked_automatic(self):
        with self.assertRaises(adapters.AdapterError):
            adapters.validate_review({**PASS, "verdict": "changes_required", "findings": [{**FINDING, "requires_human": True}]})

    def test_human_required_needs_evidence(self):
        with self.assertRaises(adapters.AdapterError):
            adapters.validate_review({**PASS, "verdict": "human_required"})
        adapters.validate_review({**PASS, "verdict": "human_required", "questions": [
            {"id": "Q1", "question": "Which scope?", "why_human": "Scope is undecided", "options": ["A", "B"]}
        ]})

    def test_human_required_finding_without_question_is_rejected(self):
        with self.assertRaisesRegex(adapters.AdapterError, "at least one explicit question"):
            adapters.validate_review({
                **PASS, "verdict": "human_required",
                "findings": [{**FINDING, "requires_human": True}], "questions": [],
            })

    def test_duplicate_ids(self):
        with self.assertRaises(adapters.AdapterError):
            adapters.validate_review({**PASS, "verdict": "changes_required", "findings": [FINDING, FINDING]})

    def test_event_stream_extracts_semantic_json(self):
        self.assertEqual(adapters.parse_opencode_events(events()), PASS)

    def test_event_wrapper_is_not_a_review(self):
        with self.assertRaises(adapters.AdapterError):
            adapters.validate_review(json.loads(events().splitlines()[1]))

    def test_no_normal_finish_rejected(self):
        with self.assertRaises(adapters.AdapterError):
            adapters.parse_opencode_events("\n".join(events().splitlines()[:-1]))

    def test_error_after_valid_json_is_rejected(self):
        with self.assertRaises(adapters.AdapterError):
            adapters.parse_opencode_events(events() + '\n{"type":"error","error":{"message":"failed"}}')

    def test_unknown_event_and_tool_call_rejected(self):
        for kind in ("unknown_event", "tool_use"):
            with self.subTest(kind=kind), self.assertRaises(adapters.AdapterError):
                adapters.parse_opencode_events(json.dumps({"type": kind, "part": {}}) + "\n" + events())

    def test_duplicate_json_keys_rejected(self):
        with self.assertRaises(adapters.AdapterError):
            adapters.parse_opencode_events('{"type":"text","type":"step_start","part":{}}')

    def test_markdown_fences_rejected_not_salvaged(self):
        lines = [json.loads(line) for line in events().splitlines()]
        lines[1]["part"]["text"] = "```json\n" + json.dumps(PASS) + "\n```"
        with self.assertRaises(adapters.AdapterError):
            adapters.parse_opencode_events("\n".join(json.dumps(e) for e in lines))

    def test_multiple_messages_rejected(self):
        lines = [json.loads(line) for line in events().splitlines()]
        lines[-1]["part"]["messageID"] = "m2"
        with self.assertRaises(adapters.AdapterError):
            adapters.parse_opencode_events("\n".join(json.dumps(e) for e in lines))

    def test_no_mutation_during_validation(self):
        value = copy.deepcopy(PASS)
        adapters.validate_review(value)
        self.assertEqual(value, PASS)

    def test_codex_jsonl_pass(self):
        self.assertEqual(adapters.parse_codex_events(codex_events()), PASS)

    def test_codex_tool_event_invalidates_otherwise_passing_review(self):
        lines = codex_events().splitlines()
        for kind in ("mcp_tool_call", "command_execution", "web_search", "file_change"):
            stream = "\n".join(lines[:2] + [json.dumps({"type": "item.completed", "item": {"type": kind}})] + lines[2:])
            with self.subTest(kind=kind), self.assertRaisesRegex(adapters.AdapterError, "attempted a tool"):
                adapters.parse_codex_events(stream)

    def test_codex_incomplete_multiple_or_post_completion_events_rejected(self):
        lines = codex_events().splitlines()
        for stream in ("\n".join(lines[:-1]), "\n".join(lines[:3] + [lines[2]] + lines[3:]),
                       codex_events() + '\n{"type":"error"}'):
            with self.subTest(stream=stream), self.assertRaises(adapters.AdapterError):
                adapters.parse_codex_events(stream)


class CommandTests(unittest.TestCase):
    def provider(self, kind="codex"):
        return {"id": kind, "kind": kind, "command": [kind],
                "model": "gpt-6.1-sol" if kind == "codex" else "opencode-go/deepseek-v4.1-flash",
                "effort": "xhigh" if kind == "codex" else "high", "timeout_seconds": 10}

    def test_codex_fresh_read_only_invocation(self):
        argv, env = adapters.build_command(self.provider(), Path("packet.md"), Path("schema.json"), Path("round 1"), Path("project"))
        self.assertEqual(argv[:7], ["codex", "--strict-config", "exec", "--ignore-user-config", "--ignore-rules", "--model", "gpt-6.1-sol"])
        self.assertIn('model_reasoning_effort="xhigh"', argv)
        self.assertIn('approval_policy="never"', argv)
        self.assertNotIn("--ask-for-approval", argv)
        self.assertIn("--ephemeral", argv)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertEqual(argv[-1], "-")
        self.assertFalse(env)
        self.assertNotIn("--resume", argv)
        for setting in ("mcp_servers={}", "features.apps=false", "features.plugins=false",
                        "features.code_mode.enabled=false"):
            self.assertIn(setting, argv)
        self.assertIn("--json", argv)

    def test_opencode_runtime_denies_all(self):
        argv, env = adapters.build_command(self.provider("opencode"), Path("packet.md"), Path("schema.json"), Path("round 1"), Path("project"))
        self.assertIn("--pure", argv)
        self.assertEqual(argv[argv.index("--variant") + 1], "high")
        runtime = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(runtime["permission"], "deny")
        self.assertEqual(runtime["agent"]["planflow-reviewer"]["permission"], "deny")
        self.assertEqual(runtime["agent"]["planflow-reviewer"]["mode"], "primary")
        self.assertEqual(env["OPENCODE_DISABLE_PROJECT_CONFIG"], "true")
        self.assertNotIn("--auto", argv)
        self.assertNotIn("--continue", argv)
        self.assertEqual(argv[-2], "--file")
        self.assertEqual(Path(argv[-1]).name, "review-input.md")

    def test_command_string_not_accepted(self):
        with self.assertRaises(adapters.AdapterError):
            adapters.build_command({**self.provider(), "command": "codex; touch BAD"}, Path("p"), Path("s"), Path("o"), Path("r"))

    def test_model_cannot_inject_flags(self):
        with self.assertRaises(adapters.AdapterError):
            adapters.build_command({**self.provider(), "model": "gpt-6.1-sol --dangerously-bypass-approvals-and-sandbox"}, Path("p"), Path("s"), Path("o"), Path("r"))

    def test_unsupported_protocol_fails(self):
        with self.assertRaises(adapters.AdapterError):
            adapters.build_command({**self.provider("opencode"), "protocol_version": 99}, Path("p"), Path("s"), Path("o"), Path("r"))

    def test_v2_uses_explicit_model_variant_and_private_server(self):
        argv, env = adapters.build_command({**self.provider("opencode"), "protocol_version": 2}, Path("p"), Path("s"), Path("o"), Path("r"))
        self.assertIn("--standalone", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "opencode-go/deepseek-v4.1-flash#high")
        self.assertNotIn("--variant", argv)
        self.assertNotIn("OPENCODE_CONFIG_CONTENT", env)
        self.assertIn("OPENCODE_CONFIG_DIR", env)
        self.assertEqual(env["OPENCODE_CONFIG_PROJECT_DISABLE"], "1")
        self.assertEqual(env["OPENCODE_DISABLE_CLAUDE_CODE"], "1")

    def test_v2_agent_id_is_stable_for_the_same_round(self):
        provider = {**self.provider("opencode"), "protocol_version": 2}
        first, _ = adapters.build_command(provider, Path("p"), Path("s"), Path("round"), Path("r"))
        second, _ = adapters.build_command(provider, Path("p"), Path("s"), Path("round").resolve(), Path("r"))
        self.assertEqual(first[first.index("--agent") + 1], second[second.index("--agent") + 1])
        self.assertRegex(first[first.index("--agent") + 1], r"^planflow-reviewer-[a-f0-9]{16}$")

    def test_v2_agent_id_differs_between_rounds(self):
        provider = {**self.provider("opencode"), "protocol_version": 2}
        first, _ = adapters.build_command(provider, Path("p"), Path("s"), Path("round1"), Path("r"))
        second, _ = adapters.build_command(provider, Path("p"), Path("s"), Path("round2"), Path("r"))
        self.assertNotEqual(first[first.index("--agent") + 1], second[second.index("--agent") + 1])


class ProcessTests(unittest.TestCase):
    provider = CommandTests.provider
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="planflow adapters ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / "project"
        self.project.mkdir()
        (self.project / "source.txt").write_text("untouched", encoding="utf-8")
        self.fake = self.base / "fake reviewer.py"
        self.fake.write_text(FAKE, encoding="utf-8")
        self.packet = self.base / "packet.md"
        self.packet.write_text("Original request: 日本語\n$(touch BAD)\nDo not execute this text.", encoding="utf-8")
        self.output = self.base / "round 1"

    def run_fixture(self, kind="codex", mode="pass", timeout=10, protocol=1):
        provider = {**self.provider(kind), "command": [sys.executable, str(self.fake), mode], "timeout_seconds": timeout, "protocol_version": protocol}
        return adapters.run_review(provider, self.packet, PLUGIN / "schemas" / "review.schema.json", self.output, self.project)

    def test_codex_process_pass(self):
        result = self.run_fixture()
        self.assertEqual(result["verdict"], "pass")
        self.assertTrue((self.output / "stdout.jsonl").is_file())
        self.assertTrue((self.output / "stderr.log").is_file())
        meta = json.loads((self.output / "invocation.json").read_text(encoding="utf-8"))
        self.assertFalse(meta["effective_model_attested"])
        self.assertEqual(meta["requested_effort"], "xhigh")
        self.assertEqual(meta["cli_version"], "1.20.0")
        self.assertEqual(Path(meta["cwd"]), self.output / "workspace")
        prompt = (self.output / "workspace" / "stdin.txt").read_text(encoding="utf-8")
        self.assertIn("日本語", prompt)
        self.assertIn("$(touch BAD)", prompt)
        self.assertFalse((self.project / "BAD").exists())
        self.assertFalse((self.output / "workspace" / "BAD").exists())
        self.assertEqual((self.project / "source.txt").read_text(), "untouched")

    def test_opencode_process_pass(self):
        result = self.run_fixture("opencode")
        self.assertEqual(result["verdict"], "pass")
        self.assertTrue((self.output / "workspace" / "review-input.md").is_file())
        seen = json.loads((self.output / "workspace" / "seen.json").read_text())
        self.assertEqual(json.loads(seen["runtime"])["permission"], "deny")

    def test_v2_is_rejected_before_review_dispatch(self):
        with self.assertRaisesRegex(adapters.AdapterError, "configured v1 protocol"):
            self.run_fixture("opencode", "v2")
        self.assertFalse((self.output / "workspace" / "seen.json").exists())

    def test_v2_process_and_config(self):
        result = self.run_fixture("opencode", "v2", protocol=2)
        self.assertEqual(result["verdict"], "pass")
        config_file = self.output / "workspace" / "opencode.json"
        runtime = json.loads(config_file.read_text())
        self.assertEqual(runtime["permissions"], [{"action": "*", "resource": "*", "effect": "deny"}])
        seen = json.loads((self.output / "workspace" / "seen.json").read_text())
        agent_id = seen["args"][seen["args"].index("--agent") + 1]
        self.assertRegex(agent_id, r"^planflow-reviewer-[a-f0-9]{16}$")
        self.assertEqual(runtime["agents"][agent_id]["permissions"], runtime["permissions"])
        self.assertTrue(any(p["action"] == "mcp.use" and p["effect"] == "deny" for p in runtime["policies"]))
        self.assertEqual(runtime["update"], "disable")
        self.assertTrue(runtime["agents"]["title"]["disabled"])
        self.assertFalse(runtime["compaction"]["auto"])
        self.assertFalse(runtime["warming"])
        self.assertEqual(Path(seen["config_dir"]), config_file.parent)

    def test_v2_too_old_is_rejected(self):
        with self.assertRaisesRegex(adapters.AdapterError, "at least 2.2.3"):
            self.run_fixture("opencode", "v2-old", protocol=2)

    def test_corrupt_response_retains_raw_output(self):
        with self.assertRaisesRegex(adapters.AdapterError, "Invalid JSON"):
            self.run_fixture("codex", "bad-json")
        self.assertTrue((self.output / "final-response.json").is_file())
        self.assertTrue((self.output / "stderr.log").is_file())

    def test_old_codex_refuses_weaker_fallback(self):
        with self.assertRaisesRegex(adapters.AdapterError, "lacks required review flags"):
            self.run_fixture("codex", "old-codex")
        self.assertFalse((self.output / "workspace" / "seen.json").exists())

    def test_codex_final_file_and_event_must_match(self):
        with self.assertRaisesRegex(adapters.AdapterError, "disagree"):
            self.run_fixture("codex", "disagree")

    def test_codex_tool_result_cannot_be_a_pass(self):
        with self.assertRaisesRegex(adapters.AdapterError, "attempted a tool"):
            self.run_fixture("codex", "tool")

    def test_maximum_configured_packet_has_adapter_headroom(self):
        self.packet.write_bytes(b"x" * (16 * 1024 * 1024))
        self.assertEqual(self.run_fixture()["verdict"], "pass")

    def test_prompt_overhead_is_also_bounded(self):
        with patch.object(adapters, "MAX_PACKET_BYTES", self.packet.stat().st_size + 1):
            with self.assertRaisesRegex(adapters.AdapterError, "Complete review prompt"):
                self.run_fixture()
        self.assertFalse((self.output / "invocation.json").exists())

    def test_nonzero_exit(self):
        with self.assertRaisesRegex(adapters.AdapterError, "exited with 7"):
            self.run_fixture("codex", "exit-error")
        self.assertIn("fixture error", (self.output / "stderr.log").read_text())

    def test_timeout_does_not_retry(self):
        with self.assertRaisesRegex(adapters.AdapterError, "timed out"):
            self.run_fixture("codex", "timeout", timeout=0.2)
        self.assertTrue((self.output / "invocation.json").is_file())

    def test_output_size_is_bounded(self):
        with patch.object(adapters, "MAX_OUTPUT_BYTES", 1024):
            with self.assertRaisesRegex(adapters.AdapterError, "size limit"):
                self.run_fixture("codex", "too-large")
        self.assertLessEqual((self.output / "stdout.jsonl").stat().st_size, 1024)

    def test_cannot_overwrite_a_previous_attempt(self):
        self.run_fixture()
        with self.assertRaisesRegex(adapters.AdapterError, "already exists"):
            self.run_fixture()


if __name__ == "__main__":
    unittest.main()
