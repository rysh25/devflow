import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "plugins" / "devflow" / "scripts"
from devflow_core.plan.transcript import CaptureError, capture_interview, transcript_marker


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

    def test_original_prompt_may_be_absent_and_unplaced_prompts_use_the_hook_records(self):
        marker = transcript_marker(str(self.path))
        self.write(entry("assistant", "Question"), entry("user", "answer"))
        out = capture_interview(marker, [self.prompt("/devflow:plan request"), self.prompt("answer")])
        self.assertEqual(out["messages"][0]["content"], "/devflow:plan request")
        self.assertEqual(out["coverage"]["mode"], "bounded-transcript-with-hook-provenance")
        # A follow-up the transcript does not show: the hook records alone, in order.
        out = capture_interview(marker, [self.prompt("req"), self.prompt("missing answer")])
        self.assertEqual(out["coverage"]["mode"], "hook-records-only")
        # The transcript's own human text (no hook record) is kept at the end, marked.
        self.assertEqual([m["content"] for m in out["messages"]], ["req", "missing answer", "answer"])
        self.assertIs(out["messages"][-1]["source"]["hook_record"], False)
        self.assertEqual(out["coverage"]["human_response_count"], 1)

    def test_unknown_visible_block_is_skipped_and_counted(self):
        marker = transcript_marker(str(self.path))
        self.write(entry("user", "req"),
                   entry("assistant", [{"type": "future-visible-block", "text": "FUTURE-BLOCK"},
                                       {"type": "text", "text": "Q?"}]),
                   entry("user", "A"))
        out = capture_interview(marker, [self.prompt("req"), self.prompt("A")])
        self.assertEqual([m["content"] for m in out["messages"]], ["req", "Q?", "A"])
        self.assertEqual(out["coverage"]["unknown_types"]["blocks"], {"future-visible-block": 1})
        self.assertNotIn("FUTURE-BLOCK", json.dumps(out))

    def test_user_text_without_a_hook_record_is_kept_and_marked(self):
        marker = transcript_marker(str(self.path))
        self.write(entry("user", "req"), entry("user", "text without a hook record"), entry("assistant", "Q?"))
        out = capture_interview(marker, [self.prompt("req")])
        kept = [m for m in out["messages"] if m["content"] == "text without a hook record"]
        self.assertEqual(len(kept), 1)
        self.assertIs(kept[0]["source"]["hook_record"], False)
        self.assertEqual(out["coverage"]["user_texts_without_hook_record"], 1)

    def test_empty_answer_never_counts_as_human_participation(self):
        marker = transcript_marker(str(self.path))
        q = {"questions": [{"question": "Q?"}]}
        self.write(entry("user", "req"), entry("assistant", [{"type": "tool_use", "id": "q", "name": "AskUserQuestion", "input": q}]))
        out = capture_interview(marker, [self.prompt("req"), {
            "hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion", "tool_input": q, "tool_response": {"answers": {}},
        }])
        # Kept unanswered and counted, never a stop.
        self.assertEqual([m["source"]["kind"] for m in out["messages"]], ["prompt", "questionnaire"])
        coverage = out["coverage"]
        self.assertEqual((coverage["unanswered_questions"], coverage["human_response_count"]), (1, 0))
        self.assertEqual(coverage["unknown_types"]["hooks"], {"PostToolUse AskUserQuestion (no answer this version reads)": 1})

    def test_prefilled_question_is_kept_but_never_counts_as_human_participation(self):
        marker = transcript_marker(str(self.path))
        question = {"questions": [{"question": "Q?"}], "answers": {"Q?": "Prefilled by model"}}
        self.write(entry("user", "req"), entry("assistant", [{"type": "tool_use", "id": "q", "name": "AskUserQuestion", "input": question}]))
        out = capture_interview(marker, [self.prompt("req"), {
            "hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion", "tool_input": question,
            "tool_response": {"answers": {"Q?": "Prefilled by model"}},
        }])
        kinds = [m["source"].get("kind") for m in out["messages"]]
        self.assertEqual(kinds, ["prompt", "questionnaire_prefilled", "questionnaire_prefilled_answer"])
        self.assertEqual(out["coverage"]["prefilled_questions"], 1)
        self.assertEqual(out["coverage"]["answered_questions"], 0)
        self.assertEqual(out["coverage"]["human_response_count"], 0)

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
        out = capture_interview(marker, [self.prompt("req"), {
            "hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion", "tool_use_id": "q",
            "tool_input": {**question, "answers": {"Q?": "Yes"}}, "tool_response": {"answers": {"Q?": "Yes"}},
        }])
        self.assertEqual(out["coverage"]["prefilled_questions"], 1)
        self.assertEqual(out["coverage"]["human_response_count"], 0)

    def test_unsuccessful_question_hook_never_replaces_valid_answer(self):
        marker = transcript_marker(str(self.path))
        question = {"questions": [{"question": "Q?"}]}
        self.write(entry("user", "req"), entry("assistant", [{"type": "tool_use", "id": "q", "name": "AskUserQuestion", "input": question}]),
                   entry("user", [{"type": "tool_result", "tool_use_id": "q", "content": "Transcript answer"}]))
        out = capture_interview(marker, [self.prompt("req"), {
            "hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion", "tool_input": question,
            "tool_response": {"answers": {"Q?": "Changed answer"}}, "is_error": True,
        }])
        self.assertIn("Transcript answer", [m["content"] for m in out["messages"]])
        self.assertNotIn("Changed answer", json.dumps(out))

    def test_unanswered_question_is_kept_and_a_question_in_another_form_is_counted(self):
        marker = transcript_marker(str(self.path))
        self.write(entry("user", "request"),
                   entry("assistant", [{"type": "tool_use", "id": "q", "name": "AskUserQuestion",
                                        "input": {"questions": [{"question": "Q?"}]}}]))
        out = capture_interview(marker, [self.prompt("request")])
        self.assertEqual(out["coverage"]["unanswered_questions"], 1)
        self.assertEqual(out["coverage"]["answered_questions"], 0)
        self.path.unlink()
        marker = transcript_marker(str(self.path))
        self.write(entry("user", "request"),
                   entry("assistant", [{"type": "tool_use", "id": "q", "name": "AskUserQuestion", "input": {"questions": []}}]))
        out = capture_interview(marker, [self.prompt("request")])
        self.assertEqual(out["coverage"]["unknown_types"]["blocks"], {"AskUserQuestion (input in another form)": 1})
        self.assertEqual(out["coverage"]["unanswered_questions"], 0)

    def test_forms_this_version_does_not_read_are_counted_never_a_stop(self):
        """R4: a future Claude Code may change how records, content and hook inputs look;
        only a transcript shown to be broken stops the capture."""
        marker = transcript_marker(str(self.path))
        self.write(entry("user", "request"), ["a", "line", "that", "is", "an", "array"],
                   {"type": "user", "message": "message as a string"},
                   {"type": "assistant", "message": {"role": "user", "content": "role and type differ"}},
                   {"type": "assistant", "message": {"role": "assistant", "content": {"text": "an object"}}},
                   entry("assistant", ["a bare string block", {"type": "text", "text": 7},
                                       {"type": "text", "text": "Question?"}]),
                   {"type": "attachment", "attachment": {"type": "queued_command", "commandMode": "prompt",
                                                         "origin": {"kind": "human"}, "prompt": {"text": "x"}}},
                   dict(entry("user", "Answer"), uuid=12345))
        hook_in_another_form = {"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion",
                                "tool_use_id": "q", "tool_input": "not an object", "tool_response": {}}
        out = capture_interview(marker, [self.prompt("request"), hook_in_another_form, self.prompt("Answer")])
        self.assertEqual([m["content"] for m in out["messages"]], ["request", "Question?", "Answer"])
        unknown = out["coverage"]["unknown_types"]
        self.assertEqual(unknown["records"], {"(line in another form)": 1, "assistant (content in another form)": 1,
                                              "assistant (message in another form)": 1,
                                              "user (message in another form)": 1})
        self.assertEqual(unknown["blocks"], {"(block in another form)": 1, "text (in another form)": 1})
        self.assertEqual(unknown["attachments"], {"queued_command (prompt in another form)": 1})
        self.assertEqual(unknown["hooks"], {"PostToolUse AskUserQuestion (in another form)": 1})
        self.assertEqual(out["coverage"]["human_response_count"], 1)

    def question_turn(self, rendering):
        """A request and one AskUserQuestion whose result Claude Code rendered as ``rendering``."""
        question = {"questions": [{"question": "Q?"}]}
        self.write(entry("user", "request"),
                   entry("assistant", [{"type": "tool_use", "id": "q1", "name": "AskUserQuestion", "input": question}]),
                   entry("user", [{"type": "tool_result", "tool_use_id": "q1", "content": rendering}]))
        pre = {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion", "tool_use_id": "q1", "tool_input": question}
        post = {"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion", "tool_use_id": "q1",
                "tool_input": question, "tool_response": {"answers": {"Q?": "A"}}}
        return pre, post

    def test_an_answer_rendering_this_version_does_not_read_is_counted_and_the_hook_answer_used(self):
        marker = transcript_marker(str(self.path))
        pre, post = self.question_turn([{"type": "future-block", "data": 1}])
        out = capture_interview(marker, [self.prompt("request"), pre, post])
        self.assertEqual([(m["source"]["kind"], m["source"]["origin"]) for m in out["messages"]],
                         [("prompt", "UserPromptSubmit"), ("questionnaire", "claude_transcript"),
                          ("questionnaire_answer", "PostToolUse")])
        coverage = out["coverage"]
        self.assertEqual((coverage["answered_questions"], coverage["human_response_count"]), (1, 1))
        self.assertEqual(coverage["unknown_types"]["blocks"], {"tool_result (no answer this version reads)": 1})
        # Without the hook record the question stays unanswered; still not a stop.
        out = capture_interview(marker, [self.prompt("request"), pre])
        self.assertEqual((out["coverage"]["unanswered_questions"], out["coverage"]["human_response_count"]), (1, 0))

    def test_a_hook_answer_this_version_does_not_read_is_counted_on_both_paths(self):
        marker = transcript_marker(str(self.path))
        pre, post = self.question_turn("User answered: Q?=A")
        post = dict(post, tool_response={"future-shape": True})
        expected = {"PostToolUse AskUserQuestion (no answer this version reads)": 1}
        # The transcript's own answer stays.
        out = capture_interview(marker, [self.prompt("request"), pre, post])
        self.assertEqual(out["messages"][-1]["source"]["origin"], "claude_transcript")
        self.assertEqual(out["coverage"]["unknown_types"]["hooks"], expected)
        # The hook records path takes the transcript's answer too.
        out = capture_interview(marker, [self.prompt("request"), pre, post, self.prompt("not in the transcript")])
        coverage = out["coverage"]
        self.assertEqual(coverage["mode"], "hook-records-only")
        self.assertEqual([m["source"]["kind"] for m in out["messages"]],
                         ["prompt", "questionnaire", "questionnaire_answer", "prompt"])
        answer = out["messages"][2]
        self.assertEqual((answer["content"], answer["source"]["origin"]), ("User answered: Q?=A", "claude_transcript"))
        self.assertEqual((coverage["answered_questions"], coverage["unanswered_questions"]), (1, 0))
        self.assertEqual(coverage["unknown_types"]["hooks"], expected)
        # Neither readable: kept without an answer.
        self.path.unlink()
        marker = transcript_marker(str(self.path))
        self.question_turn([{"type": "future-block"}])
        out = capture_interview(marker, [self.prompt("request"), pre, post, self.prompt("not in the transcript")])
        self.assertEqual([m["source"]["kind"] for m in out["messages"]], ["prompt", "questionnaire", "prompt"])
        self.assertEqual((out["coverage"]["answered_questions"], out["coverage"]["unanswered_questions"]), (0, 1))

    def test_the_hook_records_path_keeps_questions_only_the_transcript_shows(self):
        # The question's hooks were not recorded (e.g. they failed); the capture uses
        # the hook records for another reason, an unplaced prompt.
        marker = transcript_marker(str(self.path))
        pre, _ = self.question_turn("User answered: Q?=A")
        self.write(entry("user", "a later text without a hook record"))
        for events, kind, answered in (([self.prompt("request"), self.prompt("unplaced")], "questionnaire_unverified", 0),
                                       ([self.prompt("request"), pre, self.prompt("unplaced")], "questionnaire", 1)):
            with self.subTest(kind=kind):
                out = capture_interview(marker, events)
                self.assertEqual(out["coverage"]["mode"], "hook-records-only")
                # Kept at the end, in transcript order, marked; counted only with its PreToolUse record.
                self.assertEqual([(m["source"]["kind"], m["source"].get("hook_record")) for m in out["messages"]],
                                 [("prompt", None), ("prompt", None), (kind, False), (kind + "_answer", False),
                                  ("prompt", False)])
                self.assertEqual(out["messages"][3]["content"], "User answered: Q?=A")
                self.assertEqual(out["coverage"]["answered_questions"], answered)

    def test_a_hook_without_an_id_that_fits_several_questions_uses_the_hook_records(self):
        marker = transcript_marker(str(self.path))
        question = {"questions": [{"question": "Again?"}]}
        self.write(entry("user", "request"), entry("assistant", [
            {"type": "tool_use", "id": "q1", "name": "AskUserQuestion", "input": question},
            {"type": "tool_use", "id": "q2", "name": "AskUserQuestion", "input": question}]))
        post = {"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion", "tool_input": question,
                "tool_response": {"answers": {"Again?": "Yes"}}}
        out = capture_interview(marker, [self.prompt("request"), post])
        self.assertEqual(out["coverage"]["mode"], "hook-records-only")
        # The hook's question, and the transcript's two kept at the end.
        self.assertEqual(out["coverage"]["unverified_questions"], 3)
        self.assertEqual([m["source"].get("tool_use_id") for m in out["messages"] if m["role"] == "assistant"],
                         [None, "q1", "q2"])

    def test_names_and_ids_in_other_forms_are_read_as_missing(self):
        marker = transcript_marker(str(self.path))
        self.write(entry("user", "request"), {"type": ["a list"]}, {"type": "system", "subtype": ["a list"]},
                   {"type": "attachment", "attachment": {"type": {"an": "object"}}},
                   entry("assistant", [{"type": ["a list"]}, {"type": "text", "text": "Question?"}]),
                   entry("user", [{"type": "tool_result", "tool_use_id": ["a list"], "content": "output"}]),
                   entry("user", "Answer"))
        hook = {"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion", "tool_use_id": ["a list"],
                "tool_input": {"questions": [{"question": "Elsewhere?"}]}, "tool_response": {"answers": {"Elsewhere?": "x"}}}
        out = capture_interview(marker, [self.prompt("request"), self.prompt("Answer")])
        self.assertEqual([m["content"] for m in out["messages"]], ["request", "Question?", "Answer"])
        self.assertEqual({group: counts for group, counts in out["coverage"]["unknown_types"].items() if counts},
                         {"records": {"(untyped)": 1}, "attachments": {"(untyped)": 1}, "blocks": {"(untyped)": 1}})
        # A hook with an ID in another form cannot be placed: the hook records are used.
        out = capture_interview(marker, [self.prompt("request"), self.prompt("Answer"), hook])
        self.assertEqual(out["coverage"]["mode"], "hook-records-only")
        self.assertEqual(out["coverage"]["unverified_questions"], 1)

    def test_a_notice_with_content_in_another_form_is_skipped_and_counted(self):
        marker = transcript_marker(str(self.path))
        self.write(entry("user", "request"),
                   entry("user", [{"type": "text", "text": 7}, "a bare string", {"type": "text", "text": "NOTICE"}],
                         origin={"kind": "task-notification"}),
                   {"type": "attachment", "attachment": {"type": "queued_command", "commandMode": "prompt",
                                                         "origin": {"kind": "peer"}, "prompt": [{"type": "text", "text": None}]}},
                   entry("assistant", "Question?"))
        out = capture_interview(marker, [self.prompt("request"), self.prompt("NOTICE")])
        # The notice's readable text still takes its hook prompt.
        self.assertEqual(out["coverage"]["mode"], "bounded-transcript-with-hook-provenance")
        self.assertEqual([m["content"] for m in out["messages"]], ["request", "Question?"])
        self.assertEqual(out["coverage"]["unknown_types"]["blocks"],
                         {"text (in another form)": 2, "(block in another form)": 1})

    def test_an_origin_in_another_form_stays_uncounted_when_the_hook_records_are_used(self):
        marker = transcript_marker(str(self.path))
        self.write(entry("user", "request"), entry("assistant", "Question?"), entry("user", "Answer", origin="human"))
        # A question the transcript does not show sends the capture to the hook records.
        elsewhere = {"questions": [{"question": "Elsewhere?"}]}
        hook = {"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion", "tool_use_id": "zz",
                "tool_input": elsewhere, "tool_response": {"answers": {"Elsewhere?": "x"}}}
        out = capture_interview(marker, [self.prompt("request"), self.prompt("Answer"), hook])
        coverage = out["coverage"]
        self.assertEqual(coverage["mode"], "hook-records-only")
        answer = [m for m in out["messages"] if m["content"] == "Answer"][0]
        self.assertEqual(answer["source"]["origin_kind"], "unknown")
        self.assertEqual((coverage["unverified_questions"], coverage["human_response_count"]), (1, 0))

    def test_a_hooked_prompt_whose_origin_is_in_another_form_is_kept_but_not_counted(self):
        marker = transcript_marker(str(self.path))
        self.write(entry("user", "request"), entry("assistant", "Question?"),
                   entry("user", "Answer", origin="human"))
        out = capture_interview(marker, [self.prompt("request"), self.prompt("Answer")])
        kept = [m for m in out["messages"] if m["content"] == "Answer"][0]
        self.assertEqual((kept["source"]["origin"], kept["source"]["origin_kind"]), ("UserPromptSubmit", "unknown"))
        self.assertEqual(out["coverage"]["human_response_count"], 0)
        # Without an origin (older transcripts), the hook record is the evidence.
        self.path.unlink()
        marker = transcript_marker(str(self.path))
        self.write(entry("user", "request"), entry("assistant", "Question?"), entry("user", "Answer"))
        out = capture_interview(marker, [self.prompt("request"), self.prompt("Answer")])
        self.assertEqual(out["coverage"]["human_response_count"], 1)

    def cancelled_question(self, **record_extra):
        question = {"questions": [{"question": "Q?"}]}
        cancel = ("The user doesn't want to proceed with this tool use. To tell you how to proceed, the user said:\n"
                  "The user wants to clarify these questions.")
        self.write(entry("user", "request"),
                   entry("assistant", [{"type": "tool_use", "id": "q", "name": "AskUserQuestion", "input": question}]),
                   entry("user", [{"type": "tool_result", "tool_use_id": "q", "content": cancel, "is_error": True}],
                         **record_extra),
                   entry("assistant", "What would you like to clarify?"),
                   entry("user", "What does Q mean?"))
        return question, cancel

    def test_a_cancelled_question_only_the_transcript_shows_is_kept_on_the_hook_records_path(self):
        marker = transcript_marker(str(self.path))
        _, cancel = self.cancelled_question()
        out = capture_interview(marker, [self.prompt("request"), self.prompt("What does Q mean?"),
                                         self.prompt("unplaced")])
        self.assertEqual(out["coverage"]["mode"], "hook-records-only")
        tail = [(m["source"]["kind"], m["content"] if m["role"] == "user" else None) for m in out["messages"][3:]]
        self.assertEqual(tail, [("questionnaire_unverified", None), ("questionnaire_unanswered", cancel)])
        self.assertEqual((out["coverage"]["unanswered_questions"], out["coverage"]["answered_questions"]), (1, 0))

    def test_cancelled_question_is_kept_verbatim_but_not_counted_as_answer(self):
        marker = transcript_marker(str(self.path))
        _, cancel = self.cancelled_question(toolDenialKind="user-rejected")
        out = capture_interview(marker, [self.prompt("request"), self.prompt("What does Q mean?")])
        cancelled = [m for m in out["messages"] if m["source"].get("kind") == "questionnaire_unanswered"]
        self.assertEqual([m["content"] for m in cancelled], [cancel])
        self.assertEqual(cancelled[0]["source"]["denial_kind"], "user-rejected")
        self.assertFalse(any(m["source"].get("kind") == "questionnaire_answer" for m in out["messages"]))
        self.assertEqual(out["coverage"]["answered_questions"], 0)
        self.assertEqual(out["coverage"]["unanswered_questions"], 1)
        # Only the follow-up chat message is human evidence.
        self.assertEqual(out["coverage"]["human_response_count"], 1)

    def test_cancelled_question_cannot_gain_an_answer_from_a_hook(self):
        marker = transcript_marker(str(self.path))
        question, _ = self.cancelled_question()
        with self.assertRaisesRegex(CaptureError, "cancelled or failed"):
            capture_interview(marker, [self.prompt("request"), self.prompt("What does Q mean?"), {
                "hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion", "tool_use_id": "q",
                "tool_input": {**question, "answers": {"Q?": "Yes"}}, "tool_response": {"answers": {"Q?": "Yes"}},
            }])

    def test_session_metadata_and_harness_attachments_are_ignored(self):
        marker = transcript_marker(str(self.path))
        metadata = [{"type": "mode", "mode": "normal"}, {"type": "permission-mode", "permissionMode": "auto"},
                    {"type": "atis-latch", "atis": ""}, {"type": "ai-title", "aiTitle": "Plan"},
                    {"type": "tools-used", "tools": ["Read"]},
                    {"type": "api-request-blob", "hash": "h", "message": {"role": "user", "content": "COPY OF REQUEST"}},
                    {"type": "attachment", "attachment": {"type": "hook_success", "stdout": "{}"}},
                    {"type": "attachment", "attachment": {"type": "instructions", "files": [{"content": "PROJECT RULES"}]}}]
        self.write(entry("user", "request"), *metadata, entry("assistant", "Question?"), entry("user", "Answer"))
        out = capture_interview(marker, [self.prompt("request"), self.prompt("Answer")])
        self.assertEqual([m["content"] for m in out["messages"]], ["request", "Question?", "Answer"])
        self.assertEqual(out["coverage"]["ignored_records"], len(metadata))
        self.assertNotIn("COPY OF REQUEST", json.dumps(out))
        self.assertNotIn("PROJECT RULES", json.dumps(out))

    def test_automated_messages_and_unknown_records_are_skipped_and_counted(self):
        for extra, group, name in (
            # Queued messages that are not the human's: another session, a scheduler.
            ({"type": "attachment", "attachment": {"type": "queued_command", "commandMode": "prompt", "isMeta": True,
                                                   "origin": {"kind": "peer", "from": "elsewhere"},
                                                   "prompt": "<agent-message>OTHER-SECRET"}}, None, None),
            ({"type": "attachment", "attachment": {"type": "queued_command", "commandMode": "prompt",
                                                   "origin": {"kind": "scheduler"}, "prompt": "OTHER-SECRET"}}, None, None),
            ({"type": "attachment", "attachment": "OTHER-SECRET"}, "attachments", "(untyped)"),
            ({"type": "attachment", "attachment": {"type": "brand-new-attachment", "text": "OTHER-SECRET"}},
             "attachments", "brand-new-attachment"),
            ({"type": "brand-new-record", "message": {"role": "user", "content": "OTHER-SECRET"}},
             "records", "brand-new-record"),
            ({"type": "Odd Type!", "text": "OTHER-SECRET"}, "records", "Odd Type!"),
            ({"text": "OTHER-SECRET"}, "records", "(untyped)"),
        ):
            with self.subTest(extra=extra):
                self.path.unlink(missing_ok=True)
                marker = transcript_marker(str(self.path))
                self.write(entry("user", "request"), extra, entry("assistant", "Question?"))
                out = capture_interview(marker, [self.prompt("request")])
                self.assertEqual([m["content"] for m in out["messages"]], ["request", "Question?"])
                self.assertNotIn("OTHER-SECRET", json.dumps(out))
                self.assertEqual(out["coverage"]["ignored_records"], 1)
                # Known kinds left out of the interview are in the total only; unknown ones also by type.
                self.assertEqual(out["coverage"]["unknown_types"], {
                    key: ({name: 1} if key == group else {}) for key in ("records", "attachments", "blocks", "hooks")})

    def test_unknown_types_are_counted_with_a_bound(self):
        marker = transcript_marker(str(self.path))
        self.write(entry("user", "request"), *({"type": f"future-{index}"} for index in range(40)),
                   entry("assistant", "Question?"))
        counts = capture_interview(marker, [self.prompt("request")])["coverage"]["unknown_types"]["records"]
        self.assertEqual(len(counts), 33)
        self.assertEqual(counts["(other)"], 8)

    def test_unknown_bookkeeping_records_are_skipped_and_counted(self):
        marker = transcript_marker(str(self.path))
        self.write(entry("user", "request"),
                   {"type": "session-owner", "accountUuid": "a", "organizationUuid": "o", "sessionId": "s"},
                   {"type": "future-setting", "value": "BOOKKEEPING-COPY"},
                   {"type": "future-setting", "value": "again"},
                   entry("assistant", "Question?"), entry("user", "Answer"))
        out = capture_interview(marker, [self.prompt("request"), self.prompt("Answer")])
        self.assertEqual([m["content"] for m in out["messages"]], ["request", "Question?", "Answer"])
        self.assertEqual(out["coverage"]["unknown_types"]["records"], {"future-setting": 2})
        self.assertNotIn("BOOKKEEPING-COPY", json.dumps(out))
        # Human words cannot vanish into an unknown record: the hook records are used instead.
        self.path.unlink()
        marker = transcript_marker(str(self.path))
        self.write(entry("user", "request"), entry("assistant", "Question?"),
                   {"type": "future-prompt", "text": "Answer"})
        out = capture_interview(marker, [self.prompt("request"), self.prompt("Answer")])
        self.assertEqual(out["coverage"]["mode"], "hook-records-only")
        self.assertEqual([m["content"] for m in out["messages"]], ["request", "Answer"])

    def test_queued_reports_of_subagents_and_tasks_are_tool_output(self):
        marker = transcript_marker(str(self.path))
        self.write(entry("user", "request"),
                   entry("assistant", [{"type": "tool_use", "id": "a1", "name": "Agent",
                                        "input": {"subagent_type": "Explore", "prompt": "survey"}}]),
                   entry("user", [{"type": "tool_result", "tool_use_id": "a1", "content": "Async agent launched"}],
                         toolUseResult={"isAsync": True, "status": "async_launched", "agentId": "agent-7"}),
                   {"type": "attachment", "attachment": {
                       "type": "queued_command", "commandMode": "prompt", "isMeta": True,
                       "origin": {"kind": "peer", "from": "agent-7", "senderTaskId": "agent-7", "name": "Explore"},
                       "prompt": "<agent-message>SUBAGENT-REPORT-SECRET</agent-message>"}},
                   {"type": "attachment", "attachment": {
                       "type": "queued_command", "commandMode": "task-notification",
                       "origin": {"kind": "task-notification", "producer": "session-task"},
                       "prompt": "<task-notification>TASK-NOTICE-SECRET</task-notification>"}},
                   entry("assistant", "Which scope?"), entry("user", "Only the API."))
        out = capture_interview(marker, [self.prompt("request"), self.prompt("Only the API.")])
        self.assertEqual([m["content"] for m in out["messages"] if m["role"] == "user"], ["request", "Only the API."])
        text = json.dumps(out)
        for secret in ("SUBAGENT-REPORT-SECRET", "TASK-NOTICE-SECRET", "survey"):
            self.assertNotIn(secret, text)

    def test_messages_claude_code_sends_as_prompts_are_not_human_words(self):
        # Task notifications and subagent reports also reach UserPromptSubmit; the
        # transcript marks their origin, so their hook prompts are consumed.
        marker = transcript_marker(str(self.path))
        notice = "<task-notification>\n<task-id>a1</task-id>\n</task-notification>"
        report = '<agent-message from="a1">\n[Subagent hand-back] REPORT</agent-message>'
        self.write(entry("user", "request"), entry("assistant", "Question?"),
                   entry("user", notice, origin={"kind": "task-notification", "producer": "session-task"}),
                   {"type": "attachment", "attachment": {"type": "queued_command", "commandMode": "prompt",
                                                         "origin": {"kind": "peer", "from": "a1"}, "prompt": report}},
                   entry("user", "Answer", origin={"kind": "human"}))
        out = capture_interview(marker, [self.prompt("request"), self.prompt(notice), self.prompt(report),
                                         self.prompt("Answer")])
        self.assertEqual(out["coverage"]["mode"], "bounded-transcript-with-hook-provenance")
        self.assertEqual([m["content"] for m in out["messages"]], ["request", "Question?", "Answer"])
        self.assertEqual(out["coverage"]["human_response_count"], 1)

    def test_question_disagreement_stops_even_when_prompts_cannot_be_placed(self):
        marker = transcript_marker(str(self.path))
        asked = {"questions": [{"question": "Q1?"}]}
        self.write(entry("user", "request"),
                   entry("assistant", [{"type": "tool_use", "id": "q", "name": "AskUserQuestion", "input": asked}]))
        hook = {"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion", "tool_use_id": "q",
                "tool_input": {"questions": [{"question": "Q2?"}]}, "tool_response": {"answers": {"Q2?": "Yes"}}}
        with self.assertRaisesRegex(CaptureError, "disagree"):
            capture_interview(marker, [self.prompt("request"), self.prompt("a prompt the transcript lacks"), hook])

    def test_unknown_block_in_a_queued_human_message_is_skipped_and_counted(self):
        marker = transcript_marker(str(self.path))
        self.write(entry("user", "request"), entry("assistant", "Question?"),
                   {"type": "attachment", "attachment": {
                       "type": "queued_command", "commandMode": "prompt", "origin": {"kind": "human"},
                       "prompt": [{"type": "text", "text": "Typed while working"},
                                  {"type": "future-block", "data": "FUTURE-SECRET"}]}})
        out = capture_interview(marker, [self.prompt("request"), self.prompt("Typed while working")])
        self.assertIn("Typed while working", [m["content"] for m in out["messages"]])
        self.assertEqual(out["coverage"]["unknown_types"]["blocks"], {"future-block": 1})
        self.assertNotIn("FUTURE-SECRET", json.dumps(out))

    def test_question_hooks_from_a_subagent_are_not_part_of_the_interview(self):
        question = {"questions": [{"question": "Sub?"}]}
        subagent = [{"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion", "tool_use_id": "s",
                     "agent_id": "a1", "tool_input": question},
                    {"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion", "tool_use_id": "s",
                     "agent_id": "a1", "tool_input": question, "tool_response": {"answers": {"Sub?": "SUB-ANSWER"}}}]
        marker = transcript_marker(str(self.path))
        self.write(entry("user", "request"), entry("assistant", "Question?"), entry("user", "Answer"))
        out = capture_interview(marker, [self.prompt("request"), *subagent, self.prompt("Answer")])
        self.assertEqual(out["coverage"]["mode"], "bounded-transcript-with-hook-provenance")
        self.assertNotIn("SUB-ANSWER", json.dumps(out))
        # The hook records alone (a prompt the transcript lacks) do not count it either.
        out = capture_interview(marker, [self.prompt("request"), *subagent, self.prompt("missing")])
        self.assertEqual(out["coverage"]["mode"], "hook-records-only")
        self.assertNotIn("SUB-ANSWER", json.dumps(out))
        self.assertEqual(out["coverage"]["answered_questions"], 0)

    def test_a_prefilled_question_has_the_same_kind_in_the_hook_records_only_path(self):
        marker = transcript_marker(str(self.path))
        question = {"questions": [{"question": "Q?"}], "answers": {"Q?": "Model"}}
        self.write(entry("user", "request"), entry("assistant", "Question?"))
        events = [self.prompt("request"),
                  {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion", "tool_use_id": "q", "tool_input": question},
                  {"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion", "tool_use_id": "q",
                   "tool_input": question, "tool_response": {"answers": {"Q?": "Model"}}},
                  self.prompt("a prompt the transcript lacks")]
        out = capture_interview(marker, events)
        self.assertEqual(out["coverage"]["mode"], "hook-records-only")
        kinds = [m["source"]["kind"] for m in out["messages"]]
        self.assertEqual(kinds, ["prompt", "questionnaire_prefilled", "questionnaire_prefilled_answer", "prompt"])

    def test_a_question_without_its_pre_tool_use_record_is_kept_as_unverified(self):
        """Only the PreToolUse record shows whether the model filled in the answers; without it
        the answer is neither a human answer nor a prefilled one."""
        marker = transcript_marker(str(self.path))
        question = {"questions": [{"question": "Q?"}]}
        self.write(entry("user", "request"), entry("assistant", "Question?"))
        events = [self.prompt("request"),
                  {"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion", "tool_use_id": "q",
                   "tool_input": question, "tool_response": {"answers": {"Q?": "Human?"}}},
                  self.prompt("a prompt the transcript lacks")]
        out = capture_interview(marker, events)
        self.assertEqual(out["coverage"]["mode"], "hook-records-only")
        kinds = [m["source"]["kind"] for m in out["messages"]]
        self.assertEqual(kinds, ["prompt", "questionnaire_unverified", "questionnaire_unverified_answer", "prompt"])
        coverage = out["coverage"]
        self.assertEqual((coverage["answered_questions"], coverage["prefilled_questions"],
                          coverage["unverified_questions"]), (0, 0, 1))
        self.assertEqual(coverage["human_response_count"], 1)  # Only the hooked follow-up prompt.
        self.assertIn("unverified", coverage["reason"])
        # The transcript path reads the question as Claude sent it, so it never needs the hook.
        main = capture_interview(marker, [self.prompt("request")])["coverage"]
        self.assertEqual(main["unverified_questions"], 0)

    def test_a_human_message_in_several_text_blocks_counts_once_on_the_hook_records_path(self):
        marker = transcript_marker(str(self.path))
        self.write(entry("user", "request"), entry("assistant", "Question?"),
                   entry("user", [{"type": "text", "text": "part one"}, {"type": "text", "text": "part two"}],
                         origin={"kind": "human"}))
        out = capture_interview(marker, [self.prompt("request"), self.prompt("a prompt the transcript lacks")])
        self.assertEqual(out["coverage"]["mode"], "hook-records-only")
        self.assertEqual([m["content"] for m in out["messages"]][-2:], ["part one", "part two"])
        # The extra hooked prompt and this one message (not one per block).
        self.assertEqual(out["coverage"]["human_response_count"], 2)

    def test_an_origin_in_a_form_this_version_does_not_know_is_not_human(self):
        marker = transcript_marker(str(self.path))
        self.write(entry("user", "request"), entry("assistant", "Question?"),
                   entry("user", "Text with a string origin", origin="human"))
        out = capture_interview(marker, [self.prompt("request")])
        kept = [m for m in out["messages"] if m["content"] == "Text with a string origin"]
        self.assertIs(kept[0]["source"]["hook_record"], False)
        self.assertNotIn("origin_kind", kept[0]["source"])
        self.assertEqual(out["coverage"]["human_response_count"], 0)

    def test_a_queued_message_from_claude_code_in_block_form_is_not_human_words(self):
        marker = transcript_marker(str(self.path))
        report = "[Subagent hand-back] REPORT"
        self.write(entry("user", "request"), entry("assistant", "Question?"),
                   {"type": "attachment", "attachment": {"type": "queued_command", "commandMode": "prompt",
                                                         "origin": {"kind": "peer", "from": "a1"},
                                                         "prompt": [{"type": "text", "text": report}]}},
                   entry("user", "Answer"))
        out = capture_interview(marker, [self.prompt("request"), self.prompt(report), self.prompt("Answer")])
        self.assertEqual(out["coverage"]["mode"], "bounded-transcript-with-hook-provenance")
        self.assertEqual([m["content"] for m in out["messages"]], ["request", "Question?", "Answer"])

    def test_a_human_reply_with_the_same_text_as_a_notice_is_kept_when_using_the_hook_records(self):
        marker = transcript_marker(str(self.path))
        self.write(entry("user", "request"), entry("assistant", "Question?"),
                   entry("user", "Done", origin={"kind": "task-notification"}, promptId="p-notice"))
        prompts = [self.prompt("request"), dict(self.prompt("Done"), prompt_id="p-notice"),
                   dict(self.prompt("Done"), prompt_id="p-human"), self.prompt("a prompt the transcript lacks")]
        out = capture_interview(marker, prompts)
        self.assertEqual(out["coverage"]["mode"], "hook-records-only")
        self.assertEqual([m["content"] for m in out["messages"]], ["request", "Done", "a prompt the transcript lacks"])
        self.assertEqual(out["messages"][1]["source"]["hook_event_index"], 2)

    def test_an_exchange_whose_questions_are_only_in_unknown_blocks_keeps_the_human_words(self):
        marker = transcript_marker(str(self.path))
        self.write(entry("user", "request"), entry("assistant", [{"type": "future-block", "text": "Question?"}]),
                   entry("user", "Answer"), entry("user", "Visible text without a hook record"))
        out = capture_interview(marker, [self.prompt("request"), self.prompt("Answer")])
        self.assertEqual(out["coverage"]["mode"], "bounded-transcript-with-hook-provenance")
        self.assertEqual([m["content"] for m in out["messages"]],
                         ["request", "Answer", "Visible text without a hook record"])
        self.assertEqual(out["coverage"]["assistant_messages"], 0)
        self.assertIn("No message of Claude's", out["coverage"]["reason"])
        self.assertEqual(out["coverage"]["unknown_types"]["blocks"], {"future-block": 1})
        self.assertEqual(out["coverage"]["user_texts_without_hook_record"], 1)

    def test_a_notice_with_a_prompt_id_consumes_only_that_hook_prompt(self):
        marker = transcript_marker(str(self.path))
        self.write(entry("user", "request"), entry("assistant", "Question?"),
                   entry("user", "Done", origin={"kind": "task-notification"}, promptId="p-notice"),
                   entry("user", "Done", origin={"kind": "human"}))
        # The notice's own hook prompt is missing; the human's "Done" must not be taken for it.
        out = capture_interview(marker, [self.prompt("request"), dict(self.prompt("Done"), prompt_id="p-human")])
        self.assertEqual([m["content"] for m in out["messages"]], ["request", "Question?", "Done"])
        self.assertEqual(out["messages"][-1]["source"]["origin"], "UserPromptSubmit")

    def test_the_hook_records_path_keeps_visible_human_texts_without_a_hook_record(self):
        marker = transcript_marker(str(self.path))
        self.write(entry("user", "request"), entry("assistant", "Question?"), entry("user", "Seen but not hooked"))
        out = capture_interview(marker, [self.prompt("request"), self.prompt("a prompt the transcript lacks")])
        self.assertEqual(out["coverage"]["mode"], "hook-records-only")
        self.assertEqual([m["content"] for m in out["messages"]],
                         ["request", "a prompt the transcript lacks", "Seen but not hooked"])
        self.assertIs(out["messages"][-1]["source"]["hook_record"], False)
        self.assertEqual(out["coverage"]["user_texts_without_hook_record"], 1)
        # Both paths report the same coverage fields.
        main = capture_interview(marker, [self.prompt("request")])["coverage"]
        self.assertEqual(set(out["coverage"]) - {"reason"}, set(main))

    def test_a_message_typed_while_claude_works_without_a_hook_record_is_kept_on_every_path(self):
        marker = transcript_marker(str(self.path))
        self.write(entry("user", "request"), entry("assistant", "Question?"), self.queued("Keep the existing DB"))
        out = capture_interview(marker, [self.prompt("request")])
        kept = [m for m in out["messages"] if m["content"] == "Keep the existing DB"]
        self.assertIs(kept[0]["source"]["hook_record"], False)
        self.assertEqual(out["coverage"]["user_texts_without_hook_record"], 1)
        # Claude Code marked it human, so it is a response on both paths.
        self.assertEqual(out["coverage"]["human_response_count"], 1)
        # Another prompt the transcript cannot place: the hook records path keeps it too.
        out = capture_interview(marker, [self.prompt("request"), self.prompt("a prompt the transcript lacks")])
        self.assertEqual(out["coverage"]["mode"], "hook-records-only")
        self.assertEqual(out["messages"][-1]["content"], "Keep the existing DB")
        self.assertIs(out["messages"][-1]["source"]["hook_record"], False)
        self.assertEqual(out["coverage"]["user_texts_without_hook_record"], 1)
        self.assertEqual(out["coverage"]["human_response_count"], 2)  # The extra hooked prompt, and this one.

    def test_a_notice_whose_hook_prompt_has_no_or_another_prompt_id_is_still_not_human(self):
        for hook_id in (None, "p-other"):
            with self.subTest(hook_prompt_id=hook_id):
                self.path.unlink(missing_ok=True)
                marker = transcript_marker(str(self.path))
                self.write(entry("user", "request"), entry("assistant", "Question?"),
                           entry("user", "Done", origin={"kind": "task-notification"}, promptId="p-notice"))
                notice = dict(self.prompt("Done"), **({"prompt_id": hook_id} if hook_id else {}))
                out = capture_interview(marker, [self.prompt("request"), notice])
                self.assertEqual([m["content"] for m in out["messages"]], ["request", "Question?"])
                self.assertEqual(out["coverage"]["human_response_count"], 0)

    def test_a_notice_and_a_human_with_the_same_text_and_both_hooks(self):
        marker = transcript_marker(str(self.path))
        self.write(entry("user", "request"), entry("assistant", "Question?"),
                   entry("user", "Done", origin={"kind": "task-notification"}, promptId="p-notice"),
                   entry("user", "Done", origin={"kind": "human"}, promptId="p-human"))
        out = capture_interview(marker, [self.prompt("request"), dict(self.prompt("Done"), prompt_id="p-notice"),
                                         dict(self.prompt("Done"), prompt_id="p-human")])
        self.assertEqual(out["coverage"]["mode"], "bounded-transcript-with-hook-provenance")
        self.assertEqual([m["content"] for m in out["messages"]], ["request", "Question?", "Done"])
        self.assertEqual(out["messages"][-1]["source"]["hook_event_index"], 2)
        self.assertEqual(out["coverage"]["human_response_count"], 1)

    def test_texts_without_a_hook_record_are_not_responses_on_either_path(self):
        marker = transcript_marker(str(self.path))
        self.write(entry("user", "request"), entry("assistant", "Question?"), entry("user", "Answer"),
                   entry("user", "Seen but not hooked"))
        main = capture_interview(marker, [self.prompt("request"), self.prompt("Answer")])
        fallback = capture_interview(marker, [self.prompt("request"), self.prompt("Answer"),
                                              self.prompt("a prompt the transcript lacks")])
        self.assertEqual(main["coverage"]["mode"], "bounded-transcript-with-hook-provenance")
        self.assertEqual(fallback["coverage"]["mode"], "hook-records-only")
        self.assertEqual(main["coverage"]["user_texts_without_hook_record"], 1)
        self.assertEqual(fallback["coverage"]["user_texts_without_hook_record"], 1)
        self.assertEqual(main["coverage"]["human_response_count"], 1)
        self.assertEqual(fallback["coverage"]["human_response_count"], 2)  # Its extra hooked prompt counts.

    def queued(self, prompt):
        return {"type": "attachment", "uuid": "queued-1", "attachment": {
            "type": "queued_command", "commandMode": "prompt", "origin": {"kind": "human"}, "prompt": prompt}}

    def test_message_typed_while_claude_works_is_captured(self):
        for hook_fired in (True, False):
            with self.subTest(hook_fired=hook_fired):
                self.path.unlink(missing_ok=True)
                marker = transcript_marker(str(self.path))
                self.write(entry("user", "request"), entry("assistant", "Working..."), self.queued("Use option B"),
                           self.queued("Use option B"), entry("assistant", "Switching to B."))
                prompts = [self.prompt("request")] + ([self.prompt("Use option B")] if hook_fired else [])
                out = capture_interview(marker, prompts)
                typed = [m for m in out["messages"] if m["source"].get("delivery") == "queued_while_claude_was_working"]
                self.assertEqual([m["content"] for m in typed], ["Use option B"])  # duplicate record ignored
                self.assertEqual(typed[0]["source"]["origin"], "UserPromptSubmit" if hook_fired else "claude_transcript")
                self.assertEqual(out["coverage"]["human_response_count"], 1)

    def test_queued_message_with_image_keeps_text_and_counts_image(self):
        marker = transcript_marker(str(self.path))
        prompt = [{"type": "text", "text": "Like this [Image #1]"},
                  {"type": "image", "source": {"type": "base64", "data": "SECRETPIXELS"}}]
        self.write(entry("user", "request"), entry("assistant", "Working..."), self.queued(prompt))
        out = capture_interview(marker, [self.prompt("request")])
        self.assertEqual(out["messages"][-1]["content"], "Like this [Image #1]")
        self.assertEqual(out["coverage"]["omitted_attachments"], {"image": 1})
        self.assertNotIn("SECRETPIXELS", json.dumps(out))

    def test_interrupt_markers_and_shell_commands_are_not_human_words(self):
        marker = transcript_marker(str(self.path))
        self.write(entry("user", "request"), entry("assistant", "Question?"),
                   entry("user", [{"type": "text", "text": "[Request interrupted by user]"}]),
                   entry("user", [{"type": "text", "text": "[Request interrupted by user for tool use]"}]),
                   entry("user", "<bash-input>cat .env</bash-input>"),
                   entry("user", "<bash-stdout>TOKEN=SECRET</bash-stdout><bash-stderr></bash-stderr>"),
                   entry("user", "Answer"))
        out = capture_interview(marker, [self.prompt("request"), self.prompt("Answer")])
        self.assertEqual([m["content"] for m in out["messages"]], ["request", "Question?", "Answer"])
        self.assertNotIn("SECRET", json.dumps(out))
        self.assertEqual(out["coverage"]["ignored_records"], 4)

    def test_attached_images_and_files_are_counted_not_exported(self):
        marker = transcript_marker(str(self.path))
        self.write(entry("user", "request"), entry("assistant", "Question?"),
                   entry("user", [{"type": "text", "text": "See [Image #1] and @notes.md"},
                                  {"type": "image", "source": {"type": "base64", "data": "SECRETPIXELS"}}]),
                   {"type": "attachment", "attachment": {"type": "file", "filename": "notes.md", "content": "FILE BODY"}})
        out = capture_interview(marker, [self.prompt("request"), self.prompt("See [Image #1] and @notes.md")])
        self.assertEqual(out["messages"][-1]["content"], "See [Image #1] and @notes.md")
        self.assertEqual(out["coverage"]["omitted_attachments"], {"file": 1, "image": 1})
        self.assertNotIn("SECRETPIXELS", json.dumps(out))
        self.assertNotIn("FILE BODY", json.dumps(out))

    def test_compaction_boundary_and_summary_are_skipped(self):
        marker = transcript_marker(str(self.path))
        self.write(entry("user", "request"), entry("assistant", "Question?"), entry("user", "Answer"),
                   {"type": "system", "subtype": "compact_boundary", "content": "Conversation compacted"},
                   entry("user", "This session is being continued... COMPACT-SUMMARY", isCompactSummary=True,
                         isVisibleInTranscriptOnly=True),
                   entry("assistant", "Next question?"), entry("user", "Next answer"))
        out = capture_interview(marker, [self.prompt("request"), self.prompt("Answer"), self.prompt("Next answer")])
        self.assertEqual([m["content"] for m in out["messages"]],
                         ["request", "Question?", "Answer", "Next question?", "Next answer"])
        self.assertNotIn("COMPACT-SUMMARY", json.dumps(out))
        # Skipped and counted: the boundary and the summary.
        self.assertEqual(out["coverage"]["ignored_records"], 2)
        self.assertEqual(out["coverage"]["unknown_types"], {"records": {}, "attachments": {}, "blocks": {}, "hooks": {}})

    def test_without_a_transcript_path_the_hook_records_make_the_interview(self):
        question = {"questions": [{"question": "Scope?"}]}
        events = [self.prompt("request"),
                  {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion", "tool_use_id": "q1",
                   "tool_input": question},
                  {"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion", "tool_use_id": "q1",
                   "tool_input": question, "tool_response": {"answers": {"Scope?": "API"}}},
                  self.prompt("One more thing")]
        for path in (None, "", "  "):
            with self.subTest(path=path):
                marker = transcript_marker(path)
                self.assertEqual(marker, {"path": None, "offset": 0})
                out = capture_interview(marker, events)
                self.assertEqual([m["source"]["kind"] for m in out["messages"]],
                                 ["prompt", "questionnaire", "questionnaire_answer", "prompt"])
                self.assertEqual(out["messages"][3]["content"], "One more thing")
                coverage = out["coverage"]
                self.assertEqual(coverage["mode"], "hook-records-only")
                self.assertTrue(coverage["reason"].startswith("There was no Claude Code transcript"))
                # Without a transcript a later prompt may be a notice: kept, not counted.
                self.assertEqual(out["messages"][3]["source"]["origin_kind"], "unknown")
                self.assertEqual((coverage["captured_bytes"], coverage["answered_questions"],
                                  coverage["human_response_count"]), (0, 1, 1))
                # A notice alone is no human response.
                notice = capture_interview(marker, [self.prompt("request"), self.prompt("<task-notification>x")])
                self.assertEqual(notice["coverage"]["human_response_count"], 0)

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
        # NaN and Infinity are not JSON, and a number beyond a float's range would become one.
        for value in ("NaN", "-Infinity", "1e999"):
            with self.subTest(value=value):
                self.path.unlink()
                marker = transcript_marker(str(self.path))
                self.path.write_text('{"type": "user", "n": %s}\n' % value, encoding="utf-8")
                with self.assertRaisesRegex(CaptureError, "Malformed"):
                    capture_interview(marker, [self.prompt("req")])

    def test_duplicate_uuid_exact_ignored_conflict_rejected(self):
        marker = transcript_marker(str(self.path))
        row = entry("assistant", "Question", uuid="a")
        self.write(entry("user", "req"), row, row)
        self.assertEqual(len(capture_interview(marker, [self.prompt("req")])["messages"]), 2)
        self.write(entry("assistant", "different", uuid="a"))
        with self.assertRaises(CaptureError):
            capture_interview(marker, [self.prompt("req")])

    def test_a_transcript_never_written_uses_the_hook_records(self):
        marker = transcript_marker(str(self.path))
        out = capture_interview(marker, [self.prompt("req"), {"hook_event_name": "Stop", "last_assistant_message": "Q?"}])
        self.assertEqual(out["coverage"]["mode"], "hook-records-only")
        self.assertTrue(out["coverage"]["reason"].startswith("There was no Claude Code transcript"))
        # Claude's words are not taken from the Stop hook.
        self.assertEqual([m["content"] for m in out["messages"]], ["req"])
        # A transcript that existed and then vanished is a broken record.
        self.write(entry("user", "req"))
        marker = transcript_marker(str(self.path))
        self.path.unlink()
        with self.assertRaises(CaptureError):
            capture_interview(marker, [self.prompt("req")])


if __name__ == "__main__":
    unittest.main()
