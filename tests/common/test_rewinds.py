"""The shared rewind rule: chain structure from real Claude Code transcripts,
reduced to the fields detection needs (no message content)."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from devflow_core.common import rewinds
from devflow_core.common import sessions


def record(kind, uuid=None, parent=None, content=None, **extra):
    """A chain record with the real field names; parent omitted means null."""
    return {"type": kind, "uuid": uuid, "parentUuid": parent,
            "message": {"role": kind, "content": content if content is not None else "text"}, **extra}


def user(uuid, parent, content="typed", **extra):
    return record("user", uuid, parent, content, **extra)


def assistant(uuid, parent, *blocks):
    if not blocks:
        blocks = ({"type": "text", "text": "reply"},)
    return record("assistant", uuid, parent, list(blocks))


def tool_use(uuid, parent, tool_id, name="AskUserQuestion"):
    return assistant(uuid, parent, {"type": "tool_use", "id": tool_id, "name": name, "input": {}})


def tool_result(uuid, parent, tool_id):
    return record("user", uuid, parent, [{"type": "tool_result", "tool_use_id": tool_id, "content": "rendered"}])


def feed(tracker, *rows):
    """Feed JSON rows (or raw strings) through parse_line, in order."""
    for row in rows:
        tracker.feed(rewinds.parse_line(row if isinstance(row, str) else json.dumps(row)))
    return tracker


class ParseLineTests(unittest.TestCase):
    def test_whitespace_non_object_and_non_chain_records_are_skipped(self):
        for line in ("", "   ", "null", '"text"', "[1]", '42', json.dumps({"type": "progress", "data": {}}),
                     json.dumps({"type": "queue-operation"}),
                     json.dumps(record("user", None, None)),
                     json.dumps({"type": "user", "message": {}}),  # No uuid: not a chain record.
                     json.dumps({"type": "file-history-snapshot", "uuid": "x"})):
            with self.subTest(line=line):
                self.assertEqual(rewinds.parse_line(line), rewinds.Line())

    def test_unreadable_lines_are_reported_not_raised(self):
        for line in ("{", '{"type": "user",', '{"n": NaN}', '{"n": 1e999}', '{"n": Infinity}'):
            with self.subTest(line=line):
                self.assertTrue(rewinds.parse_line(line).unreadable)

    def test_chain_structure_is_extracted(self):
        line = rewinds.parse_line(json.dumps(tool_use("u1", "p1", "ask-1")))
        self.assertTrue(line.chain)
        self.assertEqual((line.uuid, line.parent), ("u1", "p1"))
        self.assertTrue(line.assistant)
        self.assertFalse(line.tool_result)
        self.assertEqual(line.tool_use_ids, ("ask-1",))
        result = rewinds.parse_line(json.dumps(tool_result("u2", "u1", "ask-1")))
        self.assertTrue(result.tool_result)
        self.assertFalse(result.assistant)
        self.assertEqual(result.tool_use_ids, ())

    def test_a_subagent_record_is_not_on_the_main_chain(self):
        self.assertEqual(rewinds.parse_line(json.dumps(user("s1", "p", isSidechain=True))), rewinds.Line())

    def test_undecodable_bytes_are_unreadable(self):
        self.assertTrue(rewinds.parse_bytes(b"\xff\xfe").unreadable)
        self.assertTrue(rewinds.parse_bytes(json.dumps(user("u", None)).encode()).chain)


class BranchDetectionTests(unittest.TestCase):
    def test_a_linear_conversation_never_rewinds(self):
        chain = [user("u1", None), assistant("a1", "u1"), user("u2", "a1"), assistant("a2", "u2"),
                 tool_use("a3", "u2", "ask-1"), tool_result("u3", "a3", "ask-1"),
                 {"type": "system", "uuid": "s1", "parentUuid": None, "subtype": "compact_boundary"},
                 user("u4", None)]
        self.assertIsNone(feed(rewinds.BranchTracker(), *chain).rewound())

    def test_reparenting_over_a_reply_is_a_rewind(self):
        # The real shape: an abandoned branch (u3..a4, holding an assistant
        # reply) ends, and the new branch reparents back to u2.
        chain = [user("u1", None), assistant("a1", "u1"), user("u2", "a1"),
                 user("u3", "u2"), assistant("a3", "u3"), tool_use("a4", "a3", "ask-1"),
                 tool_result("u4", "a4", "ask-1"), assistant("a5", "u4"),
                 user("u2b", "u2"), assistant("a6", "u2b")]
        self.assertIsNotNone(feed(rewinds.BranchTracker(), *chain).rewound())

    def test_reparenting_over_no_reply_is_not_a_rewind(self):
        # The head at u3 discards only an attachment: no reply was abandoned.
        chain = [user("u1", None), assistant("a1", "u1"), user("u2", "a1"),
                 {"type": "attachment", "uuid": "at1", "parentUuid": "u2", "attachment": {}},
                 user("u3", "u2"), assistant("a2", "u3")]
        self.assertIsNone(feed(rewinds.BranchTracker(), *chain).rewound())

    def test_a_discarded_chain_of_only_copies_is_not_a_rewind(self):
        # The head at u4 discards position 5 only: a copy of the record at 3.
        # Copies are not replies, so this branch is not a rewind.
        rows = [user("u1", None), assistant("a1", "u1"), user("u2", "a1"), assistant("a2", "u2"),
                user("u3", "a2"), dict(assistant("a2", "u2")),  # a copy of a2, chains on
                user("u4", "u3"), assistant("a3", "u4")]
        tracker = feed(rewinds.BranchTracker(), *rows)
        self.assertIsNone(tracker.rewound())

    def test_copies_of_the_parent_and_reply_do_not_hide_a_rewind(self):
        # A rewind re-append shape: copies of the parent and the abandoned reply
        # sit between the abandoned branch and the new head. The head's parent
        # resolves to the first record with that uuid, so the discarded range
        # still contains the original reply.
        rows = [user("u1", None), assistant("a1", "u1"), user("u2", "a1"),
                user("u3", "u2"), assistant("a3", "u3"),          # abandoned reply
                dict(user("u2", "a1")),                            # copy of the parent
                dict(user("u3", "u2")), dict(assistant("a3", "u3")),  # copies of the branch
                user("u4", "u2"), assistant("a4", "u4")]           # head reparents to u2
        tracker = feed(rewinds.BranchTracker(), *rows)
        self.assertEqual(tracker.rewound(), 8)

    def test_copies_never_start_a_branch(self):
        # The old-version compact-copy shape: the same record reappears at the tail.
        rows = [user("u1", None), assistant("a1", "u1"), user("u2", "a1"),
                dict(user("u2", "a1")), assistant("a2", "u2")]
        tracker = feed(rewinds.BranchTracker(), *rows)
        self.assertIsNone(tracker.rewound())
        self.assertEqual(tracker.discarded({"x"}), set())

    def test_parent_less_records_chain_on_without_heads(self):
        rows = [{"type": "system", "uuid": "s1", "parentUuid": None, "subtype": "compact_boundary"},
                user("u1", "s1"), assistant("a1", "u1"), user("u2", None), assistant("a2", "u2")]
        self.assertIsNone(feed(rewinds.BranchTracker(), *rows).rewound())

    def test_assistant_and_tool_result_records_never_start_a_branch(self):
        # An assistant/tool-result record with a surprising parent is itself a
        # discarded/reordered detail, not a human's new branch.
        rows = [user("u1", None), user("u2", "u1"), assistant("a1", "u1"),  # parent is not u2
                tool_result("u3", "u1", "x"),                              # parent is not a1
                user("u4", "u3")]
        self.assertIsNone(feed(rewinds.BranchTracker(), *rows).rewound())

    def test_later_rewind_with_a_reply_is_detected(self):
        rows = [user("u1", None), assistant("a1", "u1"), user("u2", "a1"), assistant("a2", "u2"),
                user("u3", "u1"),  # head: discards a2, which held a reply -> rewind
                assistant("a3", "u3")]
        tracker = feed(rewinds.BranchTracker(), *rows)
        self.assertEqual(tracker.rewound(), 4)  # u3's chain position.

    def test_the_earliest_branch_with_a_reply_decides(self):
        rows = [user("u1", None),
                user("u2", "u1"), assistant("a2", "u2"),          # branch 1
                user("u3", "u1"), assistant("a3", "u3"),          # branch 2 (discards a2: rewound)
                user("u4", "u1"), assistant("a4", "u4"),          # branch 3 (discards a3 too)
                ]
        tracker = feed(rewinds.BranchTracker(), *rows)
        self.assertIsNotNone(tracker.rewound())

    def test_a_rewind_back_before_the_read_range_is_detected(self):
        # The head's parent is outside the fed range: everything fed before the
        # head is the discarded range (R4-3/R5-1).
        rows = [assistant("a1", "unseen"), tool_use("a2", "a1", "ask-1"),
                user("u2", "also-unseen"), assistant("a3", "u2")]
        tracker = feed(rewinds.BranchTracker(), *rows)
        self.assertIsNotNone(tracker.rewound())
        self.assertEqual(tracker.discarded({"ask-1"}), {"ask-1"})

    def test_an_away_summary_head_over_a_reply_is_a_rewind(self):
        # One of the four measured real rewinds had this shape: the new branch's
        # first record is a system away_summary record.
        rows = [user("u1", None), assistant("a1", "u1"), user("u2", "a1"),
                user("u3", "u2"), assistant("a3", "u3"),
                {"type": "system", "uuid": "s-away", "parentUuid": "u2", "subtype": "away_summary"},
                assistant("a4", "s-away")]
        self.assertIsNotNone(feed(rewinds.BranchTracker(), *rows).rewound())

    def test_an_away_summary_head_without_a_reply_is_not_a_rewind(self):
        rows = [user("u1", None), assistant("a1", "u1"), user("u2", "a1"),
                user("u3", "u2"),   # The abandoned range holds no assistant reply.
                {"type": "system", "uuid": "s-away", "parentUuid": "u2", "subtype": "away_summary"},
                assistant("a4", "s-away")]
        self.assertIsNone(feed(rewinds.BranchTracker(), *rows).rewound())

    def test_the_agents_killed_order_and_its_reverse(self):
        # A user record with two same-parent children appended in either order.
        # system first, then the assistant sibling: nothing reparents.
        order = [user("u1", None), user("u2", "u1"),
                 {"type": "system", "uuid": "s1", "parentUuid": "u2", "subtype": "agents_killed"},
                 assistant("a1", "u2")]
        self.assertIsNone(feed(rewinds.BranchTracker(), *order).rewound())
        # The reverse order (reported for stop_hook_summary in old versions,
        # issue 88597): the system sibling is a head discarding the reply.
        reverse = [user("u1", None), user("u2", "u1"), assistant("a1", "u2"),
                   {"type": "system", "uuid": "s1", "parentUuid": "u2", "subtype": "stop_hook_summary"}]
        self.assertIsNotNone(feed(rewinds.BranchTracker(), *reverse).rewound())

    def test_null_parent_variants_never_start_a_branch(self):
        rows = [user("u1", None), assistant("a1", "u1"),
                {"type": "system", "uuid": "s1", "parentUuid": None, "subtype": "compact_boundary"},
                {"type": "system", "uuid": "s2", "parentUuid": None, "subtype": "compaction"},
                {"type": "assistant", "uuid": "a-sum", "parentUuid": None, "isCompactSummary": True,
                 "message": {"role": "assistant", "content": "summary"}},
                {"type": "system", "uuid": "s3", "parentUuid": None},
                user("u2", "s3")]
        tracker = feed(rewinds.BranchTracker(), *rows)
        self.assertIsNone(tracker.rewound())

    def test_parallel_tool_calls_and_a_hook_attachment_are_not_rewinds(self):
        rows = [user("u1", None),
                assistant("a1", "u1", {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}),
                assistant("a2", "u1", {"type": "tool_use", "id": "t2", "name": "Bash", "input": {}}),
                tool_result("u2", "a2", "t2"),
                # A hook attachment appended as a sibling of the tool_result record.
                {"type": "attachment", "uuid": "at1", "parentUuid": "a2", "attachment": {}},
                assistant("a3", "at1")]
        self.assertIsNone(feed(rewinds.BranchTracker(), *rows).rewound())

    def test_a_reappend_differing_in_outer_fields_or_message_is_a_copy(self):
        # The same uuid written again — outer fields or the whole message may
        # differ — is a copy: never a head, never a discarded original's reply.
        rows = [user("u1", None), assistant("a1", "u1"),
                dict(user("u1", None), timestamp="later"),
                dict(user("u1", None), message={"role": "user", "content": "edited"}),
                user("u2", "a1"), assistant("a2", "u2")]
        tracker = feed(rewinds.BranchTracker(), *rows)
        self.assertIsNone(tracker.rewound())

    def issue_92089_rows(self):
        """Issue 92089: before the second /compact, copies of all records append;
        a new stop_hook_summary parents to the last copy, and the following
        compact_boundary's logicalParentUuid names the summary record."""
        originals = [user("u1", None), assistant("a1", "u1"), tool_use("a2", "a1", "ask-1"),
                     tool_result("u2", "a2", "ask-1")]
        tail = [{"type": "system", "uuid": "shs", "parentUuid": "u2", "subtype": "stop_hook_summary"},
                {"type": "system", "uuid": "cb", "parentUuid": None, "subtype": "compact_boundary",
                 "logicalParentUuid": "shs"},
                user("u3", "cb")]
        return originals, [dict(row) for row in originals], tail

    def test_the_92089_copy_shape_is_not_a_rewind(self):
        originals, copies, tail = self.issue_92089_rows()
        tracker = feed(rewinds.BranchTracker(), *originals, *copies, *tail)
        self.assertIsNone(tracker.rewound())
        self.assertEqual(tracker.discarded({"ask-1"}), set())

    def test_the_92089_shape_read_from_between_originals_and_copies(self):
        # R5-1: when the read range starts between the originals and the copies
        # (seal start after the original question), the copies chain on.
        originals, copies, tail = self.issue_92089_rows()
        tracker = feed(rewinds.BranchTracker(), *copies, *tail)
        self.assertIsNone(tracker.rewound())
        self.assertEqual(tracker.discarded({"ask-1"}), set())


