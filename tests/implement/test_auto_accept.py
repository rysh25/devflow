"""auto_accept_at_review_limit: an Implement run's configuration acceptance at the review limit."""
from __future__ import annotations

from unittest.mock import patch
import json
import os
import shutil
import subprocess
import threading
import unittest

from devflow_core.common import git as git_module
from devflow_core.common.errors import DevFlowError, ProcessCleanupError
from devflow_core.common.process import Completed
from devflow_core.common.review import AUTO_ACCEPTANCE, REVIEW_LIMIT_REASON
from devflow_core.common.settings import read_project_settings
from devflow_core.common.storage import atomic_write_json, digest, read_json
from devflow_core.implement.config import load_config, validate_config
from devflow_core.implement.engine import Engine
from devflow_core.workflows import WORKFLOWS
from tests.implement.test_engine import EngineFixture


class ConfigLayeringTests(EngineFixture):
    """The shared setting's precedence and strict boolean validation for Implement."""

    def load(self):
        return load_config(read_project_settings(self.project, "claude", WORKFLOWS), WORKFLOWS["implement"])

    def test_defaults_to_false(self):
        self.assertFalse(self.load()["auto_accept_at_review_limit"])

    def test_shared_top_level_enables_and_flow_overrides(self):
        atomic_write_json(self.project / "devflow.config.json",
                          {"schema_version": 1, "auto_accept_at_review_limit": True,
                           "implement": {**self.config, "auto_accept_at_review_limit": False}})
        self.assertFalse(self.load()["auto_accept_at_review_limit"])

    def test_local_overrides_shared(self):
        self.config["auto_accept_at_review_limit"] = True
        self.write_config()
        atomic_write_json(self.project / "devflow.config.local.json",
                          {"schema_version": 1, "implement": {"auto_accept_at_review_limit": False}})
        self.assertFalse(self.load()["auto_accept_at_review_limit"])

    def test_only_a_boolean_is_accepted(self):
        for bad in ("true", 1, 0, None):
            with self.subTest(value=bad):
                # A complete effective config, with only the new key made invalid.
                with self.assertRaisesRegex(DevFlowError, "must be a boolean"):
                    validate_config({**self.load(), "auto_accept_at_review_limit": bad})
        self.config["auto_accept_at_review_limit"] = "enabled"
        self.write_config()
        with self.assertRaises(DevFlowError):
            self.load()


