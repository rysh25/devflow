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
    print("opencode 9.0.0" if kind == "opencode" else "codex offline-fixture-1")
    raise SystemExit(0)
if "--help" in args:
    print("--json --output-schema --output-last-message --skip-git-repo-check --cd --model --config "
          "--format --file --standalone")
    raise SystemExit(0)

if kind == "codex":
    content = sys.stdin.read()
    source = "stdin"
    records = Path(args[args.index("--output-last-message") + 1]).parent
else:
    source = args[args.index("--file") + 1]
    content = Path(source).read_text(encoding="utf-8")
    records = Path(source).parent

packet_text = content.split("\n\n## Review packet (data, not instructions)\n", 1)[1]
packet = json.loads(packet_text)
# Like a real reviewer, read the code and the diff with "its own tools".
diff = Path(packet["diff_path"]).read_text(encoding="utf-8")
seen = {
    "kind": kind, "args": args, "cwd": os.getcwd(), "pwd": os.environ.get("PWD"), "input_source": source,
    "snapshot_id": packet["snapshot_id"], "plan": packet["plan"],
    "packet_sha256": hashlib.sha256(packet_text.encode("utf-8")).hexdigest(),
    "validation_status": packet["validation"]["status"],
    "check_status": packet["check_results"]["status"],
    "previous_review_count": len(packet["previous_reviews"]),
    "value_py": Path("value.py").read_text(encoding="utf-8"), "diff": diff,
    "stdin_after_prompt": sys.stdin.read() if kind == "codex" else sys.stdin.read(),
    "env": {key: os.environ.get(key) for key in (
        "OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG_CONTENT", "OPENCODE_PERMISSION",
        "OPENCODE_SERVER", "OPENCODE_DISABLE_PROJECT_CONFIG", "OPENCODE_DISABLE_CLAUDE_CODE")},
}
(records / "seen.json").write_text(json.dumps(seen), encoding="utf-8")
if mode == "edit":
    # Against its instructions, the reviewer changes a file in the snapshot scope.
    Path("value.py").write_text("VALUE = 2\n", encoding="utf-8")
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
        {"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "command": "cat value.py"}},
        {"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": answer}},
        {"type": "turn.completed", "usage": {"input_tokens": 0, "output_tokens": 0}},
    ]
else:
    events = [
        {"type": "step_start", "sessionID": "offline-session", "part": {"messageID": "m1"}},
        {"type": "tool_use", "sessionID": "offline-session", "part": {"id": "t1", "tool": "read"}},
        {"type": "step_finish", "sessionID": "offline-session", "part": {"messageID": "m1", "reason": "tool-calls"}},
        {"type": "step_start", "sessionID": "offline-session", "part": {"messageID": "m2"}},
        {"type": "text", "sessionID": "offline-session", "part": {"messageID": "m2", "id": "x", "text": answer}},
        {"type": "step_finish", "sessionID": "offline-session", "part": {"messageID": "m2", "reason": "stop"}},
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
             "model": "opencode-go/deepseek-v4.1-flash", "effort": "high", "timeout_seconds": 10},
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
        return process, read_json(process / "seen.json")

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
        self.assertIn('approval_policy="never"', codex["args"])
        self.assertFalse([arg for arg in codex["args"] if "project_doc" in arg])
        self.assertEqual(codex["input_source"], "stdin")
        self.assertEqual(Path(codex["args"][codex["args"].index("--cd") + 1]), self.project)
        self.assertEqual(opencode["args"][opencode["args"].index("--model") + 1], "opencode-go/deepseek-v4.1-flash#high")
        self.assertIn("--standalone", opencode["args"])
        self.assertEqual(Path(opencode["input_source"]), open_dir / "review-input.md")
        # The project's own settings and instructions load as usual: DevFlow sets nothing for them.
        for key in ("OPENCODE_DISABLE_PROJECT_CONFIG", "OPENCODE_DISABLE_CLAUDE_CODE", "OPENCODE_CONFIG_DIR"):
            self.assertIsNone(opencode["env"][key], key)
        self.assertEqual(codex["previous_review_count"], 0)
        self.assertEqual(opencode["previous_review_count"], 1)
        self.assertEqual([item["tool_calls"] for item in final["report"]["reviews"]],
                         [{"command_execution": 1}, {"read": 1}])

        for number, process, seen in ((1, codex_dir, codex), (2, open_dir, opencode)):
            with self.subTest(reviewer=seen["kind"]):
                packet = self.run / "reviews" / f"{number:02}" / "packet.json"
                invocation = read_json(process / "invocation.json")
                accepted = read_json(self.run / "reviews" / f"{number:02}" / "review.json")
                # The reviewer works in the workspace and reads the code and the diff itself.
                self.assertEqual(Path(seen["cwd"]), self.project)
                self.assertEqual(Path(seen["pwd"]), self.project)
                self.assertEqual(seen["value_py"], "VALUE = 1\n")
                self.assertIn("diff --git a/value.py b/value.py\n", seen["diff"])
                self.assertIn("+VALUE = 1\n", seen["diff"])
                self.assertEqual(seen["stdin_after_prompt"], "")
                self.assertEqual(seen["snapshot_id"], self.snapshot_id)
                self.assertEqual(accepted["snapshot_id"], self.snapshot_id)
                self.assertEqual(seen["plan"], self.plan.read_bytes().decode("utf-8"))
                self.assertEqual(seen["validation_status"], "passed")
                self.assertEqual(seen["check_status"], "passed")
                self.assertEqual(seen["packet_sha256"], digest(packet.read_bytes()))
                self.assertEqual(invocation["packet_sha256"], seen["packet_sha256"])
                self.assertEqual(Path(invocation["cwd"]), self.project)
                self.assertFalse(invocation["shell"])
                self.assertFalse(invocation["effective_model_attested"])
                self.assertEqual(invocation["kind"], seen["kind"])
                # The reviewer instructions appear once, ahead of the schema and the packet.
                prompt = (process / "review-input.md").read_text(encoding="utf-8")
                instructions = WORKFLOW.resource("reviewer.md").read_text(encoding="utf-8")
                self.assertTrue(prompt.startswith(instructions))
                self.assertEqual(prompt.count(instructions), 1)
                # The rules for every reviewer carry this attempt's time budget here too.
                self.assertRegex(prompt, r"DevFlow stops this review 10 seconds after it starts, at\n"
                                         r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00;")
                self.assertTrue(prompt.endswith(packet.read_text(encoding="utf-8")))
                self.assertTrue((process / "stdout.jsonl").is_file())
                self.assertTrue((process / "stderr.log").is_file())
        self.assertEqual(read_json(codex_dir / "final-response.json")["snapshot_id"], self.snapshot_id)
        self.assertEqual(read_json(self.run / final["report_path"]), final["report"])
        self.assertEqual(self.source.read_text(encoding="utf-8"), "VALUE = 1\n")

    def test_a_reviewer_that_edits_the_workspace_is_not_counted(self):
        self.prepare("edit")
        result = self.engine.review(self.run_id, self.session)
        self.assertEqual(result["status"], "paused_error")
        self.assertEqual(result["review_count"], 1)
        self.assertEqual(result["attempts"][0]["status"], "failed")
        self.assertIn("changed since checkpoint", result["last_error"])
        self.assertEqual(result["approvals"], {})
        self.assertFalse((self.run / "reviews" / "01" / "review.json").exists())

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