class DiscardClassificationTests(unittest.TestCase):
    def rewound_transcript(self):
        """u1-a1-u2, abandoned branch u3-a3(tool_use ask-1)-result, new branch u2b."""
        return [user("u1", None), assistant("a1", "u1"), user("u2", "a1"),
                user("u3", "u2"), tool_use("a3", "u3", "ask-1"), tool_result("u4", "a3", "ask-1"),
                assistant("a4", "u4"),
                user("u2b", "u2"), tool_use("a5", "u2b", "ask-2"), tool_result("u5", "a5", "ask-2")]

    def test_a_tool_use_on_a_discarded_branch_is_reported(self):
        tracker = feed(rewinds.BranchTracker(), *self.rewound_transcript())
        self.assertEqual(tracker.discarded({"ask-1", "ask-2", "absent"}), {"ask-1"})

    def test_the_parent_record_itself_is_not_discarded(self):
        rows = [user("u1", None), tool_use("a1", "u1", "ask-p"),
                user("u2", "a1"), assistant("a2", "u2"),
                user("u3", "a1")]  # a1 is the head's parent: not inside the range.
        tracker = feed(rewinds.BranchTracker(), *rows)
        self.assertEqual(tracker.discarded({"ask-p"}), set())

    def test_a_tool_use_after_the_head_is_kept(self):
        tracker = feed(rewinds.BranchTracker(), *self.rewound_transcript())
        self.assertEqual(tracker.discarded({"ask-2"}), set())

    def test_unknown_ids_are_not_reported(self):
        tracker = feed(rewinds.BranchTracker(), *self.rewound_transcript())
        self.assertEqual(tracker.discarded({"nowhere", "also-absent"}), set())

    def test_unreadable_lines_are_indexed_for_the_caller(self):
        tracker = rewinds.BranchTracker()
        tracker.feed(rewinds.parse_line(json.dumps(user("u1", None))))
        tracker.feed(rewinds.parse_line("{"))
        tracker.feed(rewinds.parse_line(json.dumps(assistant("a1", "u1"))))
        self.assertEqual(tracker.unreadable, [1])


