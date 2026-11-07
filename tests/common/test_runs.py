"""RunStore contract tests with a minimal workflow; no real workflow is involved."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from devflow_core.common import runs, storage
from devflow_core.common.errors import DevFlowError
from devflow_core.common.runs import RunStore, make_event, open_escalation
from devflow_core.common.storage import atomic_json, read_json
from devflow_core.common.workflow import Workflow


class Toy(Workflow):
    name = "toy"
    statuses = frozenset({"working", "running", "paused_error"}) | runs.TERMINAL
    inflight_statuses = frozenset({"running"})
    state_fields = frozenset({"note"})


def prompt(session="owner", text="hello"):
    return {"host": "claude", "hook_event_name": "UserPromptSubmit", "session_id": session, "prompt": text}


def question(session="owner", use_id="q1", answer="yes"):
    return {"host": "claude", "hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion", "session_id": session,
            "tool_use_id": use_id, "tool_input": {"questions": [{"question": "Q?"}]}, "tool_response": answer}


def asked(session="owner", use_id="q1", **fields):
    """An AskUserQuestion as Claude sent it, recorded at PreToolUse (an observation)."""
    return {"host": "claude", "hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion", "session_id": session,
            "tool_use_id": use_id, "tool_input": {"questions": [{"question": "Q?"}], **fields}}


class RunStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="devflow runs ")
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name).resolve()
        self.store = RunStore(self.project, Toy(), lock_wait=0.2, host="claude")
        self.run_id = runs.new_run_id()
        self.state = self.store.create({"status": "working", "note": "n", "escalation": None},
                                       {"input.txt": b"data\n"}, run_id=self.run_id, owner="owner",
                                       first_event=make_event(prompt(), "prompt"))

    def test_create_publishes_a_complete_run_and_a_private_gitignore(self):
        run = self.store.run_dir(self.run_id)
        self.assertEqual(read_json(run / "state.json"), self.state)
        self.assertEqual((run / "input.txt").read_bytes(), b"data\n")
        self.assertTrue((run / "draft").is_dir())
        self.assertEqual((self.project / ".devflow" / ".gitignore").read_text(), "# Private DevFlow records; never publish them.\n*\n")
        self.assertEqual(self.state["schema_version"], runs.SCHEMA_VERSION)
        self.assertEqual(len(self.state["event_ids"]), 1)

    def test_recovery_requires_the_original_host_and_preserves_foreign_records(self):
        with self.store.lock(self.run_id):
            state = self.store.load(self.run_id)
            self.store.reserve(state, "running", "review", 1, "target", "reviewer")
        path = self.store.run_dir(self.run_id) / "state.json"
        before = path.read_bytes()
        other = RunStore(self.project, Toy(), lock_wait=0.2, host="codex")
        with patch.object(other.workflow, "on_recover", side_effect=lambda store, state: state.update(status="working")) as callback:
            with self.assertRaisesRegex(DevFlowError, "Cross-host recovery"):
                other.recover(self.run_id)
        callback.assert_not_called()
        self.assertEqual(path.read_bytes(), before)
        with patch.object(self.store.workflow, "on_recover", side_effect=lambda store, state: state.update(status="working")):
            recovered = self.store.recover(self.run_id)
        self.assertEqual(recovered["status"], "working")
        self.assertEqual(recovered["owner_host"], "claude")
        self.assertIsNone(recovered["inflight"])

    def test_failed_creation_leaves_no_run_directory(self):
        identifier = runs.new_run_id()
        with patch.object(runs.os, "rename", side_effect=OSError("simulated stop")), self.assertRaises(OSError):
            self.store.create({"status": "working", "note": "n", "escalation": None}, {}, run_id=identifier,
                              owner="owner", first_event=make_event(prompt(), "prompt"))
        self.assertFalse((self.store.root / identifier).exists())
        self.assertFalse(any(path.name.startswith(runs.CREATING) for path in self.store.root.iterdir()))

    def test_unknown_fields_and_inflight_mismatch_are_invalid(self):
        for change in ({"surprise": 1}, {"status": "running"}, {"inflight": {"kind": "x"}}, {"status": "unknown"},
                       {"owner_session": "../x"}, {"event_ids": ["not-hex"]}):
            with self.subTest(change=change), self.assertRaises(DevFlowError):
                self.store.validate(dict(self.state, **change), self.run_id)
        self.store.validate(dict(self.state, status="running", inflight={"kind": "x"}), self.run_id)

    def test_save_requires_the_lock_and_increments_the_revision(self):
        with self.assertRaisesRegex(DevFlowError, "lock"):
            self.store.save(dict(self.state))
        with self.store.lock(self.run_id):
            state = self.store.load(self.run_id)
            state["note"] = "changed"
            self.store.save(state)
        self.assertEqual(self.store.read(self.run_id)["state_revision"], 1)

    def interrupted(self, change, *, records=None):
        """A save that stops before ``state.json`` is replaced, after writing its records."""
        original = runs.atomic_json

        def stop(path, value):
            if Path(path).name == "state.json":
                raise OSError("stopped before the commit")
            return original(path, value)
        with self.store.lock(self.run_id):
            state = self.store.load(self.run_id)
            change(state)
            for relative, data in (records or {}).items():
                self.store.stage(state, relative, data)
            with patch.object(runs, "atomic_json", side_effect=stop), self.assertRaises(OSError):
                self.store.save(state)

    def test_display_reads_never_write_and_a_stopped_save_is_not_committed(self):
        run = self.store.run_dir(self.run_id)
        before = sorted(path.relative_to(run) for path in run.rglob("*"))
        self.store.read(self.run_id)
        self.store.inspect(self.run_id)
        self.assertEqual(sorted(path.relative_to(run) for path in run.rglob("*")), before)
        self.interrupted(lambda state: state.update(note="never"), records={"left.json": b'{"a": 1}\n'})
        committed = self.store.read(self.run_id)
        self.assertEqual((committed["note"], committed["state_revision"]), ("n", 0))
        self.assertNotIn("left.json", committed["artifacts"])
        # The leftover is listed by nothing, so verification and reads ignore it.
        self.assertTrue((run / "left.json").is_file())
        self.store.verify_artifacts(committed)
        self.assertEqual(self.store.inspect(self.run_id).format, "current")
        self.assertFalse((run / "transaction.json").exists())

    def test_a_save_writes_new_records_replaces_leftovers_and_never_changes_committed_ones(self):
        run = self.store.run_dir(self.run_id)
        self.interrupted(lambda state: None, records={"rounds/01.json": b'{"old": true}\n'})
        with self.store.lock(self.run_id):
            state = self.store.load(self.run_id)
            self.store.stage(state, "rounds/01.json", b'{"new": true}\n')  # Replaces the leftover.
            self.store.stage(state, "rounds/02.json", b'{"fresh": true}\n')  # Nothing there yet.
            self.store.stage(state, "input.txt", b"data\n")  # Committed and identical: nothing to do.
            self.store.save(state)
        self.assertEqual((run / "rounds" / "01.json").read_bytes(), b'{"new": true}\n')
        self.assertEqual((run / "rounds" / "02.json").read_bytes(), b'{"fresh": true}\n')
        self.store.verify_artifacts(self.store.read(self.run_id))
        with self.store.lock(self.run_id):
            state = self.store.load(self.run_id)
            self.store.stage(state, "input.txt", b"different\n")
            with self.assertRaisesRegex(DevFlowError, "Refusing to overwrite a different immutable artifact: input.txt"):
                self.store.save(state)
        self.assertEqual((run / "input.txt").read_bytes(), b"data\n")
        self.assertEqual(self.store.read(self.run_id)["state_revision"], 1)

    def test_a_save_after_the_state_changed_on_disk_is_refused(self):
        with self.store.lock(self.run_id):
            state = self.store.load(self.run_id)
            stale = dict(state, note="stale")
            state["note"] = "first"
            self.store.save(state)
            with self.assertRaisesRegex(DevFlowError, "changed since it was loaded"):
                self.store.save(stale)
        self.assertEqual(self.store.read(self.run_id)["note"], "first")

    def test_verification_reads_files_in_chunks_and_finds_changes(self):
        run = self.store.run_dir(self.run_id)
        with patch.object(Path, "read_bytes", side_effect=AssertionError("read whole")):
            self.store.verify_artifacts(self.state)
        (run / "input.txt").write_bytes(b"changed\n")
        with self.assertRaisesRegex(DevFlowError, "Immutable artifact changed or is missing: input.txt"):
            self.store.verify_artifacts(self.state)

    def test_json_records_that_could_not_be_read_back_are_never_written(self):
        with patch.object(storage, "JSON_LIMIT", 64):
            with self.store.lock(self.run_id):
                state = self.store.load(self.run_id)
                with self.assertRaisesRegex(DevFlowError, "Record big.json would be .* over the 64 MiB JSON limit"):
                    self.store.stage(state, "big.json", b"x" * 65)
                state["note"] = "x" * 100
                with self.assertRaisesRegex(DevFlowError, "Record state.json would be"):
                    self.store.save(state)
            identifier = runs.new_run_id()
            with self.assertRaisesRegex(DevFlowError, "Record start.json would be"):
                self.store.create({"status": "working", "note": "n", "escalation": None}, {"start.json": b"x" * 65},
                                  run_id=identifier, owner="owner", first_event=make_event(prompt(), "prompt"))
        self.assertEqual(self.store.read(self.run_id)["note"], "n")
        self.assertFalse((self.store.root / identifier).exists())
        # A JSON record given as a file is refused the same way, by its size.
        identifier = runs.new_run_id()
        with self.store.scratch(identifier) as scratch:
            source = scratch / "start.json"
            storage.write_new(source, b"x" * 65)
            with patch.object(storage, "JSON_LIMIT", 64), \
                    self.assertRaisesRegex(DevFlowError, "Record start.json would be 65 bytes, over the 64 MiB JSON limit"):
                self.store.create({"status": "working", "note": "n", "escalation": None}, {"start.json": source},
                                  run_id=identifier, owner="owner", first_event=make_event(prompt(), "prompt"))
        self.assertFalse((self.store.root / identifier).exists())
        self.assertFalse(any(path.name.startswith(runs.CREATING) for path in self.store.root.iterdir()))

    def test_a_file_record_is_moved_into_the_new_run(self):
        identifier = runs.new_run_id()
        with self.store.scratch(identifier) as scratch:
            self.assertTrue(scratch.name.startswith(runs.CREATING))
            source = scratch / "copy"
            storage.write_new(source, [b"large ", b"record\n"])
            state = self.store.create({"status": "working", "note": "n", "escalation": None},
                                      {"baseline/copy": source}, run_id=identifier, owner="owner",
                                      first_event=make_event(prompt(), "prompt"))
            self.assertFalse(source.exists())
        run = self.store.run_dir(identifier)
        self.assertEqual((run / "baseline" / "copy").read_bytes(), b"large record\n")
        self.assertEqual(state["artifacts"]["baseline/copy"], storage.digest(b"large record\n"))
        self.assertFalse(scratch.exists())

    def test_the_scratch_directory_is_removed_after_a_failed_start(self):
        identifier = runs.new_run_id()
        with self.assertRaises(OSError):
            with self.store.scratch(identifier) as scratch:
                (scratch / "copy").write_bytes(b"x")
                raise OSError("start failed")
        self.assertFalse(scratch.exists())

    def test_scratch_creates_the_record_directories_of_a_new_project(self):
        project = self.project / "fresh project"
        project.mkdir()
        store = RunStore(project, Toy(), host="claude")
        with store.scratch(runs.new_run_id()) as scratch:
            self.assertTrue(scratch.is_dir())
        self.assertTrue((project / ".devflow" / ".gitignore").is_file())

    def test_question_retries_are_deduplicated_per_session(self):
        first = self.store.append_event(self.run_id, question(), "question")
        self.assertEqual(self.store.append_event(self.run_id, question(), "question"), first)
        with self.assertRaisesRegex(DevFlowError, "different content"):
            self.store.append_event(self.run_id, question(answer="no"), "question")
        with self.store.lock(self.run_id):
            state = self.store.load(self.run_id)
            state["owner_session"] = "next-owner"
            self.store.save(state)
        other = self.store.append_event(self.run_id, question(session="next-owner"), "question")
        self.assertNotEqual(other, first)

    def test_every_question_is_recorded_as_an_observation_once_per_tool_use(self):
        # Ordinary and prefilled questions alike: the observation is the question as sent.
        ordinary = self.store.append_event(self.run_id, asked(), "observation")
        self.assertEqual(self.store.append_event(self.run_id, asked(), "observation"), ordinary)
        prefilled = self.store.append_event(self.run_id, asked(use_id="q2", answers={"Q?": "Yes"}), "observation")
        self.assertNotEqual(prefilled, ordinary)
        with self.assertRaisesRegex(DevFlowError, "different content"):
            self.store.append_event(self.run_id, asked(answers={"Q?": "No"}), "observation")
        kinds = [event["kind"] for event in self.store.events(self.store.read(self.run_id))]
        self.assertEqual(kinds, ["prompt", "observation", "observation"])

    def test_an_observation_needs_its_tool_identity_and_input_and_the_owner(self):
        for payload in (dict(asked(), tool_use_id=""), dict(asked(), tool_input="Q?")):
            with self.subTest(payload=payload), self.assertRaisesRegex(DevFlowError, "Observation capture requires"):
                self.store.append_event(self.run_id, payload, "observation")
        with self.assertRaisesRegex(DevFlowError, "another session"):
            self.store.append_event(self.run_id, asked(session="intruder"), "observation")

    def test_events_from_other_sessions_and_terminal_runs_are_not_recorded(self):
        with self.assertRaisesRegex(DevFlowError, "another session"):
            self.store.append_event(self.run_id, prompt(session="intruder"), "prompt")
        with self.store.lock(self.run_id):
            state = self.store.load(self.run_id)
            state["status"] = "completed"
            self.store.save(state)
        self.assertIsNone(self.store.append_event(self.run_id, prompt(), "prompt"))
        self.assertEqual(len(self.store.read(self.run_id)["event_ids"]), 1)

    def test_event_size_and_count_limits(self):
        with self.assertRaisesRegex(DevFlowError, "4 MiB"):
            self.store.append_event(self.run_id, prompt(text="x" * (runs.MAX_EVENT_BYTES + 1)), "prompt")
        with patch.object(runs, "MAX_EVENTS", 2):
            self.store.append_event(self.run_id, prompt(), "prompt")
            with self.assertRaisesRegex(DevFlowError, "limit"):
                self.store.append_event(self.run_id, prompt(), "prompt")

    def test_event_append_waits_for_a_short_lock_holder(self):
        holder_done = threading.Event()

        def hold():
            with self.store.lock(self.run_id):
                holder_done.wait(0.1)

        thread = threading.Thread(target=hold)
        thread.start()
        identifier = RunStore(self.project, Toy(), lock_wait=2, host="claude").append_event(self.run_id, prompt(), "prompt")
        thread.join()
        self.assertIn(identifier, self.store.read(self.run_id)["event_ids"])

    def test_transfer_moves_the_answer_boundary_only_when_the_owner_changes(self):
        with self.store.lock(self.run_id):
            state = self.store.load(self.run_id)
            open_escalation(state, "E1", "Decide", ["Q?"], "working")
            self.store.save(state)
            same = self.store.transfer(self.store.load(self.run_id), "owner", prompt(text="/devflow:resume x"))
        self.assertEqual(same["escalation"]["boundary"], 1)
        self.assertEqual(len(same["event_ids"]), 2)
        with self.store.lock(self.run_id):
            moved = self.store.transfer(self.store.load(self.run_id), "new-owner", prompt("new-owner", "/devflow:resume x"))
        self.assertEqual(moved["owner_session"], "new-owner")
        self.assertEqual(moved["escalation"]["boundary"], len(moved["event_ids"]))
        self.assertEqual(moved["escalation"]["session_id"], "new-owner")

    def test_reserve_and_claim_touch_only_envelope_fields(self):
        with self.store.lock(self.run_id):
            state = self.store.load(self.run_id)
            reservation = self.store.reserve(state, "running", "job", 1, "target")
            state = self.store.claim(self.run_id, reservation, "running", {"result": "ok", "error": None})
            state["status"] = "working"
            self.store.save(state)
        self.assertNotIn("last_error", self.store.read(self.run_id))  # The toy workflow has no such field.

    def test_the_shared_engine_needs_no_workflow_fields(self):
        from devflow_core.common.engine import RunEngine
        engine = RunEngine(self.project, Toy(), host="claude")
        status = engine.status(self.run_id)  # The toy has no config, budget or summary values.
        self.assertEqual((status["status"], status["human_events"]), ("working", []))
        engine._verify(engine.store.read(self.run_id))
        self.assertIsNone(engine.read_frozen_config(self.run_id))

    def test_transfer_refuses_an_inflight_run(self):
        with self.store.lock(self.run_id):
            state = self.store.load(self.run_id)
            state.update(status="running", inflight={"kind": "x", "number": 1})
            self.store.save(state)
            with self.assertRaisesRegex(DevFlowError, "recover"):
                self.store.transfer(self.store.load(self.run_id), "new-owner", prompt("new-owner"))

    def test_malformed_types_and_missing_fields_are_invalid_not_exceptions(self):
        for change in ({"status": []}, {"status": {"x": 1}}, {"artifacts": []}, {"event_ids": None},
                       {"escalation": {"boundary": "1"}}, {"state_revision": "1"}):
            with self.subTest(change=change):
                state = dict(self.state, **change)
                atomic_json(self.store.run_dir(self.run_id) / "state.json", state)
                info = self.store.inspect(self.run_id)
                self.assertEqual(info.format, "invalid", info)
        missing = {key: value for key, value in self.state.items() if key != "note"}
        atomic_json(self.store.run_dir(self.run_id) / "state.json", missing)
        self.assertEqual(self.store.inspect(self.run_id).format, "invalid")
        self.assertEqual([info.format for info in self.store.scan()[0]], ["invalid"])

    def test_the_first_event_obeys_the_event_size_limit(self):
        identifier = runs.new_run_id()
        with self.assertRaisesRegex(DevFlowError, "4 MiB"):
            self.store.create({"status": "working", "note": "n", "escalation": None}, {}, run_id=identifier,
                              owner="owner", first_event=make_event(prompt(text="x" * runs.MAX_EVENT_BYTES), "prompt"))
        self.assertFalse((self.store.root / identifier).exists())

    def test_inspect_classifies_old_damaged_and_current_runs(self):
        self.assertEqual(self.store.inspect(self.run_id).format, "current")
        old = runs.new_run_id()
        atomic_json(self.store.root / old / "state.json", {"schema_version": 1, "run_id": old, "flow": "toy",
                                                            "project": str(self.project), "status": "x"})
        self.assertEqual((self.store.inspect(old).format, self.store.inspect(old).schema_version), ("unsupported", 1))
        previous = runs.new_run_id()
        atomic_json(self.store.root / previous / "state.json", {"schema_version": 2, "run_id": previous, "flow": "toy",
                                                                 "project": str(self.project), "status": "x"})
        info = self.store.inspect(previous)
        self.assertEqual((info.format, info.schema_version), ("unsupported", 2))
        self.assertEqual(info.public()["message"], "Started with DevFlow 0.2.x; this version cannot continue it")
        self.assertEqual(runs.unsupported_message(previous, 2),
                         f"Run {previous} was started with DevFlow 0.2.x; this version cannot continue it")
        self.assertEqual(self.store.inspect(self.run_id).schema_version, runs.SCHEMA_VERSION)
        # 0.3.x wrote format 3; it is now an older format that is never continued.
        third = runs.new_run_id()
        atomic_json(self.store.root / third / "state.json", {"schema_version": 3, "run_id": third, "flow": "toy",
                                                              "project": str(self.project), "status": "x"})
        info = self.store.inspect(third)
        self.assertEqual((info.format, info.schema_version), ("unsupported", 3))
        self.assertEqual(info.public()["message"], "Started with DevFlow 0.3.x; this version cannot continue it")
        # 0.6.x wrote format 8; its unfinished runs are finished with 0.6.x.
        sixth = runs.new_run_id()
        atomic_json(self.store.root / sixth / "state.json", {"schema_version": 8, "run_id": sixth, "flow": "toy",
                                                              "project": str(self.project), "status": "x"})
        self.assertEqual(self.store.inspect(sixth).public()["message"],
                         "Started with DevFlow 0.6.x; this version cannot continue it")
        bad = runs.new_run_id()
        (self.store.root / bad).mkdir()
        self.assertEqual((self.store.inspect(bad).format, self.store.inspect(bad).schema_version), ("invalid", None))
        listed, creating = self.store.scan()
        self.assertEqual(sorted(info.format for info in listed),
                         ["current", "invalid", "unsupported", "unsupported", "unsupported", "unsupported"])
        self.assertEqual(creating, [])


    def test_the_end_time_is_set_once_when_a_run_first_ends(self):
        self.assertIsNone(self.state["finished_at"])
        self.assertIn("accepted", runs.TERMINAL)
        seen = {}
        # The end time is there before before_save, so a terminal report can use it.
        with patch.object(Toy, "before_save", lambda workflow, store, state: seen.update(at=state["finished_at"])):
            with self.store.lock(self.run_id):
                state = self.store.load(self.run_id)
                state["status"] = "accepted"
                self.store.save(state)
        ended = self.store.read(self.run_id)
        self.assertEqual(seen["at"], ended["finished_at"])
        self.assertIsInstance(ended["finished_at"], str)
        with self.store.lock(self.run_id):
            state = self.store.load(self.run_id)
            state["note"] = "later"
            self.store.save(state)
        self.assertEqual(self.store.read(self.run_id)["finished_at"], ended["finished_at"])
        # A running run has no end time, and an ended one always has one.
        for change in ({"finished_at": "2026-11-05T00:00:00+00:00"}, {"finished_at": 1}):
            with self.subTest(change=change), self.assertRaises(DevFlowError):
                self.store.validate(dict(self.state, **change), self.run_id)
        with self.assertRaises(DevFlowError):
            self.store.validate(dict(ended, finished_at=None), self.run_id)

    def test_deeply_nested_json_is_an_ordinary_read_error(self):
        with self.assertRaisesRegex(DevFlowError, "nested too deeply"):
            storage.parse_json(b"[" * 100000 + b"]" * 100000, "deep.json")

    def test_a_record_is_read_from_the_pending_save_or_verified_on_disk(self):
        with self.store.lock(self.run_id):
            state = self.store.load(self.run_id)
            self.assertEqual(self.store.record(state, "input.txt"), b"data\n")
            self.store.stage(state, "held.json", b'{"a": 1}')
            # Not on disk yet: the next save writes it.
            self.assertEqual(self.store.record_json(state, "held.json"), {"a": 1})
            with self.assertRaisesRegex(DevFlowError, "Not a record"):
                self.store.record(state, "other.txt")
        (self.store.run_dir(self.run_id) / "input.txt").write_bytes(b"changed\n")
        with self.assertRaisesRegex(DevFlowError, "changed or is missing"):
            self.store.record(self.store.read(self.run_id), "input.txt")


if __name__ == "__main__":
    unittest.main()