class AutoAcceptTests(EngineFixture):
    def setUp(self):
        super().setUp()
        self.config["auto_accept_at_review_limit"] = True
        self.config["max_reviews"] = 2
        self.write_config()

    def to_limit(self):
        """Two handled reviews at max_reviews 2; ready_review with a validated candidate."""
        self.start()
        self.ready()
        self.response_kind = "changes_required"
        for _ in range(2):
            self.review()
            self.reflect()
            self.ready()
        self.assertEqual(self.state()["status"], "ready_review")
        self.assertEqual(len(self.state()["attempts"]), 2)

    def test_enabled_ends_as_auto_accepted_with_report_and_evidence(self):
        self.to_limit()
        # A checkpoint after the last review: the accepted snapshot was never reviewed.
        self.source.write_text("VALUE = 7\n", encoding="utf-8")
        self.checkpoint()
        self.assertEqual(self.engine.check(self.run_id, self.session)["status"], "ready_review")
        self.preflight.reset_mock()
        result = self.review()
        self.assertEqual(self.reviewer.call_count, 2)  # no reviewer started at the limit
        self.preflight.assert_not_called()
        self.assertEqual(result["status"], "auto_accepted")
        state = self.state()
        self.assertIn(AUTO_ACCEPTANCE, state["artifacts"])
        record = read_json(self.run / AUTO_ACCEPTANCE)
        self.assertEqual((record["source"], record["auto_accept_at_review_limit"]), ("config", True))
        self.assertEqual(record["version"], state["candidate"]["snapshot_id"])
        self.assertEqual((record["review_count"], record["review_limit"]), (2, 2))
        self.assertFalse(record["version_reviewed"])
        self.assertFinalReportSaved(result)
        self.assertIsNotNone(result["report"]["auto_acceptance"])
        self.assertIsNone(result["report"]["human_acceptance"])
        self.assertIn("auto_accept_at_review_limit", result["report"]["warning"])
        # A duplicate review request cannot end it again or dispatch a reviewer.
        with self.assertRaises(DevFlowError):
            self.review()

    def test_disabled_keeps_the_human_question(self):
        self.config["auto_accept_at_review_limit"] = False
        self.write_config()
        self.to_limit()
        asked = self.review()
        self.assertEqual(asked["status"], "awaiting_human")
        self.assertIn(REVIEW_LIMIT_REASON, asked["escalation"]["reason"])

    def test_zero_successful_reviews_keeps_the_question(self):
        self.start()
        self.ready()
        self.reviewer.side_effect = DevFlowError("provider down")
        self.review()  # codex fails once
        self.review()  # opencode fails once: the limit is reached without a success
        asked = self.review()
        self.assertEqual(asked["status"], "awaiting_human")
        self.assertNotIn(AUTO_ACCEPTANCE, self.state()["artifacts"])

    def test_manual_attestation_satisfies_the_validation_gate(self):
        self.config["checks"] = []
        self.write_config()
        self.start()
        result = self.ready()
        # No checks configured: validation is the human's attestation.
        self.assertEqual(result["status"], "awaiting_human")
        evidence = self.answer("Yes, I independently verified this version.")
        result = self.engine.resolve(self.run_id, self.session, evidence, "continue",
                                     "The user verified this version manually.")
        self.assertEqual(result["status"], "ready_review")
        self.response_kind = "changes_required"
        for _ in range(2):
            self.review()
            self.reflect()
            self.source.write_text("VALUE = 2\n", encoding="utf-8")
            self.checkpoint()
            result = self.engine.check(self.run_id, self.session)
            self.assertEqual(result["status"], "awaiting_human")
            evidence = self.answer("Yes, I independently verified this version.")
            result = self.engine.resolve(self.run_id, self.session, evidence, "continue",
                                         "The user verified this version manually.")
            self.assertEqual(result["status"], "ready_review")
        result = self.review()
        self.assertEqual(result["status"], "auto_accepted")

    def test_unanimous_approval_at_the_last_slot_still_completes(self):
        self.start()
        self.ready()
        self.review()  # codex pass
        result = self.review()  # opencode pass at the limit
        self.assertEqual(result["status"], "completed")
        self.assertNotIn(AUTO_ACCEPTANCE, self.state()["artifacts"])

    def test_resume_keeps_the_terminal_state_without_recapture(self):
        self.to_limit()
        result = self.review()
        report = result["report"]
        self.assertEqual(report["workspace_snapshot_id_at_report"],
                         self.state()["candidate"]["snapshot_id"])
        # Status and resume reuse the staged report; no new snapshot is captured.
        resumed = self.resume("another-session")
        self.assertEqual(resumed["status"], "auto_accepted")
        status = self.engine.status(self.run_id)
        self.assertEqual(status["report_path"], resumed["report_path"])

    def test_a_cleanup_failure_at_the_limit_pauses_then_can_auto_accept(self):
        self.to_limit()
        # The workspace identity/snapshot read at the limit fails to clean up a
        # child: a technical pause, no attempt consumed, no acceptance recorded.
        with patch.object(Engine, "_current", side_effect=ProcessCleanupError("a helper lingers")):
            result = self.review()
        self.assertEqual(result["status"], "paused_error")
        self.assertIn("a helper lingers", result["last_error"])
        self.assertEqual(self.state()["status"], "paused_error")
        self.assertNotIn(AUTO_ACCEPTANCE, self.state()["artifacts"])
        # Resuming restores ready_review; the next review request auto-accepts.
        resumed = self.resume()
        self.assertEqual(resumed["status"], "ready_review")
        result = self.review()
        self.assertEqual(result["status"], "auto_accepted")

    def test_a_wrapped_interrupt_in_the_limit_workspace_check_saves_the_pause(self):
        self.to_limit()
        # verify_workspace re-raises the interrupt a Git cleanup failure
        # wrapped; the pause must still be saved before the interrupt goes out.
        failure = ProcessCleanupError("a descendant retained output pipes")
        failure.__cause__ = KeyboardInterrupt()
        with patch("devflow_core.implement.workspace._inspect", side_effect=failure):
            with self.assertRaises(KeyboardInterrupt):
                self.review()
        state = self.state()
        self.assertEqual(state["status"], "paused_error")
        self.assertIn("retained output pipes", state["last_error"])
        self.assertEqual(len(state["attempts"]), 2)  # no attempt consumed
        self.assertNotIn(AUTO_ACCEPTANCE, state["artifacts"])

    def test_a_wrapped_interrupt_below_the_limit_is_not_a_pause(self):
        self.start()
        self.ready()
        failure = ProcessCleanupError("a descendant retained output pipes")
        failure.__cause__ = KeyboardInterrupt()
        with patch("devflow_core.implement.workspace._inspect", side_effect=failure):
            with self.assertRaises(KeyboardInterrupt):
                self.review()
        self.assertEqual(self.state()["status"], "ready_review")

    def test_a_post_review_cleanup_failure_at_the_limit_pauses(self):
        self.start()
        self.ready()
        self.response_kind = "changes_required"
        self.review()
        self.reflect()
        self.ready()
        calls = []
        original = Engine._current

        def wrapped(engine_self, state):
            calls.append(1)
            if len(calls) > 1:  # prepare's read passed; the post-review read fails
                raise ProcessCleanupError("untracked helper lingers")
            return original(engine_self, state)

        with patch.object(Engine, "_current", wrapped):
            result = self.review()
        self.assertEqual(result["status"], "paused_error")
        self.assertIn("untracked helper", result["last_error"])

    def test_an_unvalidated_checkpoint_is_never_auto_accepted(self):
        self.to_limit()
        # A committed state with validation removed cannot auto-accept.
        state = self.engine.store.read(self.run_id)
        state["validation"] = None
        atomic_write_json(self.run / "state.json", state)
        asked = self.review()
        self.assertEqual(asked["status"], "awaiting_human")
        self.assertNotIn(AUTO_ACCEPTANCE, self.state()["artifacts"])

    def test_the_ceiling_is_reached_by_auto_accept_before_exhaustion(self):
        self.to_limit()
        # Fabricate the 98-attempt, limit-98 boundary: auto-accept is decided
        # before the ceiling turns the run exhausted.
        state = self.engine.store.read(self.run_id)
        template = state["attempts"][-1]
        while len(state["attempts"]) < 98:
            state["attempts"].append(dict(template, number=len(state["attempts"]) + 1))
        state["review_limit"] = 98
        atomic_write_json(self.run / "state.json", state)
        result = self.review()
        self.assertEqual(result["status"], "auto_accepted")
        record = read_json(self.run / AUTO_ACCEPTANCE)
        self.assertEqual((record["review_count"], record["review_limit"]), (98, 98))

    def test_an_ineligible_run_at_the_ceiling_is_exhausted(self):
        self.to_limit()
        state = self.engine.store.read(self.run_id)
        template = state["attempts"][-1]
        while len(state["attempts"]) < 98:
            state["attempts"].append(dict(template, number=len(state["attempts"]) + 1))
        state["review_limit"] = 98
        state["validation"] = None  # no gate satisfied → the ceiling applies
        atomic_write_json(self.run / "state.json", state)
        result = self.review()
        self.assertEqual(result["status"], "exhausted")

    def test_a_numeric_frozen_value_is_rejected_on_read(self):
        self.to_limit()
        self.review()
        # A frozen config file holding 1 for the boolean is never "true".
        config = read_json(self.run / "config.json")
        config["auto_accept_at_review_limit"] = 1
        data = json.dumps(config).encode("utf-8")
        (self.run / "config.json").write_bytes(data)
        state = self.engine.store.read(self.run_id)
        state["artifacts"]["config.json"] = digest(data)
        atomic_write_json(self.run / "state.json", state)
        with self.assertRaises(DevFlowError):
            self.engine.read_frozen_config(self.run_id)

    def test_a_numeric_zero_for_a_frozen_false_is_rejected_on_read(self):
        self.config["auto_accept_at_review_limit"] = False
        self.write_config()
        self.start()
        # ``False == 0`` in Python: only the strict type check catches the swap.
        config = read_json(self.run / "config.json")
        config["auto_accept_at_review_limit"] = 0
        data = json.dumps(config).encode("utf-8")
        (self.run / "config.json").write_bytes(data)
        state = self.engine.store.read(self.run_id)
        state["artifacts"]["config.json"] = digest(data)
        atomic_write_json(self.run / "state.json", state)
        with self.assertRaises(DevFlowError):
            self.engine.read_frozen_config(self.run_id)

    def test_the_binary_attestation_gate_still_applies(self):
        # A candidate containing a binary change cannot reach the limit without
        # the human's attestation; once attested, auto-accept may end the run.
        self.start()
        (self.project / "asset.bin").write_bytes(b"\x00binary\xffcontent")
        self.response_kind = "changes_required"

        def attested_ready():
            asked = self.checkpoint()
            self.assertEqual(asked["status"], "awaiting_human")
            self.assertEqual(asked["escalation"]["detail"]["kind"], "binary_changes")
            with self.assertRaises(DevFlowError):
                self.engine.review(self.run_id, self.session)
            evidence = self.answer("I inspected asset.bin and approve these bytes.")
            resolved = self.engine.resolve(self.run_id, self.session, evidence, "continue",
                                           "The actual binary was inspected by the user.")
            self.assertEqual(resolved["status"], "ready_check")
            self.engine.check(self.run_id, self.session)

        attested_ready()
        for index in range(2):
            self.review()
            self.reflect()
            # A revision changes the candidate, so the attestation must be renewed.
            (self.project / f"note-{index}.txt").write_text(f"revision {index}\n", encoding="utf-8")
            attested_ready()
        result = self.review()
        self.assertEqual(result["status"], "auto_accepted")

    def test_an_interrupted_acceptance_save_is_retried_by_the_next_review(self):
        self.to_limit()
        original, failures = Engine._artifact, []

        def flaky(self, state, relative, data):
            if relative == AUTO_ACCEPTANCE and not failures:
                failures.append(1)
                raise OSError("simulated interrupted save")
            return original(self, state, relative, data)

        with patch.object(Engine, "_artifact", flaky):
            with self.assertRaises(OSError):
                self.review()
        state = self.state()
        self.assertEqual(state["status"], "ready_review")  # nothing was committed
        self.assertNotIn(AUTO_ACCEPTANCE, state["artifacts"])
        self.assertIsNone(state["report_path"])

    def test_an_interrupted_terminal_commit_leaves_the_old_state_and_retries(self):
        # The acceptance artifact reached disk but the terminal state
        # replacement failed: the committed state still governs and the next
        # review retries the acceptance cleanly.
        self.to_limit()
        original, tripped = self.engine.store.save, {"hit": False}

        def flaky(state):
            if state.get("status") == "auto_accepted" and not tripped["hit"]:
                tripped["hit"] = True
                raise OSError("simulated interrupted terminal commit")
            return original(state)

        with patch.object(self.engine.store, "save", flaky):
            with self.assertRaises(OSError):
                self.review()
        state = self.state()  # the committed state, not the interrupted one
        self.assertEqual(state["status"], "ready_review")
        self.assertNotIn(AUTO_ACCEPTANCE, state["artifacts"])
        self.assertIsNone(state["report_path"])
        result = self.review()
        self.assertEqual(result["status"], "auto_accepted")
        self.assertIn(AUTO_ACCEPTANCE, self.state()["artifacts"])

    def test_a_resume_reuses_the_saved_report_after_a_workspace_edit(self):
        self.to_limit()
        self.review()
        original = self.engine.status(self.run_id)["report"]
        self.source.write_text("VALUE = 42  # edited after the run ended\n", encoding="utf-8")
        with patch("devflow_core.implement.snapshot.capture",
                   side_effect=RuntimeError("the workspace must not be captured")):
            resumed = self.resume("another-session")
        self.assertEqual(resumed["status"], "auto_accepted")
        self.assertEqual(resumed["report"], original)
        self.assertEqual(resumed["report"]["workspace_snapshot_id_at_report"],
                         original["workspace_snapshot_id_at_report"])

    def test_modified_evidence_is_reported_and_never_verified_clean(self):
        self.to_limit()
        self.review()
        record = read_json(self.run / AUTO_ACCEPTANCE)
        record["source"] = "human"
        atomic_write_json(self.run / AUTO_ACCEPTANCE, record)
        state = self.engine.store.read(self.run_id)
        with self.assertRaises(DevFlowError):
            self.engine.store.verify_artifacts(state)
        status = self.engine.status(self.run_id)
        self.assertEqual(status["status"], "auto_accepted")
        self.assertIsNone(status["review_summary"])
        self.assertIn("cannot be built", status["review_summary_error"])
        # The saved report is immutable, but a tampered record is never
        # presented as the basis of acceptance.
        self.assertIsNone(status["report"]["auto_acceptance"])
        self.assertIn("auto-acceptance", status["report"]["auto_acceptance_error"])