class RealTranscriptFixtureTests(unittest.TestCase):
    """Structural fixtures in the shape of an actual measured rewind: the
    discarded branch is the longer one, and the new branch's first record is a
    human user record reparented to a system record near the branch point."""

    def test_measured_rewind_shape(self):
        base = [user("u1", None), assistant("a1", "u1"), user("u2", "a1"),
                {"type": "system", "uuid": "sys-1", "parentUuid": "u2", "subtype": "informational"}]
        abandoned = [user("old-user", "sys-1"), tool_use("old-ask", "old-user", "ask-discarded"),
                     tool_result("old-result", "old-ask", "ask-discarded"),
                     assistant("old-a1", "old-result"), assistant("old-a2", "old-a1")]
        new = [user("new-user", "sys-1"), tool_use("new-ask", "new-user", "ask-current"),
               tool_result("new-result", "new-ask", "ask-current"), assistant("new-a1", "new-result")]
        tracker = feed(rewinds.BranchTracker(), *base, *abandoned, *new)
        self.assertIsNotNone(tracker.rewound())
        self.assertEqual(tracker.discarded({"ask-discarded"}), {"ask-discarded"})
        self.assertEqual(tracker.discarded({"ask-current"}), set())


class SealIntegrationTests(unittest.TestCase):
    """capture_interview stops the seal on a rewound transcript."""

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

    def test_seal_stops_when_the_transcript_rewound(self):
        from devflow_core.plan.transcript import CaptureError, capture_interview, transcript_marker
        marker = transcript_marker(str(self.path))
        self.write(user("u1", None, content="request"), assistant("a1", "u1"), user("u2", "a1"),
                   assistant("a2", "u2"),        # a reply the rewind abandons
                   user("u3", "a1", content="again"), assistant("a3", "u3"))
        with self.assertRaisesRegex(CaptureError, "rewound.*cancel this run"):
            capture_interview(marker, [self.prompt("request"), self.prompt("again")])

    def test_seal_passes_a_linear_transcript(self):
        from devflow_core.plan.transcript import capture_interview, transcript_marker
        marker = transcript_marker(str(self.path))
        self.write(user("u1", None, content="request"), assistant("a1", "u1"), user("u2", "a1"))
        out = capture_interview(marker, [self.prompt("request")])
        self.assertEqual(out["coverage"]["mode"], "bounded-transcript-with-hook-provenance")

    def test_a_rewind_in_the_marker_prefix_does_not_stop_the_seal(self):
        # Records before the marker are never fed: an older rewind is history.
        from devflow_core.plan.transcript import capture_interview, transcript_marker
        self.write(user("p1", None), assistant("p2", "p1"), user("p3", "p1"))  # A rewind already past.
        marker = transcript_marker(str(self.path))
        self.write(user("u1", None, content="request"), assistant("a1", "u1"))
        out = capture_interview(marker, [self.prompt("request")])
        self.assertEqual([m["content"] for m in out["messages"]], ["request", "reply"])

    def test_a_missing_transcript_runs_no_check(self):
        from devflow_core.plan.transcript import capture_interview, transcript_marker
        marker = transcript_marker(str(self.path))
        out = capture_interview(marker, [self.prompt("request")])
        self.assertEqual(out["coverage"]["mode"], "hook-records-only")

    def test_malformed_jsonl_still_stops_before_the_rewind_check(self):
        from devflow_core.plan.transcript import CaptureError, capture_interview, transcript_marker
        marker = transcript_marker(str(self.path))
        with self.path.open("w", encoding="utf-8") as stream:
            stream.write('{"type": "assistant",')
        with self.assertRaisesRegex(CaptureError, "Malformed or incomplete"):
            capture_interview(marker, [self.prompt("request")])

    def test_seal_stops_on_a_rewind_even_when_prompts_cannot_be_placed(self):
        # D13: the prompts match nothing in the transcript, so assembly falls
        # back to hook order — the rewind check still runs on the transcript.
        from devflow_core.plan.transcript import CaptureError, capture_interview, transcript_marker
        prompts = [self.prompt("a prompt no transcript record matches"),
                   self.prompt("a second prompt no transcript record matches")]
        marker = transcript_marker(str(self.path))
        self.write(user("u1", None), assistant("a1", "u1"), user("u2", "a1"))
        out = capture_interview(marker, prompts)
        self.assertEqual(out["coverage"]["mode"], "hook-records-only")  # Prove the fixture's path.
        self.path.unlink()
        marker = transcript_marker(str(self.path))
        self.write(user("u1", None), assistant("a1", "u1"), user("u2", "a1"), assistant("a2", "u2"),
                   user("u3", "a1"), assistant("a3", "u3"))
        with self.assertRaisesRegex(CaptureError, "rewound.*cancel this run"):
            capture_interview(marker, prompts)

    def test_a_withdrawn_message_does_not_stop_the_seal(self):
        # The head reparents over user records only: a withdrawn message, not a rewind.
        from devflow_core.plan.transcript import capture_interview, transcript_marker
        marker = transcript_marker(str(self.path))
        self.write(user("u1", None, content="request"), assistant("a1", "u1"),
                   user("u2", "a1", content="withdrawn"),
                   user("u3", "a1", content="request again"), assistant("a2", "u3"))
        out = capture_interview(marker, [self.prompt("request"), self.prompt("request again")])
        self.assertEqual(out["coverage"]["mode"], "bounded-transcript-with-hook-provenance")

    def test_the_tracker_is_fed_the_same_lines_the_reader_parsed(self):
        # The check runs on the transcript lines capture_interview itself read,
        # not on a separate interpretation or re-read of the file.
        from devflow_core.plan.transcript import capture_interview, transcript_marker
        fed = []

        class Recording(rewinds.BranchTracker):
            def feed(self, line):
                fed.append(line)
                return super().feed(line)

        self.write(user("u1", None, content="request"), assistant("a1", "u1"))
        marker = transcript_marker(str(self.path))
        self.write(user("u2", "a1", content="again"), assistant("a2", "u2"))
        with patch.object(rewinds, "BranchTracker", Recording):
            capture_interview(marker, [self.prompt("request"), self.prompt("again")])
        expected = [rewinds.parse_line(line) for line in self.path.read_text().splitlines()[2:]]
        self.assertEqual(fed, expected)


