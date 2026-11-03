"""Real offline engine/reviewer/hook/check integration, with no mocked seams.

The fake native Python CLI handles only local help/version probes and emits
fixture review protocols. It performs no authentication or network/model calls.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


from devflow_core import router
from devflow_core.common.errors import DevFlowError
from devflow_core.common.paths import PLUGIN_ROOT
from devflow_core.common.storage import atomic_write_json, digest, read_json
from devflow_core.implement.engine import Engine
from devflow_core.implement.workflow import WORKFLOW


FAKE_REVIEWER = r'''
import hashlib
import json
import os
from pathlib import Path
import sys

kind, mode = sys.argv[1:3]
args = sys.argv[3:]
if "--version" in args:
    print("opencode 2.2.3" if kind == "opencode" else "codex offline-fixture-1")
    raise SystemExit(0)
if "--help" in args:
    print("--strict-config --ignore-user-config --ignore-rules --output-schema "
          "--json --ephemeral --format --agent --model --file --standalone")
    raise SystemExit(0)

if kind == "codex":
    content = sys.stdin.read()
    source = "stdin"
else:
    source = args[args.index("--file") + 1]
    content = Path(source).read_text(encoding="utf-8")

packet_text = content.split("\n\n## Untrusted review packet\n", 1)[1]
packet = json.loads(packet_text)
seen = {
    "kind": kind, "args": args, "cwd": os.getcwd(), "input_source": source,
    "snapshot_id": packet["snapshot_id"], "plan": packet["plan"],
    "packet_sha256": hashlib.sha256(packet_text.encode("utf-8")).hexdigest(),
    "validation_status": packet["validation"]["status"],
    "check_status": packet["check_results"]["status"],
    "previous_review_count": len(packet["previous_reviews"]),
    "files_at_start": sorted(p.name for p in Path.cwd().iterdir()),
    "opencode_config_dir": os.environ.get("OPENCODE_CONFIG_DIR"),
}
Path("seen.json").write_text(json.dumps(seen), encoding="utf-8")
review = {
    "schema_version": 1, "snapshot_id": packet["snapshot_id"], "verdict": "pass",
    "summary": "Offline fixture independently accepted the exact packet.",
    "findings": [], "questions": [],
}
answer = json.dumps(review)
if kind == "codex":
    final_text = '{"schema_version":' if mode == "bad-final" else answer
    Path(args[args.index("--output-last-message") + 1]).write_text(final_text, encoding="utf-8")
    events = [
        {"type": "thread.started", "thread_id": "offline-thread"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": answer}},
        {"type": "turn.completed", "usage": {"input_tokens": 0, "output_tokens": 0}},
    ]
else:
    events = [
        {"type": "step_start", "sessionID": "offline-session", "part": {"messageID": "offline-message"}},
        {"type": "text", "sessionID": "offline-session", "part": {
            "messageID": "offline-message", "id": "offline-text", "text": answer}},
        {"type": "step_finish", "sessionID": "offline-session", "part": {
            "messageID": "offline-message", "reason": "stop"}},
    ]
for event in events:
    print(json.dumps(event), flush=True)
'''


class FullStackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="devflow implement full stack ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.project = self.base / "project with spaces"
        self.project.mkdir()
        self.fake = self.base / "fake reviewer.py"
        self.fake.write_text(FAKE_REVIEWER, encoding="utf-8")
        self.plan = self.base / "standalone plan.md"
        self.plan.write_text("# 実装プラン\n\nSet VALUE to 1 and validate it.\n", encoding="utf-8")
        self.source = self.project / "value.py"
        self.source.write_text("VALUE = 0\n", encoding="utf-8")
        self.session = "full-stack-human-session"
        self.engine = Engine(self.project)

    def hook(self, event, **fields):
        return router.handle_hook({"hook_event_name": event, "cwd": str(self.project),
                                   "session_id": self.session, **fields}, PLUGIN_ROOT)

    def tool(self, name="Write", **tool_input):
        return self.hook("PreToolUse", tool_name=name, tool_input=tool_input)

    def prepare(self, mode="pass"):
        reviewers = [
            {"id": "codex", "kind": "codex", "command": [sys.executable, str(self.fake), "codex", mode],
             "model": "gpt-6.1-sol", "effort": "xhigh", "timeout_seconds": 10},
            {"id": "opencode", "kind": "opencode", "command": [sys.executable, str(self.fake), "opencode", "pass"],
             "model": "opencode-go/deepseek-v4.1-flash", "effort": "high", "timeout_seconds": 10, "protocol_version": 2},
        ]
        check_script = "from pathlib import Path; assert Path('value.py').read_text(encoding='utf-8') == 'VALUE = 1\\n'; print('implementation checked')"
        atomic_write_json(self.project / "devflow.config.json", {"schema_version": 1, "implement": {
            "snapshot": {"mode": "files"}, "reviewers": reviewers,
            "checks": [{"id": "verify", "argv": [sys.executable, "-c", check_script], "required": True, "timeout_seconds": 10}],
        }})
        hook = self.hook("UserPromptSubmit", prompt=f'/devflow:implement "{self.plan}"')
        self.assertIn("start is pending", hook["hookSpecificOutput"]["additionalContext"])
        started = router.runner_start(self.project, self.session, "implement", str(self.plan))
        self.assertEqual(started["status"], "implementing")
        self.run_id = started["run_id"]
        self.run = Path(started["run_dir"])
        self.assertIsNone(self.tool(file_path=str(self.source)))
        self.source.write_text("VALUE = 1\n", encoding="utf-8")
        summary = Path(started["draft_dir"]) / "summary.md"
        self.assertIsNone(self.tool(file_path=str(summary)))
        summary.write_text("Set VALUE to 1; verify the exact new file contents.\n", encoding="utf-8")
        checkpoint = self.engine.checkpoint(self.run_id, self.session, summary)
        self.assertEqual(checkpoint["status"], "ready_check")
        self.snapshot_id = checkpoint["candidate"]["snapshot_id"]
        checked = self.engine.check(self.run_id, self.session)
        self.assertEqual(checked["status"], "ready_review")
        self.assertEqual(checked["validation"]["status"], "passed")
        # DevFlow does not stop the edit (D1); the post-check snapshot protects the reviewed code.
        self.assertIsNone(self.tool(file_path=str(self.source)))

    def observed(self, number):
        process = self.run / "review-processes" / f"{number:02}"
        return process, read_json(process / "workspace" / "seen.json")

    def test_two_real_subprocess_adapters_complete_exact_snapshot(self):
        self.prepare()
        first = self.engine.review(self.run_id, self.session)
        self.assertEqual(first["status"], "ready_review")
        self.assertEqual(first["review_count"], 1)
        self.assertEqual(first["next_reviewer"], "opencode")
        final = self.engine.review(self.run_id, self.session)
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["review_count"], 2)
        self.assertTrue(final["report"]["all_reviewers_approved"])
        self.assertTrue(final["report"]["final_workspace_verified"])
        self.assertEqual(set(final["approvals"]), {"codex", "opencode"})
        self.assertEqual(set(final["approvals"].values()), {self.snapshot_id})

        codex_dir, codex = self.observed(1)
        open_dir, opencode = self.observed(2)
        self.assertEqual(codex["args"][codex["args"].index("--model") + 1], "gpt-6.1-sol")
        self.assertIn('model_reasoning_effort="xhigh"', codex["args"])
        self.assertEqual(codex["input_source"], "stdin")
        self.assertEqual(codex["args"][codex["args"].index("--sandbox") + 1], "read-only")
        self.assertIn("--ignore-user-config", codex["args"])
        self.assertEqual(opencode["args"][opencode["args"].index("--model") + 1], "opencode-go/deepseek-v4.1-flash#high")
        self.assertIn("--standalone", opencode["args"])
        self.assertEqual(Path(opencode["input_source"]), open_dir / "workspace" / "review-input.md")
        self.assertEqual(Path(opencode["opencode_config_dir"]), open_dir / "workspace")
        self.assertEqual(codex["previous_review_count"], 0)
        self.assertEqual(opencode["previous_review_count"], 1)
        runtime = read_json(open_dir / "workspace" / "opencode.json")
        # Everything is denied except, by default, the web tools (allowed last).
        self.assertEqual(runtime["permissions"], [{"action": "*", "resource": "*", "effect": "deny"},
                                                  {"action": "webfetch", "resource": "*", "effect": "allow"},
                                                  {"action": "websearch", "resource": "*", "effect": "allow"}])
        agent = opencode["args"][opencode["args"].index("--agent") + 1]
        self.assertIn(agent, runtime["agents"])

        for number, process, seen in ((1, codex_dir, codex), (2, open_dir, opencode)):
            with self.subTest(reviewer=seen["kind"]):
                packet = self.run / "reviews" / f"{number:02}" / "packet.json"
                invocation = read_json(process / "invocation.json")
                accepted = read_json(self.run / "reviews" / f"{number:02}" / "review.json")
                self.assertEqual(Path(seen["cwd"]), process / "workspace")
                self.assertNotEqual(Path(seen["cwd"]), self.project)
                self.assertNotIn("value.py", seen["files_at_start"])
                self.assertEqual(seen["snapshot_id"], self.snapshot_id)
                self.assertEqual(accepted["snapshot_id"], self.snapshot_id)
                self.assertEqual(seen["plan"], self.plan.read_text(encoding="utf-8"))
                self.assertEqual(seen["validation_status"], "passed")
                self.assertEqual(seen["check_status"], "passed")
                self.assertEqual(seen["packet_sha256"], digest(packet.read_bytes()))
                self.assertEqual(invocation["packet_sha256"], seen["packet_sha256"])
                self.assertFalse(invocation["shell"])
                self.assertFalse(invocation["effective_model_attested"])
                self.assertEqual(invocation["kind"], seen["kind"])
                # The reviewer instructions appear once, ahead of the schema and the packet.
                prompt = (process / "workspace" / "review-input.md").read_text(encoding="utf-8")
                instructions = WORKFLOW.resource("reviewer.md").read_text(encoding="utf-8")
                self.assertTrue(prompt.startswith(instructions))
                self.assertEqual(prompt.count(instructions), 1)
                self.assertTrue(prompt.endswith(packet.read_text(encoding="utf-8")))
                self.assertTrue((process / "stdout.jsonl").is_file())
                self.assertTrue((process / "stderr.log").is_file())
        self.assertEqual(read_json(codex_dir / "final-response.json")["snapshot_id"], self.snapshot_id)
        self.assertEqual(read_json(self.run / final["report_path"]), final["report"])
        self.assertEqual(self.source.read_text(encoding="utf-8"), "VALUE = 1\n")

    def test_malformed_final_file_consumes_reserved_attempt_and_stops(self):
        self.prepare("bad-final")
        result = self.engine.review(self.run_id, self.session)
        self.assertEqual(result["status"], "paused_error")
        self.assertEqual(result["review_count"], 1)
        self.assertEqual(result["remaining_reviews"], 5)
        self.assertEqual(result["next_reviewer"], "opencode")
        self.assertEqual(result["attempts"][0]["status"], "failed")
        self.assertEqual(result["approvals"], {})
        self.assertIn("Invalid reviewer JSON", result["last_error"])
        process, seen = self.observed(1)
        self.assertEqual(seen["snapshot_id"], self.snapshot_id)
        self.assertEqual((process / "final-response.json").read_text(encoding="utf-8"), '{"schema_version":')
        lines = [json.loads(line) for line in (process / "stdout.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(lines[-1]["type"], "turn.completed")
        self.assertFalse((self.run / "reviews" / "01" / "review.json").exists())
        self.assertFalse((self.run / "review-processes" / "02").exists())
        with self.assertRaises(DevFlowError):
            self.engine.review(self.run_id, self.session)
        stored = read_json(self.run / "state.json")
        self.assertEqual(len(stored["attempts"]), 1)
        self.assertEqual(stored["status"], "paused_error")
        self.assertFalse((self.run / "review-processes" / "02").exists())


if __name__ == "__main__":
    unittest.main()