class GitAutoAcceptTests(EngineFixture):
    """The cleanup pause at the limit exercised end to end through real Git calls."""

    def setUp(self):
        super().setUp()
        self.config["auto_accept_at_review_limit"] = True
        self.config["max_reviews"] = 2

    def git(self, *args):
        environment = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        environment.pop("GIT_DIR", None)
        environment.pop("GIT_WORK_TREE", None)
        return subprocess.run([shutil.which("git"), *args], cwd=self.project, env=environment,
                              capture_output=True, check=True, text=True, timeout=15).stdout

    def clean_git(self):
        self.config["snapshot"]["mode"] = "git"
        self.write_config()
        self.git("init", "--quiet")
        self.git("add", "--all")
        self.git("-c", "user.name=DevFlow offline tests", "-c", "user.email=tests@example.invalid",
                 "commit", "--quiet", "-m", "Fixture baseline")

    def to_limit(self):
        """Two handled reviews at max_reviews 2 in git mode; ready_review at the limit."""
        self.clean_git()
        self.start()
        self.ready()
        self.response_kind = "changes_required"
        for _ in range(2):
            self.review()
            self.reflect()
            self.ready()
        self.assertEqual(self.state()["status"], "ready_review")
        self.assertEqual(len(self.state()["attempts"]), 2)

    def last_review_ready(self):
        """A run whose next (and last allowed) review lands exactly at the limit."""
        self.to_limit()
        state = self.engine.store.read(self.run_id)
        state["review_limit"] = 4  # the limit grows only in whole reviewer rounds
        atomic_write_json(self.run / "state.json", state)
        self.review()
        self.reflect()
        self.ready()
        self.assertEqual(self.state()["status"], "ready_review")
        self.assertEqual(len(self.state()["attempts"]), 3)

    def wrapped_interrupt(self):
        failure = ProcessCleanupError("a descendant retained output pipes")
        failure.__cause__ = KeyboardInterrupt()
        return failure

    def test_a_git_wrapped_interrupt_in_the_limit_workspace_check_saves_the_pause(self):
        self.to_limit()
        # Injected at the real Git call under verify_workspace; the interrupt
        # reaches review() bare, and the pause must be saved before it does.
        with patch("devflow_core.common.git.run_bounded", side_effect=self.wrapped_interrupt()):
            with self.assertRaises(KeyboardInterrupt):
                self.review()
        state = self.state()
        self.assertEqual(state["status"], "paused_error")
        self.assertIn("retained output pipes", state["last_error"])
        self.assertEqual(len(state["attempts"]), 2)  # no attempt consumed
        self.assertNotIn(AUTO_ACCEPTANCE, state["artifacts"])
        self.assertIsNone(state["report_path"])

    def test_a_git_cleanup_failure_after_the_last_review_pauses(self):
        self.last_review_ready()
        armed = {"on": False}
        original_respond = self.respond
        real_bounded = git_module.run_bounded

        def respond_then_arm(*args, **kwargs):
            armed["on"] = True  # the reviewer ran; the post-review check fails
            return original_respond(*args, **kwargs)

        def flaky(*args, **kwargs):
            if armed["on"]:
                raise ProcessCleanupError("a descendant retained output pipes")
            return real_bounded(*args, **kwargs)

        self.reviewer.side_effect = respond_then_arm
        with patch("devflow_core.common.git.run_bounded", side_effect=flaky):
            result = self.review()
        self.assertEqual(result["status"], "paused_error")
        self.assertIn("retained output pipes", result["last_error"])
        self.assertEqual(len(self.state()["attempts"]), 4)  # the dispatched attempt stayed consumed
        self.assertNotIn(AUTO_ACCEPTANCE, self.state()["artifacts"])

    def test_a_git_retained_pipes_result_after_the_last_review_pauses(self):
        # run_git turns a completed-but-retained result into a DevFlowError
        # whose cause is the typed cleanup failure; the post-review check at
        # the limit must still pause.
        self.last_review_ready()
        armed = {"on": False}
        original_respond = self.respond
        real_bounded = git_module.run_bounded

        def respond_then_arm(*args, **kwargs):
            armed["on"] = True
            return original_respond(*args, **kwargs)

        def flaky(*args, **kwargs):
            if armed["on"]:
                return Completed(exit_code=0, stdout=b"", stderr=b"", timed_out=False,
                                 retained_pipes=True, output_error=None, duration_seconds=0.01)
            return real_bounded(*args, **kwargs)

        self.reviewer.side_effect = respond_then_arm
        with patch("devflow_core.common.git.run_bounded", side_effect=flaky):
            result = self.review()
        self.assertEqual(result["status"], "paused_error")
        self.assertIn("retained output pipes", result["last_error"])
        self.assertEqual(len(self.state()["attempts"]), 4)
        self.assertNotIn(AUTO_ACCEPTANCE, self.state()["artifacts"])

    def test_a_git_interrupt_at_the_process_wait_saves_the_pause(self):
        """A KeyboardInterrupt at the post-monitor process.wait inside a real
        Git call, with the stop unconfirmed, saves the pause and propagates."""
        self.to_limit()
        calls = {"n": 0}

        def fake_wait(self, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise KeyboardInterrupt()
            raise subprocess.TimeoutExpired(self.args, timeout)  # the stop cannot be confirmed

        with patch.object(subprocess.Popen, "wait", fake_wait):
            with self.assertRaises(KeyboardInterrupt):
                self.review()
        self.assertGreaterEqual(calls["n"], 2)  # cleanup ran and waited again
        state = self.state()
        self.assertEqual(state["status"], "paused_error")
        self.assertIn("did not terminate", state["last_error"])
        self.assertEqual(len(state["attempts"]), 2)  # no attempt consumed
        self.assertNotIn(AUTO_ACCEPTANCE, state["artifacts"])
        self.assertIsNone(state["report_path"])

    def test_a_git_interrupt_at_the_descendant_pipe_wait_saves_the_pause(self):
        """A KeyboardInterrupt at the pipe-reader grace wait inside a real Git
        call, with the stop unconfirmed, saves the pause and propagates."""
        self.to_limit()
        real_event_wait = threading.Event.wait
        real_popen_wait = subprocess.Popen.wait
        event_calls, wait_calls = {"n": 0}, {"n": 0}
        tripped = {"hit": False}

        def fake_event_wait(self, timeout=None):
            event_calls["n"] += 1
            # Thread.start's internal _started.wait also lands here; only the
            # first Event.wait after the post-monitor wait is the pipe grace.
            if wait_calls["n"] >= 1 and not tripped["hit"]:
                tripped["hit"] = True
                raise KeyboardInterrupt()
            return real_event_wait(self, timeout)

        def fake_popen_wait(self, timeout=None):
            wait_calls["n"] += 1
            if wait_calls["n"] == 1:
                return real_popen_wait(self, timeout)  # the post-monitor wait succeeds
            raise subprocess.TimeoutExpired(self.args, timeout)  # cleanup's wait: unconfirmed

        with patch.object(threading.Event, "wait", fake_event_wait), \
                patch.object(subprocess.Popen, "wait", fake_popen_wait):
            with self.assertRaises(KeyboardInterrupt):
                self.review()
        self.assertGreaterEqual(wait_calls["n"], 2)  # cleanup ran and waited again
        state = self.state()
        self.assertEqual(state["status"], "paused_error")
        self.assertIn("did not terminate", state["last_error"])
        self.assertEqual(len(state["attempts"]), 2)
        self.assertNotIn(AUTO_ACCEPTANCE, state["artifacts"])
        self.assertIsNone(state["report_path"])

    def test_a_git_wrapped_interrupt_after_a_review_keeps_the_reservation(self):
        self.last_review_ready()
        armed = {"on": False}
        original_respond = self.respond
        real_bounded = git_module.run_bounded

        def respond_then_arm(*args, **kwargs):
            armed["on"] = True
            return original_respond(*args, **kwargs)

        def flaky(*args, **kwargs):
            if armed["on"]:
                raise self.wrapped_interrupt()
            return real_bounded(*args, **kwargs)

        self.reviewer.side_effect = respond_then_arm
        with patch("devflow_core.common.git.run_bounded", side_effect=flaky):
            with self.assertRaises(KeyboardInterrupt):
                self.review()
        state = self.state()
        # The interrupt keeps the reservation in reviewing for human recovery.
        self.assertEqual(state["status"], "reviewing")
        self.assertNotIn(AUTO_ACCEPTANCE, state["artifacts"])
        self.assertIsNone(state["report_path"])


if __name__ == "__main__":
    unittest.main()