class TailScanTests(unittest.TestCase):
    """The reverse transcript read behind answer verification."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "transcript.jsonl"
        self._tail = None

    def write(self, *rows):
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def test_the_reverse_read_never_crosses_the_size_it_started_at(self):
        # Records appended after the read starts are not part of what the check sees.
        self.write(user("u1", None), tool_use("a1", "u1", "ask-1"))
        size = self.path.stat().st_size
        self.write(assistant("a2", "a1"), user("u2", "unseen"))  # Would be a head if seen.
        real_stat = Path.stat

        def capped(path, *args, **kwargs):
            result = real_stat(path, *args, **kwargs)
            if path == self.path:
                result = os.stat_result((*result[:6], size, *result[7:]))
            return result

        with patch.object(Path, "stat", capped):
            lines, found = sessions._scan_tail(self.path, {"ask-1"})
        self.assertTrue(lines[found["ask-1"]].assistant)
        self.assertNotIn("u2", {line.uuid for line in lines})  # The later head record was never seen.

    def test_a_transcript_truncated_during_the_read_is_not_evidence(self):
        # Fewer bytes than stat promised must fail the transcript, not pass a
        # partial scan off as complete.
        self.write(user("u1", None), tool_use("a1", "u1", "ask-1"))
        size = self.path.stat().st_size
        self.path.write_text("", encoding="utf-8")  # Truncate after the size was taken.
        real_stat = Path.stat

        def stale(path, *args, **kwargs):
            result = real_stat(path, *args, **kwargs)
            if path == self.path:
                result = os.stat_result((*result[:6], size, *result[7:]))
            return result

        with patch.object(Path, "stat", stale):
            with self.assertRaisesRegex(sessions.DevFlowError, "shrank"):
                sessions._scan_tail(self.path, {"ask-1"})

    def test_a_tool_result_line_is_not_mistaken_for_the_tool_use_record(self):
        # The tool_result carries the same id but sits after the tool_use record.
        self.write(user("u1", None), tool_use("a1", "u1", "ask-1"), tool_result("u2", "a1", "ask-1"))
        lines, found = sessions._scan_tail(self.path, {"ask-1"})
        self.assertTrue(lines[found["ask-1"]].assistant)  # The tool_use record, not the tool_result.

    def pairs(self):
        """One qualified answer pair, as _qualified_answers hands _verify_answers."""
        return [({"event_id": "e1", "kind": "question", "answer_text": "yes"},
                 {"transcript_path": str(self.path), "tool_use_id": "ask-1"})]

    def test_a_reappend_whose_original_is_before_the_boundary_rejects_a_current_answer(self):
        # Documented cutoff behavior: the re-appended record's original sits
        # before the earliest matching question, so the fed range sees a new
        # branch head and conservatively rejects a current answer.
        self.write(user("u0", None), user("u1", "u0"))
        self.write(tool_use("aq", "u1", "ask-1"))
        self.write(dict(user("u1", "u0")))   # Re-append: its original is before the question.
        kept, rejected = sessions._verify_answers(self.pairs())
        self.assertEqual(kept, [])
        self.assertEqual([reason for _, reason in rejected],
                         ["the answer is on a conversation branch a rewind discarded"])

    def test_a_copied_head_parent_before_the_boundary_accepts_a_discarded_answer(self):
        # Documented cutoff behavior: the head's parent original sits before
        # the earliest matching question, so the copy inside the fed range
        # anchors the discarded range too late and a discarded answer verifies.
        self.write(user("u1", None))
        self.write(tool_use("aq", "u1", "ask-1"), assistant("a1", "aq"),
                   dict(user("u1", None)),    # Re-append of the head's parent.
                   user("s1", "u1"),           # Chains on so u2's parent differs from prev.
                   user("u2", "u1"))           # The real rewind head.
        kept, rejected = sessions._verify_answers(self.pairs())
        self.assertEqual([item["event_id"] for item in kept], ["e1"])
        self.assertEqual(rejected, [])


class EvidenceLevelTests(unittest.TestCase):
    """approval_evidence: when no read happens and who is checked."""

    def state(self, **extra):
        return {"owner_host": "claude", "owner_session": "owner-session",
                "escalation": {"boundary": 0}, **extra}

    def answer_events(self, host="claude", session="owner-session", tool_use_id="q1", turn=None):
        tool_input = {"questions": [{"question": "Continue?"}]}
        observed = {"host": host, "session_id": session, "tool_use_id": tool_use_id,
                    "agent_scope": "main", "tool_input": tool_input}
        answered = {"host": host, "session_id": session, "tool_use_id": tool_use_id,
                    "agent_scope": "main", "hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion",
                    "outcome": "answered", "tool_input": tool_input,
                    "tool_response": {"answers": {"Continue?": "Yes"}}}
        if turn:
            observed["turn_id"] = answered["turn_id"] = turn
        return [{"kind": "observation", "event_id": "o1", "payload": observed},
                {"kind": "question", "event_id": "a1", "payload": answered}]

    def test_no_escalation_reads_no_transcript(self):
        with patch.object(sessions, "_scan_tail", side_effect=AssertionError("must not read")):
            candidates, excluded = sessions.approval_evidence(
                {"owner_host": "claude", "owner_session": "owner-session"}, self.answer_events())
        self.assertEqual((candidates, excluded), ([], []))

    def test_a_non_claude_run_never_reads_a_transcript(self):
        with patch.object(sessions, "_scan_tail", side_effect=AssertionError("must not read")):
            for host in ("codex", "devin"):
                with self.subTest(host=host):
                    candidates, excluded = sessions.approval_evidence(
                        self.state(owner_host=host), self.answer_events(host=host, turn="t1"))
                    self.assertEqual([item["event_id"] for item in candidates], ["a1"])
                    self.assertEqual(excluded, [])


if __name__ == "__main__":
    unittest.main()
