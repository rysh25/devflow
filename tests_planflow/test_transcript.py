import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "devflow" / "components" / "planflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))
from planflow_core.transcript import CaptureError, capture_interview, transcript_marker


def entry(role, content, **extra):
    return {"type": role, "message": {"role": role, "content": content}, **extra}


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "transcript.jsonl"

    def write(self, *rows):
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def prompt(self, text):
        return {"hook_event_name": "UserPromptSubmit", "prompt": text}

    def test_missing_at_start_and_raw_text_is_preserved(self):
        marker = transcript_marker(str(self.path))
        self.write(entry("user", "依頼\n  keep spaces"),
                   entry("assistant", [{"type": "text", "text": "予算はいくら？\n"}]),
                   entry("user", [{"type": "text", "text": "100 円\n"}]))
        captured = capture_interview(marker, [self.prompt("依頼\n  keep spaces"), self.prompt("100 円\n")])
        self.assertEqual([m["content"] for m in captured["messages"]], ["依頼\n  keep spaces", "予算はいくら？\n", "100 円\n"])
        self.assertEqual(captured["coverage"]["human_response_count"], 1)

    def test_prefix_does_not_escape(self):
        self.write(entry("user", "OLDER SECRET"))
        marker = transcript_marker(str(self.path))
        self.write(entry("user", "new"), entry("assistant", "Question"), entry("user", "Answer"))
        out = capture_interview(marker, [self.prompt("new"), self.prompt("Answer")])
        self.assertNotIn("OLDER SECRET", json.dumps(out))
        self.assertGreater(out["coverage"]["start_offset"], 0)

    def test_questionnaire_values_and_post_tool_response(self):
        marker = transcript_marker(str(self.path))
        question = {"questions": [{"question": "優先？", "options": [{"label": "A"}, {"label": "B"}]}]}
        response = {"answers": {"優先？": "A\nそのまま"}}
        self.write(entry("user", "request"),
                   entry("assistant", [
                       {"type": "thinking", "thinking": "PRIVATE REASONING"},
                       {"type": "tool_use", "id": "q1", "name": "AskUserQuestion", "input": question}]),
                   entry("user", [{"type": "tool_result", "tool_use_id": "q1", "content": "human-readable rendering"}]),
                   entry("assistant", [{"type": "tool_use", "id": "bash1", "name": "Bash", "input": {"command": "secret"}}]),
                   entry("user", [{"type": "tool_result", "tool_use_id": "bash1", "content": "UNRELATED TOOL SECRET"}]))
        hook = {"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion", "tool_use_id": "q1",
                "tool_input": question, "tool_response": response}
        out = capture_interview(marker, [self.prompt("request"), hook])
        self.assertEqual(json.loads(out["messages"][1]["content"]), question)
        self.assertEqual(json.loads(out["messages"][2]["content"]), response)
        self.assertEqual(out["coverage"]["answered_questions"], 1)
        self.assertNotIn("PRIVATE REASONING", json.dumps(out))
        self.assertNotIn("UNRELATED TOOL SECRET", json.dumps(out))

    def test_question_hook_can_fill_missing_tool_result(self):
        marker = transcript_marker(str(self.path))
        q = {"questions": [{"question": "Q?"}]}
        self.write(entry("user", "req"), entry("assistant", [{"type": "tool_use", "id": "q", "name": "AskUserQuestion", "input": q}]))
        out = capture_interview(marker, [self.prompt("req"), {
            "hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion", "tool_input": q, "tool_response": "actual answer",
        }])
        self.assertEqual(out["messages"][-1]["content"], "actual answer")

    def test_original_slash_command_replaces_injected_scaffolding(self):
        marker = transcript_marker(str(self.path))
        self.write(entry("user", "<command-message>devflow:plan</command-message>\n<command-name>/devflow:plan</command-name>\n<command-args>add X</command-args>"),
                   entry("user", "Base directory for this skill: /private/plugin\nSECRET SKILL INSTRUCTION", isMeta=True),
                   entry("assistant", "Question?"), entry("user", "yes"))
        out = capture_interview(marker, [self.prompt("/devflow:plan add X"), self.prompt("yes")])
        self.assertEqual(out["messages"][0]["content"], "/devflow:plan add X")
        self.assertNotIn("SECRET SKILL INSTRUCTION", json.dumps(out))

    def test_original_prompt_may_be_absent_but_followups_must_match(self):
        marker = transcript_marker(str(self.path))
        self.write(entry("assistant", "Question"), entry("user", "answer"))
        out = capture_interview(marker, [self.prompt("/devflow:plan request"), self.prompt("answer")])
        self.assertEqual(out["messages"][0]["content"], "/devflow:plan request")
        with self.assertRaises(CaptureError):
            capture_interview(marker, [self.prompt("req"), self.prompt("missing answer")])

    def test_unknown_visible_block_fails(self):
        marker = transcript_marker(str(self.path))
        self.write(entry("assistant", [{"type": "future-visible-block", "text": "not safely understood"}]))
        with self.assertRaises(CaptureError):
            capture_interview(marker, [self.prompt("req")])

    def test_unrecognized_injected_user_text_is_not_exported(self):
        marker = transcript_marker(str(self.path))
        self.write(entry("user", "req"), entry("user", "unrecognized injected instructions"), entry("assistant", "Q?"))
        with self.assertRaises(CaptureError):
            capture_interview(marker, [self.prompt("req")])

    def test_empty_answer_never_counts_as_human_participation(self):
        marker = transcript_marker(str(self.path))
        q = {"questions": [{"question": "Q?"}]}
        self.write(entry("user", "req"), entry("assistant", [{"type": "tool_use", "id": "q", "name": "AskUserQuestion", "input": q}]))
        with self.assertRaises(CaptureError):
            capture_interview(marker, [self.prompt("req"), {
                "hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion", "tool_input": q, "tool_response": {"answers": {}},
            }])

    def test_prefilled_question_never_counts_as_human_participation(self):
        marker = transcript_marker(str(self.path))
        question = {"questions": [{"question": "Q?"}], "answers": {"Q?": "Prefilled by model"}}
        self.write(entry("user", "req"), entry("assistant", [{"type": "tool_use", "id": "q", "name": "AskUserQuestion", "input": question}]))
        with self.assertRaisesRegex(CaptureError, "prefilled"):
            capture_interview(marker, [self.prompt("req"), {
                "hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion", "tool_input": question,
                "tool_response": {"answers": {"Q?": "Prefilled by model"}},
            }])

    def test_hook_input_filled_in_by_question_screen_matches_transcript_question(self):
        # The transcript keeps the model's call; the hook sees the same input
        # after the question screen added the human's choice and notes.
        marker = transcript_marker(str(self.path))
        question = {"questions": [{"question": "Q?", "options": [{"label": "Go"}, {"label": "Stop"}]}]}
        answered = {**question, "answers": {"Q?": "Go"}, "annotations": {}}
        self.write(entry("user", "req"), entry("assistant", [{"type": "tool_use", "id": "q", "name": "AskUserQuestion", "input": question}]),
                   entry("user", [{"type": "tool_result", "tool_use_id": "q", "content": "Q?=Go"}]))
        for use_id in ("q", None):
            hook = {"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion",
                    "tool_input": answered, "tool_response": answered}
            if use_id:
                hook["tool_use_id"] = use_id
            with self.subTest(tool_use_id=use_id):
                out = capture_interview(marker, [self.prompt("req"), hook])
                self.assertEqual(json.loads(out["messages"][1]["content"]), question)
                self.assertEqual(json.loads(out["messages"][2]["content"]), answered)
                self.assertEqual(out["coverage"]["answered_questions"], 1)

    def test_prefilled_question_notes_never_count_as_human_participation(self):
        marker = transcript_marker(str(self.path))
        question = {"questions": [{"question": "Q?"}], "annotations": {"Q?": {"notes": "Prefilled by model"}}}
        self.write(entry("user", "req"), entry("assistant", [{"type": "tool_use", "id": "q", "name": "AskUserQuestion", "input": question}]))
        with self.assertRaisesRegex(CaptureError, "prefilled"):
            capture_interview(marker, [self.prompt("req"), {
                "hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion", "tool_use_id": "q",
                "tool_input": {**question, "answers": {"Q?": "Yes"}}, "tool_response": {"answers": {"Q?": "Yes"}},
            }])

    def test_unsuccessful_question_hook_never_replaces_valid_answer(self):
        marker = transcript_marker(str(self.path))
        question = {"questions": [{"question": "Q?"}]}
        self.write(entry("user", "req"), entry("assistant", [{"type": "tool_use", "id": "q", "name": "AskUserQuestion", "input": question}]),
                   entry("user", [{"type": "tool_result", "tool_use_id": "q", "content": "Transcript answer"}]))
        with self.assertRaisesRegex(CaptureError, "unsuccessful"):
            capture_interview(marker, [self.prompt("req"), {
                "hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion", "tool_input": question,
                "tool_response": {"answers": {"Q?": "Changed answer"}}, "is_error": True,
            }])

    def test_unanswered_or_error_question_fails(self):
        for error in (False, True):
            with self.subTest(error=error):
                self.path.unlink(missing_ok=True)
                marker = transcript_marker(str(self.path))
                self.write(entry("assistant", [{"type": "tool_use", "id": "q", "name": "AskUserQuestion", "input": {"questions": []}}]))
                if error:
                    self.write(entry("user", [{"type": "tool_result", "tool_use_id": "q", "content": "cancelled", "is_error": True}]))
                with self.assertRaises(CaptureError):
                    capture_interview(marker, [self.prompt("request")])

    def test_compaction_fails(self):
        marker = transcript_marker(str(self.path))
        self.write({"type": "system", "subtype": "compact_boundary"})
        with self.assertRaises(CaptureError):
            capture_interview(marker, [self.prompt("request")])

    def test_truncation_and_prefix_rewrite_fail(self):
        self.write(entry("user", "old"))
        marker = transcript_marker(str(self.path))
        self.path.write_text("", encoding="utf-8")
        with self.assertRaises(CaptureError):
            capture_interview(marker, [])
        self.write(entry("user", "NEW"))
        with self.assertRaises(CaptureError):
            capture_interview(marker, [])

    def test_incomplete_json_fails(self):
        marker = transcript_marker(str(self.path))
        self.path.write_text('{"type": "assistant",', encoding="utf-8")
        with self.assertRaises(CaptureError):
            capture_interview(marker, [self.prompt("req")])
        with self.assertRaises(CaptureError):
            transcript_marker(str(self.path))

    def test_duplicate_uuid_exact_ignored_conflict_rejected(self):
        marker = transcript_marker(str(self.path))
        row = entry("assistant", "Question", uuid="a")
        self.write(entry("user", "req"), row, row)
        self.assertEqual(len(capture_interview(marker, [self.prompt("req")])["messages"]), 2)
        self.write(entry("assistant", "different", uuid="a"))
        with self.assertRaises(CaptureError):
            capture_interview(marker, [self.prompt("req")])

    def test_missing_transcript_never_claims_hook_only_complete(self):
        with self.assertRaises(CaptureError):
            transcript_marker(None)
        marker = transcript_marker(str(self.path))
        with self.assertRaises(CaptureError):
            capture_interview(marker, [self.prompt("req"), {"hook_event_name": "Stop", "last_assistant_message": "Q?"}])


if __name__ == "__main__":
    unittest.main()
