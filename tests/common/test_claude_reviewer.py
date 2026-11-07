"""Native Claude fixtures, strict result selection and four-field token counts."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from devflow_core.common import reviewers
from devflow_core.common.errors import ReviewerError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "claude_2_1_319"


class ClaudeTests(unittest.TestCase):
    def setUp(self):
        self.raw = (FIXTURES / "child-result.jsonl").read_bytes()
        self.result = json.loads(self.raw)
        self.session = self.result["session_id"]
        self.init = {"type": "system", "subtype": "init", "session_id": self.session}

    def parse(self, *events):
        return reviewers.parse_claude_events(
            event if isinstance(event, bytes) else json.dumps(event).encode() for event in events)

    def test_real_result_and_late_init(self):
        expected = self.result["structured_output"]
        for events in ((self.raw,), (self.init, self.raw), (self.raw, self.init), (self.init, self.init, self.raw)):
            with self.subTest(events=len(events)):
                self.assertEqual(self.parse(*events)[0], expected)

    def test_child_results_are_not_answers_and_unknown_events_only_count(self):
        child = {**self.result, "session_id": "other", "is_error": True}
        answer, activity = self.parse(child, {"type": "future-event"}, self.raw, self.init)
        self.assertEqual(answer, self.result["structured_output"])
        self.assertEqual(activity["other_events"]["future-event"], 1)
        with self.assertRaisesRegex(ReviewerError, "without a main result"):
            self.parse({**self.result, "parent_tool_use_id": "child-tool"})

    def test_any_main_failure_wins_over_a_later_success(self):
        for failure in ({**self.result, "is_error": True}, {**self.result, "subtype": "error_max_structured_output_retries"}):
            with self.assertRaisesRegex(ReviewerError, "failed main result"):
                self.parse(self.init, failure, self.raw)
        last = {**self.result, "structured_output": {"last": True}}
        self.assertEqual(self.parse(self.raw, last)[0], {"last": True})

    def test_conflicting_init_or_invalid_main_controls_fail(self):
        with self.assertRaisesRegex(ReviewerError, "multiple main init"):
            self.parse(self.init, {**self.init, "session_id": "different"}, self.raw)
        for field, value in (("session_id", 42), ("subtype", None), ("is_error", 1),
                             ("structured_output", "{}")):
            with self.subTest(field=field), self.assertRaises(ReviewerError):
                self.parse(self.init, {**self.result, field: value})

    def test_invalid_or_missing_usage_only_makes_tokens_unknown(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "stdout.jsonl"
            for field in ("usage", "modelUsage"):
                for value in ([], None, False, "unknown", 12):
                    with self.subTest(field=field, value=value):
                        result = {**self.result, field: value}
                        self.assertEqual(self.parse(self.init, result)[0], self.result["structured_output"])
                        output.write_text(json.dumps(result), encoding="utf-8")
                        self.assertIsNone(reviewers.Claude().tokens(output))
                result = {key: value for key, value in self.result.items() if key != field}
                self.assertEqual(self.parse(self.init, result)[0], self.result["structured_output"])
                output.write_text(json.dumps(result), encoding="utf-8")
                self.assertIsNone(reviewers.Claude().tokens(output))

    def test_child_result_and_init_are_counted_without_affecting_main_answer(self):
        child = {**self.result, "session_id": "child", "parent_tool_use_id": "child-tool", "is_error": True}
        child_init = {"type": "system", "subtype": "init", "session_id": "child", "parent_tool_use_id": "child-tool"}
        answer, activity = self.parse(child_init, child, self.init, self.raw)
        self.assertEqual(answer, self.result["structured_output"])
        self.assertEqual(activity["other_events"].get("result in a subagent"), 1)
        self.assertEqual(activity["other_events"].get("init in a subagent"), 1)

    def test_claude_preflight_uses_root_help_and_gates_reviewer_version(self):
        config = {"id": "claude", "kind": "claude", "command": ["claude"], "model": "opus",
                  "effort": "high", "timeout_seconds": 60}
        help_text = " ".join(reviewers.Claude().flags()).encode()
        with patch.object(reviewers, "_resolve", return_value=["/native/claude"]), \
                patch.object(reviewers, "_capture", side_effect=[(0, b"2.1.319 (Claude Code)"), (0, help_text)]) as capture:
            self.assertEqual(reviewers.preflight(config)["kind"], "claude")
        self.assertEqual([call.args[0] for call in capture.call_args_list],
                         [["/native/claude", "--version"], ["/native/claude", "--help"]])
        with patch.object(reviewers, "_resolve", return_value=["/native/claude"]), \
                patch.object(reviewers, "_capture", return_value=(0, b"2.1.318 (Claude Code)")) as capture:
            with self.assertRaisesRegex(ReviewerError, "2.1.319"):
                reviewers.preflight(config)
        self.assertEqual(capture.call_count, 1)
        for version in (b"2.1.319 (Claude Code)", b"2.1.320 (Claude Code)"):
            with self.subTest(version=version), \
                    patch.object(reviewers, "_resolve", return_value=["/native/claude"]), \
                    patch.object(reviewers, "_capture", side_effect=[(0, version), (0, help_text.replace(b"--verbose", b""))]):
                with self.assertRaisesRegex(ReviewerError, "lacks flags.*--verbose"):
                    reviewers.preflight(config)

    def test_raw_main_result_is_strict_even_in_unknown_fields(self):
        malformed = (self.raw.replace(b'"type":"result"', b'"type":"result","type":"result"'),
                     self.raw.replace(b'{', b'{"future":NaN,', 1),
                     self.raw.replace(b'{', b'{"future":"\\ud800",', 1),
                     self.raw.replace(b'{', b'{"future":"\xff",', 1))
        for raw in malformed:
            with self.subTest(raw=raw[:50]), self.assertRaises(ReviewerError):
                self.parse(self.init, raw)

    def test_native_structured_output_tool_is_counted(self):
        _, activity = self.parse({"type": "assistant", "session_id": self.session, "message": {
            "content": [{"type": "tool_use", "id": "t", "name": "StructuredOutput"}]}}, self.raw)
        self.assertEqual(activity["tool_calls"], {"StructuredOutput": 1})

    def test_unknown_assistant_blocks_are_counted_without_changing_answer_or_tools(self):
        tool = {'type':'tool_use', 'id':'read-1', 'name':'Read'}
        answer, activity = self.parse({'type':'assistant', 'session_id':self.session, 'message':{
            'content': [
                {'type':'text', 'text':'Visible text'},
                {'type':'thinking', 'thinking':'Known reasoning block'},
                {'type':'redacted_thinking', 'data':'redacted'},
                {'type':'future_block', 'text':'first'},
                {'type':'future_block', 'text':'second'},
                {'type':['not-a-name']}, 42, {'type':'\ud800'}, tool, tool,
            ]}}, self.raw)
        self.assertEqual(answer, self.result['structured_output'])
        self.assertEqual(activity['tool_call_count'], 1)
        self.assertEqual(activity['tool_calls'], {'Read':1})
        self.assertEqual(activity['other_events'], {'future_block':2, '(unnamed)':3})

    def test_token_source_is_model_usage_with_all_four_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "stdout.jsonl"
            output.write_bytes(self.raw)
            cli = reviewers.Claude()
            native = next(iter(self.result["modelUsage"].values()))
            self.assertEqual(cli.tokens(output), {"input_tokens": native["inputTokens"],
                "output_tokens": native["outputTokens"], "cache_creation_input_tokens": native["cacheCreationInputTokens"],
                "cache_read_input_tokens": native["cacheReadInputTokens"]})
            for field in ("inputTokens", "outputTokens", "cacheCreationInputTokens", "cacheReadInputTokens"):
                for invalid in (None, True, -1):
                    value = copy.deepcopy(self.result)
                    next(iter(value["modelUsage"].values()))[field] = invalid
                    output.write_text(json.dumps(value))
                    self.assertIsNone(cli.tokens(output))
            output.write_bytes(self.raw + self.raw)
            self.assertIsNone(cli.tokens(output))

    def test_usage_object_is_metadata_and_model_usage_is_the_only_numeric_source(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'stdout.jsonl'
            output.write_bytes(self.raw)
            expected = reviewers.Claude().tokens(output)
            self.assertIsNotNone(expected)
            for usage in ({}, {'input_tokens':1}, {'input_tokens':-1, 'output_tokens':'unused'}):
                with self.subTest(usage=usage):
                    result = {**self.result, 'usage':usage}
                    self.assertEqual(self.parse(result)[0], self.result['structured_output'])
                    output.write_text(json.dumps(result), encoding='utf-8')
                    self.assertEqual(reviewers.Claude().tokens(output), expected)

    def test_schema_is_minified_ascii_and_limited_separately_from_stdin(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            schema, instructions = root / "schema.json", root / "reviewer.md"
            schema.write_text('{"description": "日本語", "type": "object"}', encoding="utf-8")
            instructions.write_text('Review this.')
            config = {"id": "claude", "kind": "claude", "command": ["claude"], "model": "opus",
                      "effort": "high", "timeout_seconds": 60}
            prepared = reviewers.prepare_review(config, b"packet", root / "process", schema, instructions, cwd=root, probe={})
            self.assertIn("--verbose", prepared.argv)
            self.assertEqual(prepared.argv[prepared.argv.index("--json-schema") + 1],
                             '{"description":"\\u65e5\\u672c\\u8a9e","type":"object"}')
            self.assertEqual(list(root.iterdir()), [schema, instructions])
            with patch.object(reviewers, "CLAUDE_INPUT_BYTES", 1):
                with self.assertRaisesRegex(ReviewerError, "10,000,000"):
                    reviewers.prepare_review(config, b"packet", root / "process", schema, instructions, cwd=root, probe={})
            schema.write_text(json.dumps({"description": "x" * 8192}))
            with self.assertRaisesRegex(ReviewerError, "8 KiB"):
                reviewers.prepare_review(config, b"packet", root / "process", schema, instructions, cwd=root, probe={})

    def test_windows_argv_counts_quoted_utf16_units_including_terminator(self):
        reviewers._check_argv_length(['claude', 'x'*32759], windows=True)
        for value in ('x'*32760, '\U0001f600'*17000):
            with self.assertRaisesRegex(ReviewerError, '32,767'):
                reviewers._check_argv_length(['claude',value], windows=True)
