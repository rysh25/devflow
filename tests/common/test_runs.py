"""RunStore contract tests with a minimal workflow; no real workflow is involved."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from devflow_core.common import runs
from devflow_core.common.errors import DevFlowError
from devflow_core.common.runs import RunStore, make_event, open_escalation
from devflow_core.common.storage import atomic_json, read_json
from devflow_core.common.workflow import Workflow


class Toy(Workflow):
    name = "toy"
    statuses = frozenset({"working", "running", "paused_error", "completed", "exhausted", "cancelled"})
    inflight_statuses = frozenset({"running"})
    state_fields = frozenset({"note"})


def prompt(session="owner", text="hello"):
    return {"hook_event_name": "UserPromptSubmit", "session_id": session, "prompt": text}


def question(session="owner", use_id="q1", answer="yes"):
    return {"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion", "session_id": session,
            "tool_use_id": use_id, "tool_input": {"questions": [{"question": "Q?"}]}, "tool_response": answer}


class RunStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="devflow runs ")
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name).resolve()
        self.store = RunStore(self.project, Toy(), lock_wait=0.2)
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
        self.assertEqual(self.state["schema_version"], 3)
        self.assertEqual(len(self.state["event_ids"]), 1)

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

    def test_read_never_replays_a_pending_transaction_but_load_does(self):
        with self.store.lock(self.run_id):
            state = self.store.load(self.run_id)
        desired = dict(state, note="journaled", state_revision=1)
        journal = self.store.run_dir(self.run_id) / "transaction.json"
        atomic_json(journal, {"schema_version": 3, "base_revision": 0, "state": desired, "writes": {}})
        info = self.store.inspect(self.run_id)
        self.assertTrue(info.recovery_required)
        self.assertEqual(self.store.read(self.run_id)["note"], "n")
        self.assertTrue(journal.exists())
        with self.store.lock(self.run_id):
            self.assertEqual(self.store.load(self.run_id)["note"], "journaled")
        self.assertFalse(journal.exists())

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
        identifier = RunStore(self.project, Toy(), lock_wait=2).append_event(self.run_id, prompt(), "prompt")
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
        engine = RunEngine(self.project, Toy())
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
        bad = runs.new_run_id()
        (self.store.root / bad).mkdir()
        self.assertEqual((self.store.inspect(bad).format, self.store.inspect(bad).schema_version), ("invalid", None))
        listed, creating = self.store.scan()
        self.assertEqual(sorted(info.format for info in listed), ["current", "invalid", "unsupported", "unsupported"])
        self.assertEqual(creating, [])


if __name__ == "__main__":
    unittest.main()
