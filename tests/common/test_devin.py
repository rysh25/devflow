"""Devin as a reviewer CLI and Devin's interview-skill metadata, offline.

The reviewer runs as a fake local process that answers like Devin CLI
3000.12.57: standard output joins every agent message without a separator, and
--export writes an ATIF-v1.7 record whose last agent message is the answer.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from devflow_core.common import reviewers
from devflow_core.common.errors import ReviewerError
from devflow_core.common.review import check_fields, check_verdict
from devflow_core.common.hosts.skills import DEVIN_PROJECT_SKILLS, devin_user_skill_roots
from devflow_core.common.startup import apply_startup, parse_start
from devflow_core.plan.interview import devin_frontmatter, inspect_interview_skill

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "devin_3000_12_57"
REVIEW = {"schema_version": 1, "verdict": "pass", "summary": "ok", "findings": [], "questions": []}


def accept(value):
    """A workflow's schema check: exactly the review keys."""
    if not isinstance(value, dict) or set(value) != set(REVIEW):
        raise ReviewerError("Review has missing or unknown fields")
    return value


def strict(value):
    """A workflow's checks: the exact review keys, then the cross-field verdict rules."""
    check_fields(value, set(REVIEW), "Review")
    check_verdict(value["verdict"], value["findings"], value["questions"])
    return value

FAKE_DEVIN = r'''
import json, sys, time
from pathlib import Path
args = sys.argv[1:]
mode = args.pop(0)
if args == ["--version"]:
    print("devin 3000.12.57 (96542e930752)"); sys.exit(0)
if args == ["--help"]:
    flags = ["--print", "--prompt-file", "--resume", "--model", "--export"]
    if mode == "old":
        flags.remove("--export")
    print("Usage: devin [OPTIONS]\n" + "\n".join("  " + f for f in flags)); sys.exit(0)
if mode == "untrusted":
    print("Error: Refusing to run in an untrusted workspace", file=sys.stderr); sys.exit(1)
stdin = sys.stdin.read() if not sys.stdin.closed else ""
values = dict(zip(args[1::2], args[2::2])) if args[0] == "--print" else {}
work = Path(values["--prompt-file"]).parent
resume = "--resume" in values
base = Path(sys.argv[0]).parent
with open(base / "calls.jsonl", "a", encoding="utf-8") as log:
    log.write(json.dumps({"mode": mode, "resume": resume, "argv": args}) + "\n")
(work / "seen.json").write_text(json.dumps({"argv": args, "stdin": stdin}))
record = json.loads((base / ("follow_export.json" if resume else "review_export.json")).read_text())
answer = json.dumps(%s)
prose = "Analysis of the diff.\n\n" + answer
texts = {"pass": answer, "empty": "", "prose": prose, "tidy": prose, "slow": prose,
         "exit-error": prose, "odd": prose, "not-review": json.dumps({"unexpected": True}),
         "no-session": prose,
         "missing-fields": json.dumps({"verdict": "pass", "findings": [], "questions": []}),
         "contradict": json.dumps({**json.loads(answer),
                                   "findings": [{"id": "F1", "requires_human": False}]}),
         "duplicate": '{"\\ud800": 1, "\\ud800": 2}',
         "prose-resume": "Analysis again.\n\n" + answer, "odd-resume": json.dumps({"unexpected": True}),
         "still-bad": json.dumps({"unexpected": True}), "still-bad-resume": json.dumps({"unexpected": True})}
if mode == "no-session":
    record.pop("session_id", None)
if resume and mode == "slow":
    time.sleep(30)
if mode == "fail" and not resume:
    print("fixture failure", file=sys.stderr); sys.exit(7)
if resume and mode == "exit-error":
    print("follow-up fixture failure", file=sys.stderr); sys.exit(7)
final = texts.get(mode + ("-resume" if resume else ""), answer)
agents = [step for step in record["steps"] if step["source"] == "agent"]
agents[0]["message"] = "Reading the diff."
agents[-1]["message"] = final
if mode != "no-export":
    Path(values["--export"]).write_text(json.dumps(record))
print("".join(step["message"] for step in agents))
''' % json.dumps(REVIEW)


class DevinReviewerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="devflow devin ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.project = self.base / "project"
        self.project.mkdir()
        self.fake = self.base / "fake devin.py"
        self.fake.write_text(FAKE_DEVIN, encoding="utf-8")
        (self.base / "review_export.json").write_bytes((FIXTURES / "review_export.json").read_bytes())
        follow = json.loads((FIXTURES / "review_export.json").read_bytes())
        # The continued session's export: the whole conversation and cumulative metrics.
        follow["final_metrics"] = {"total_prompt_tokens": 58188, "total_completion_tokens": 240,
                                   "total_cached_tokens": 23040, "total_steps": 11}
        follow["steps"] += [{"source": "user", "message": "Send the same review again."},
                            {"source": "agent", "message": "", "tool_calls": [{"function_name": "read"}]}]
        (self.base / "follow_export.json").write_text(json.dumps(follow), encoding="utf-8")
        self.schema = self.base / "schema.json"
        # A "$" inside the schema stays literal in a filled follow-up request.
        self.schema.write_text('{"$schema": "urn:test-schema", "type": "object"}', encoding="utf-8")
        self.instructions = self.base / "instructions.md"
        self.instructions.write_text("# Review\n", encoding="utf-8")
        self.work = self.base / "attempt 1"

    def config(self, mode="pass", **changes):
        return {"id": "devin", "kind": "devin", "command": [sys.executable, str(self.fake), mode],
                "model": "swe-2", "effort": "high", "timeout_seconds": 30, **changes}

    def prepare(self, config):
        return reviewers.prepare_review(config, b"packet", self.work, self.schema, self.instructions,
                                        cwd=self.project, probe=reviewers.preflight(config))

    def run_review(self, mode="pass", validate=lambda value: value, **changes):
        config = self.config(mode, **changes)
        return reviewers.run_review(self.prepare(config), validate), config

    def calls(self):
        """Each fake devin process DevFlow launched, in order."""
        path = self.base / "calls.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] \
            if path.exists() else []

    def assert_follow_up_failed(self, message, cause, first="Invalid reviewer JSON"):
        """The shared failure shape: the follow-up, the first refusal, this cause, the records."""
        self.assertIn("follow-up", message)
        self.assertIn(first, message)  # the first answer's refusal
        self.assertIn(cause, message)
        self.assertIn(str(self.work / "follow-up"), message)

    def answer(self, export_text):
        """Devin.answer on a written export; standard output is never read."""
        self.work.mkdir(exist_ok=True)
        output = self.work / "stdout.jsonl"
        output.write_text("not the answer", encoding="utf-8")
        (self.work / "export.json").write_text(export_text, encoding="utf-8")
        return reviewers.CLIS["devin"].answer(output, self.work)

    def test_arguments_pass_the_prompt_file_model_effort_and_export(self):
        cli = reviewers.CLIS["devin"]
        self.assertEqual(cli.command, ("devin",))
        self.assertEqual(cli.arguments("swe-2", "high", Path("/w"), Path("/c"), Path("/s")),
                         ["--print", "--prompt-file", str(Path("/w") / "review-input.md"), "--model", "swe-2-high",
                          "--export", str(Path("/w") / "export.json")])
        self.assertIsNone(cli.stdin("prompt"))
        self.assertEqual(cli.flags(), ["--print", "--prompt-file", "--resume", "--model", "--export"])

    def test_a_review_reads_the_answer_activity_and_tokens_from_the_export(self):
        result, config = self.run_review()
        self.assertEqual(result["review"], REVIEW)
        # Standard output joined the messages, so it is not JSON; the answer still reads.
        self.assertTrue((self.work / "stdout.jsonl").read_text().startswith("Reading the diff.{"))
        self.assertEqual(result["activity"]["tool_calls"], {"grep": 1, "read": 1})
        seen = json.loads((self.work / "seen.json").read_text())
        self.assertEqual(seen["stdin"], "")
        self.assertEqual(seen["argv"][:2], ["--print", "--prompt-file"])
        self.assertEqual(reviewers.review_tokens(config, self.work),
                         {"total_cached_tokens": 11520, "total_completion_tokens": 120, "total_prompt_tokens": 29094})

    def test_the_measured_stdout_joins_the_agent_messages_and_the_export_keeps_the_last_one(self):
        record = json.loads((FIXTURES / "joined_export.json").read_text(encoding="utf-8"))
        messages = [step["message"] for step in record["steps"] if step["source"] == "agent"]
        stdout = (FIXTURES / "joined_stdout.txt").read_text(encoding="utf-8").rstrip("\n")
        self.assertEqual(stdout, "".join(messages))
        self.assertGreater(len(messages), 1)
        with self.assertRaises(ValueError):
            json.loads(stdout)
        self.assertEqual(reviewers.answer_value(reviewers._devin_answer(record)), {"lines": 5})

    def test_a_follow_up_answer_with_text_around_the_json_still_fails(self):
        with self.assertRaisesRegex(ReviewerError, "follow-up") as caught:
            self.run_review("prose")
        self.assert_follow_up_failed(str(caught.exception), "Invalid reviewer JSON")
        self.assertTrue((self.work / "export.json").is_file())  # The original record stays for inspection.
        self.assertEqual(len(self.calls()), 2)

    def test_a_prose_answer_is_sent_back_once_in_the_same_session(self):
        result, config = self.run_review("tidy")
        self.assertEqual(result["review"], REVIEW)
        self.assertEqual(result["activity"]["answer_follow_ups"], 1)
        first, second = self.calls()
        self.assertFalse(first["resume"])
        self.assertTrue(second["resume"])
        argv = second["argv"]
        self.assertEqual(argv[argv.index("--resume") + 1], "kind-spider")
        self.assertEqual(argv[-2:], ["--export", str(self.work / "follow-up" / "export.json")])
        follow = self.work / "follow-up"
        for name in ("review-input.md", "invocation.json", "stdout.jsonl", "stderr.log", "export.json"):
            self.assertTrue((follow / name).is_file(), name)
        # The request is the one template filled once: the first refusal's reason
        # and the whole schema (a "$" inside the schema stays literal).
        with self.assertRaises(ReviewerError) as first:
            reviewers.answer_value("Analysis of the diff.\n\n" + json.dumps(REVIEW))
        request = reviewers._fill(reviewers.FOLLOW_UP.read_text(encoding="utf-8"),
                                  "the answer follow-up request",
                                  reason=str(first.exception), schema=self.schema.read_text(encoding="utf-8"))
        self.assertEqual((follow / "review-input.md").read_text(encoding="utf-8"), request)
        self.assertIn('"$schema": "urn:test-schema"', request)
        meta = json.loads((follow / "invocation.json").read_text(encoding="utf-8"))
        first_meta = json.loads((self.work / "invocation.json").read_text(encoding="utf-8"))
        self.assertEqual(set(meta), set(first_meta) | {"follow_up"})
        self.assertTrue(meta["follow_up"])
        self.assertGreater(meta["timeout_seconds"], 0)
        self.assertLessEqual(meta["timeout_seconds"], first_meta["timeout_seconds"])
        # The first turn's export keeps its refused answer; the follow-up's export gives the
        # cumulative token counts and the whole conversation's tool calls.
        record = json.loads((self.work / "export.json").read_text(encoding="utf-8"))
        self.assertIn("Analysis of the diff.", record["steps"][-1]["message"])
        self.assertEqual(result["activity"]["tool_calls"], {"grep": 1, "read": 2})
        self.assertEqual(reviewers.review_tokens(config, self.work),
                         {"total_cached_tokens": 23040, "total_completion_tokens": 240,
                          "total_prompt_tokens": 58188})

    def test_the_follow_up_gets_only_the_time_left(self):
        config = self.config("tidy")
        prepared = self.prepare(config)
        timeouts = []
        capture = reviewers._capture
        with patch.object(reviewers, "_capture",
                          side_effect=lambda argv, **kw: (timeouts.append(kw["timeout"]), capture(argv, **kw))[1]):
            reviewers.run_review(prepared, lambda value: value)
        self.assertEqual(timeouts[0], 30)
        self.assertEqual(len(timeouts), 2)
        self.assertGreater(timeouts[1], 25)
        self.assertLessEqual(timeouts[1], 30)

    def test_a_clean_first_answer_never_follows_up(self):
        result, _ = self.run_review()
        self.assertEqual(result["activity"]["answer_follow_ups"], 0)
        self.assertFalse((self.work / "follow-up").exists())
        self.assertEqual(len(self.calls()), 1)

    def test_follow_up_arguments_continue_the_recorded_session(self):
        cli = reviewers.CLIS["devin"]
        self.work.mkdir(exist_ok=True)
        (self.work / "export.json").write_bytes((FIXTURES / "review_export.json").read_bytes())
        self.assertEqual(cli.follow_up_arguments("swe-2", "high", self.work, self.work / "follow-up"),
                         ["--print", "--prompt-file", str(self.work / "follow-up" / "review-input.md"),
                          "--resume", "kind-spider", "--model", "swe-2-high",
                          "--export", str(self.work / "follow-up" / "export.json")])
        for record in ({}, {"session_id": 7}, {"session_id": ""}, {"session_id": "-looks-like-an-option"},
                       {"session_id": "has space"}, {"session_id": "odd?chars"}):
            with self.subTest(record=record):
                (self.work / "export.json").write_text(json.dumps(record), encoding="utf-8")
                self.assertIsNone(cli.follow_up_arguments("m", "h", self.work, self.work / "follow-up"))
        for kind in ("codex", "opencode", "claude"):
            with self.subTest(kind=kind):
                self.assertIsNone(reviewers.CLIS[kind].follow_up_arguments("m", "h", self.work, self.work / "f"))

    def test_an_unreadable_session_id_keeps_the_first_error(self):
        self.work = self.base / "attempt no-session"
        with self.assertRaisesRegex(ReviewerError, "Invalid reviewer JSON") as caught:
            self.run_review("no-session")
        self.assertNotIn("follow-up", str(caught.exception))
        self.assertFalse((self.work / "follow-up").exists())

    def test_failures_that_are_not_the_answers_checks_never_follow_up(self):
        for mode, pattern in (("fail", "exited with 7"), ("no-export", "without an answer"),
                              ("empty", "without an answer")):
            self.work = self.base / f"attempt {mode}"
            with self.subTest(mode), self.assertRaisesRegex(ReviewerError, pattern):
                self.run_review(mode)
            self.assertFalse((self.work / "follow-up").exists())
        self.assertEqual(len(self.calls()), 3)

    def test_an_answer_the_workflows_checks_refuse_is_sent_back_once(self):
        # Valid JSON that the workflow's schema check refuses is re-asked once too.
        result, _ = self.run_review("not-review", accept)
        self.assertEqual(result["review"], REVIEW)
        self.assertEqual(result["activity"]["answer_follow_ups"], 1)
        first, second = self.calls()
        self.assertFalse(first["resume"])
        self.assertTrue(second["resume"])
        argv = second["argv"]
        self.assertEqual(argv[argv.index("--resume") + 1], "kind-spider")
        request = (self.work / "follow-up" / "review-input.md").read_text(encoding="utf-8")
        self.assertIn("Review has missing or unknown fields", request)
        self.assertIn(self.schema.read_text(encoding="utf-8"), request)
        # The first turn's export keeps its refused answer.
        record = json.loads((self.work / "export.json").read_text(encoding="utf-8"))
        self.assertEqual(record["steps"][-1]["message"], json.dumps({"unexpected": True}))

    def test_a_missing_fields_answer_is_sent_back_with_the_reason_and_the_schema(self):
        # The badjob2 answer: strict JSON, refused by the field check.
        result, _ = self.run_review("missing-fields", strict)
        self.assertEqual(result["review"], REVIEW)
        self.assertEqual(result["activity"]["answer_follow_ups"], 1)
        request = (self.work / "follow-up" / "review-input.md").read_text(encoding="utf-8")
        self.assertIn('missing: "schema_version", "summary"', request)
        self.assertIn(self.schema.read_text(encoding="utf-8"), request)
        argv = self.calls()[1]["argv"]
        self.assertEqual(argv[argv.index("--resume") + 1], "kind-spider")

    def test_a_verdict_the_checks_refuse_is_sent_back_once(self):
        # pass with findings: a cross-field refusal, not a missing field.
        result, _ = self.run_review("contradict", strict)
        self.assertEqual(result["review"], REVIEW)
        self.assertEqual(result["activity"]["answer_follow_ups"], 1)
        request = (self.work / "follow-up" / "review-input.md").read_text(encoding="utf-8")
        self.assertIn("A pass cannot contain findings or questions", request)

    def test_a_duplicate_key_answer_is_sent_back_with_the_escaped_reason(self):
        result, _ = self.run_review("duplicate", strict)
        self.assertEqual(result["review"], REVIEW)
        self.assertEqual(result["activity"]["answer_follow_ups"], 1)
        request = (self.work / "follow-up" / "review-input.md").read_text(encoding="utf-8")
        self.assertIn('Duplicate JSON key: "\\ud800"', request)
        self.assertTrue(request.isascii())
        self.assertIn(self.schema.read_text(encoding="utf-8"), request)

    def test_a_follow_up_answer_the_checks_refuse_fails_in_the_shared_shape(self):
        with self.assertRaisesRegex(ReviewerError, "follow-up") as caught:
            self.run_review("still-bad", accept)
        self.assert_follow_up_failed(str(caught.exception), "missing or unknown fields",
                                     first="missing or unknown fields")
        self.assertEqual(len(self.calls()), 2)

    def test_a_template_the_fill_cannot_complete_fails_in_the_shared_shape(self):
        read = reviewers._read_text

        def unfillable(path, limit):
            if Path(path).name == "answer-follow-up.md":
                return "A template with $unknown and $reason and $schema."
            return read(path, limit)

        with patch.object(reviewers, "_read_text", side_effect=unfillable), \
                self.assertRaisesRegex(ReviewerError, "follow-up") as caught:
            self.run_review("tidy")
        self.assert_follow_up_failed(str(caught.exception), "Cannot fill the answer follow-up request")
        self.assertEqual(len(self.calls()), 1)  # The follow-up turn never launched.

    def test_a_follow_up_that_exits_or_times_out_fails_in_the_shared_shape(self):
        validate = reviewers.validate_reviewer
        with patch.object(reviewers, "validate_reviewer", lambda c: validate({**c, "timeout_seconds": 5})):
            for mode, cause in (("exit-error", "exited with 7"), ("slow", "timed out")):
                self.work = self.base / f"attempt {mode}"
                with self.subTest(mode), self.assertRaisesRegex(ReviewerError, "follow-up") as caught:
                    self.run_review(mode, timeout_seconds=0.5 if mode == "slow" else 30)
                self.assert_follow_up_failed(str(caught.exception), cause)
        self.assertEqual(len(self.calls()), 4)  # Two launches each; never a second follow-up.

    def test_a_follow_up_answer_that_breaks_the_schema_fails_the_same_way(self):
        with self.assertRaisesRegex(ReviewerError, "follow-up") as caught:
            self.run_review("odd", accept)
        self.assert_follow_up_failed(str(caught.exception), "missing or unknown fields")
        self.assertEqual(len(self.calls()), 2)

    def test_no_time_left_keeps_the_first_error(self):
        real, shifted = reviewers.time.monotonic, {"on": False}
        devin, answer = reviewers.CLIS["devin"], reviewers.CLIS["devin"].answer

        def expire_after_first(output, work_dir):
            try:
                return answer(output, work_dir)
            finally:
                if Path(work_dir).name != "follow-up":
                    shifted["on"] = True

        with patch.object(reviewers.time, "monotonic", side_effect=lambda: real() + (100 if shifted["on"] else 0)), \
                patch.object(devin, "answer", side_effect=expire_after_first), \
                self.assertRaisesRegex(ReviewerError, "Invalid reviewer JSON") as caught:
            self.run_review("tidy")
        self.assertNotIn("follow-up", str(caught.exception))
        self.assertFalse((self.work / "follow-up").exists())
        self.assertEqual(len(self.calls()), 1)

    def test_time_spent_preparing_the_follow_up_fails_in_the_shared_shape(self):
        real, shifted = reviewers.time.monotonic, {"on": False}
        read = reviewers._read_text

        def expire_on_request(path, limit):
            if Path(path).name == "answer-follow-up.md":
                shifted["on"] = True
            return read(path, limit)

        with patch.object(reviewers.time, "monotonic", side_effect=lambda: real() + (100 if shifted["on"] else 0)), \
                patch.object(reviewers, "_read_text", side_effect=expire_on_request), \
                self.assertRaisesRegex(ReviewerError, "follow-up") as caught:
            self.run_review("tidy")
        self.assert_follow_up_failed(str(caught.exception), "time limit")
        self.assertEqual(len(self.calls()), 1)  # The follow-up turn never launched.
        self.assertTrue((self.work / "follow-up" / "invocation.json").is_file())
        self.assertFalse((self.work / "follow-up" / "stdout.jsonl").exists())

    def test_an_existing_or_linked_follow_up_dir_is_refused_in_the_shared_shape(self):
        self.work = self.base / "attempt existing"
        (self.work / "follow-up").mkdir(parents=True)
        with self.assertRaisesRegex(ReviewerError, "follow-up") as caught:
            self.run_review("tidy")
        self.assert_follow_up_failed(str(caught.exception), "already ran")
        self.assertEqual(len(self.calls()), 1)

    @unittest.skipIf(os.name == "nt", "Creating symlinks needs extra rights on Windows")
    def test_a_symlinked_follow_up_dir_is_refused_the_same_way(self):
        (self.base / "elsewhere").mkdir()
        self.work.mkdir()
        (self.work / "follow-up").symlink_to(self.base / "elsewhere", target_is_directory=True)
        with self.assertRaisesRegex(ReviewerError, "follow-up"):
            self.run_review("tidy")
        self.assertEqual(len(self.calls()), 1)

    def test_tokens_fall_back_to_the_first_turn_when_the_follow_up_has_none(self):
        config = self.config("tidy")
        (self.work / "follow-up").mkdir(parents=True)
        (self.work / "follow-up" / "stdout.jsonl").write_text("x", encoding="utf-8")
        (self.work / "stdout.jsonl").write_text("x", encoding="utf-8")
        (self.work / "export.json").write_text(json.dumps({"final_metrics": {"total_prompt_tokens": 10}}))
        # The follow-up's output exists but its export cannot be read: the first turn's count stands.
        self.assertEqual(reviewers.review_tokens(config, self.work), {"total_prompt_tokens": 10})

    @unittest.skipIf(os.name == "nt", "Directory permission bits differ on Windows")
    def test_an_unreadable_follow_up_dir_falls_back_to_the_first_turn(self):
        config = self.config("tidy")
        follow = self.work / "follow-up"
        follow.mkdir(parents=True)
        (follow / "stdout.jsonl").write_text("x", encoding="utf-8")
        (self.work / "stdout.jsonl").write_text("x", encoding="utf-8")
        (self.work / "export.json").write_text(json.dumps({"final_metrics": {"total_prompt_tokens": 10}}))
        follow.chmod(0)
        self.addCleanup(follow.chmod, 0o700)
        self.assertEqual(reviewers.review_tokens(config, self.work), {"total_prompt_tokens": 10})

    def test_unknown_export_shapes_are_ignored_not_counted(self):
        review, activity = self.answer(json.dumps({
            "steps": [{"tool_calls": [None, 7, {}, {"function_name": 3}, {"function_name": "read"}]}, "odd",
                      {"source": "agent", "message": json.dumps(REVIEW)}],
            "final_metrics": {"total_prompt_tokens": {"unexpected": 5}, "total_completion_tokens": -1,
                              "total_cached_tokens": True, "other_tokens": 9}}))
        self.assertEqual(review, REVIEW)
        self.assertEqual((activity["tool_call_count"], activity["tool_calls"]), (1, {"read": 1}))
        cli, output = reviewers.CLIS["devin"], self.work / "stdout.jsonl"
        self.assertIsNone(cli.tokens(output, self.work))
        (self.work / "export.json").write_text(json.dumps({"final_metrics": {
            "total_prompt_tokens": 10, "total_completion_tokens": {"x": 1}}}), encoding="utf-8")
        self.assertEqual(cli.tokens(output, self.work), {"total_prompt_tokens": 10})

    def test_without_a_readable_final_agent_message_there_is_no_answer(self):
        cases = {
            "broken": "{not json",
            "too deep": '{"steps": [], "x": ' + "[" * 100000 + "]" * 100000 + "}",
            "no steps": json.dumps({"final_metrics": {}}),
            "no agent step": json.dumps({"steps": [{"source": "user", "message": json.dumps(REVIEW)}]}),
            "message not a string": json.dumps({"steps": [{"source": "agent", "message": REVIEW}]}),
            "empty message": json.dumps({"steps": [{"source": "agent", "message": json.dumps(REVIEW)},
                                                   {"source": "agent", "message": " "}]}),
        }
        for name, text in cases.items():
            with self.subTest(name), self.assertRaisesRegex(ReviewerError, "no readable final agent message"):
                self.answer(text)

    def test_a_missing_export_or_empty_answer_fails_and_an_untrusted_workspace_says_how_to_fix_it(self):
        for mode in ("no-export", "empty"):
            self.work = self.base / f"attempt {mode}"
            with self.subTest(mode), self.assertRaisesRegex(ReviewerError, "Devin finished without an answer"):
                self.run_review(mode)
            self.assertTrue((self.work / "stdout.jsonl").is_file())
        self.work = self.base / "attempt untrusted"
        with self.assertRaisesRegex(ReviewerError, "trust it"):
            self.run_review("untrusted")

    def test_preflight_requires_every_flag_devflow_passes(self):
        self.assertEqual(reviewers.preflight(self.config())["cli_version"], "devin 3000.12.57 (96542e930752)")
        with self.assertRaisesRegex(ReviewerError, "--export"):
            reviewers.preflight(self.config("old"))

    def test_a_startup_reviewer_without_devin_in_the_settings_uses_the_devin_command(self):
        config = {"max_reviews": 6, "reviewers": [
            {"id": "codex", "kind": "codex", "command": ["codex"], "model": "gpt-6.1-sol", "effort": "high",
             "timeout_seconds": 1800},
            {"id": "opencode", "kind": "opencode", "command": ["opencode"], "model": "m", "effort": "high",
             "timeout_seconds": 1800}]}
        startup = parse_start("--reviewer devin swe-2 high --reviewer codex gpt-6.1-sol high -- feature.md")
        result = apply_startup(config, startup)
        self.assertEqual([(r["id"], r["command"], r["model"]) for r in result["reviewers"]],
                         [("devin", ["devin"], "swe-2"), ("codex", ["codex"], "gpt-6.1-sol")])


class DevinSkillTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="devflow devin skills ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.project = self.base / "project"
        self.home = self.base / "home"
        self.project.mkdir()
        self.home.mkdir()

    def write(self, directory: Path, text: str) -> Path:
        path = directory / "grilling" / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def environment(self):
        """The user's own variables point into the temporary home on every OS."""
        environ = patch.dict(os.environ, {"APPDATA": str(self.home / "AppData")})
        environ.start()
        self.addCleanup(environ.stop)
        os.environ.pop("COPILOT_HOME", None)

    def inspect(self):
        self.environment()
        return inspect_interview_skill(self.project, "grilling", config_dir=self.home, host="devin")

    def test_each_documented_location_alone_is_found(self):
        self.environment()
        roots = [root for _, root in devin_user_skill_roots(self.home)]
        roots += [self.project / path for path in DEVIN_PROJECT_SKILLS]
        self.assertIn(self.home / ".config" / "cognition" / "skills", roots)
        # Windsurf's global skills depend on Devin's release channel, which nothing offline names.
        self.assertFalse(any(".codeium" in root.parts for root in roots if self.home in root.parents))
        self.assertIn(self.project / ".claude" / "skills", roots)
        self.assertNotIn(self.home / ".claude" / "skills", roots)  # Devin imports no global Claude skills.
        for root in roots:
            with self.subTest(root=root):
                path = self.write(root, "---\nname: grilling\ndescription: \"Interview\"\n---\nAsk.\n")
                report = self.inspect()
                self.assertEqual((report["status"], report["source"]), ("available", str(path)))
                path.unlink()
        report = self.inspect()
        self.assertEqual(report["status"], "missing")
        self.assertIn("checked locations", report["reason"])
        self.assertNotIn("not exist", report["reason"])

    def test_the_parent_project_is_not_searched(self):
        nested = self.project / "child"
        nested.mkdir()
        self.write(self.project / ".devin" / "skills", "---\nname: grilling\n---\n")
        self.assertEqual(inspect_interview_skill(nested, "grilling", config_dir=self.home, host="devin")["status"],
                         "missing")

    def test_windows_and_copilot_locations_follow_their_variables(self):
        roots = dict((str(root), scope) for scope, root in devin_user_skill_roots(
            self.home, {"APPDATA": str(self.base / "AppData"), "COPILOT_HOME": str(self.base / "copilot")},
            windows=True))
        self.assertIn(str(self.base / "AppData" / "devin" / "skills"), roots)
        self.assertIn(str(self.base / "copilot" / "skills"), roots)
        self.assertNotIn(str(self.home / ".config" / "devin" / "skills"), roots)
        self.assertIn(str(self.home / ".config" / "devin" / "skills"),
                      {str(root) for _, root in devin_user_skill_roots(self.home, {}, windows=False)})

    def test_frontmatter_decides_whether_the_model_can_hold_the_interview(self):
        cases = {
            "triggers: [user]": "blocked",
            "triggers: [user, model]": "available",
            "triggers:\n  - user\n  - model": "available",
            "triggers:\n- model": "available",
            "triggers:\n  - user\n - model": "unverified",
            "triggers:\n - user\n  - model": "unverified",
            "description: \"no triggers\"": "available",
            "subagent: true": "blocked",
            "subagent: false": "available",
            "agent: reviewer": "blocked",
            "agent:": "available",
            "agent: []": "unverified",
            "agent:\n  - reviewer": "unverified",
            "subagent: maybe": "unverified",
            "subagent: False  # YAML boolean": "available",
            "subagent: TRUE": "blocked",
            # Devin documents triggers as a list and subagent as a boolean; nothing else is coerced.
            "triggers: model": "unverified",
            "triggers:": "unverified",
            "subagent: \"false\"": "unverified",
            "subagent: 0": "unverified",
            "subagent: yes": "unverified",
            "subagent:": "unverified",
            "triggers:\n  user: yes": "unverified",
            "description: Probe: unquoted colon": "invalid",
            # Claude's fields do not decide anything on Devin.
            "disable-model-invocation: true\ncontext: fork": "available",
        }
        for header, status in cases.items():
            with self.subTest(header=header):
                self.write(self.home / ".agents" / "skills", f"---\nname: grilling\n{header}\n---\nBody\n")
                self.assertEqual(self.inspect()["status"], status)

    def test_devins_documented_example_is_read(self):
        example = ("name: review\ndescription: Review code changes for issues\nargument-hint: \"[filename]\"\n"
                   "model: sonnet\nallowed-tools:\n  - read\n  - grep\n  - glob\n  - exec\n"
                   "permissions:\n  allow:\n    - Exec(npm run test)\n  deny:\n    - Write(/etc/**)\n"
                   "triggers:\n  - user\n  - model\n")
        fields = devin_frontmatter(example)
        self.assertEqual(fields["triggers"], ["user", "model"])
        self.assertEqual(fields["allowed-tools"], ["read", "grep", "glob", "exec"])
        self.assertIn("permissions", fields)
        self.write(self.home / ".agents" / "skills", "---\n" + example + "---\nBody\n")
        self.assertEqual(self.inspect()["status"], "available")

    @unittest.skipIf(os.name == "nt", "Creating symlinks needs extra rights on Windows")
    def test_symlinked_copies_are_one_definition(self):
        path = self.write(self.home / ".agents" / "skills", "---\nname: grilling\n---\n")
        (self.project / ".claude" / "skills").mkdir(parents=True)
        (self.project / ".claude" / "skills" / "grilling").symlink_to(path.parent, target_is_directory=True)
        self.assertEqual(self.inspect()["status"], "available")


if __name__ == "__main__":
    unittest.main()
