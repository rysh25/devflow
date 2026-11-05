"""Implement workflow rules that need no run on disk."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from devflow_core.implement import WORKFLOW

# A run directory without review process records: no token counts are found.
STORE = SimpleNamespace(run_dir=lambda run_id: Path(tempfile.gettempdir()) / "devflow-no-such-run" / run_id)


class RecoverTests(unittest.TestCase):
    def state(self, status, attempts):
        return {"run_id": "20261105T000000Z-000000000000", "status": status,
                "attempts": [{"number": i + 1, "status": "succeeded", "reviewer_id": "codex"} for i in range(attempts)],
                "config": {"max_reviews": 6, "reviewers": [{"id": "codex", "kind": "codex"}]},
                "review_limit": 6, "approvals": {"codex": "x"}}

    def test_interrupted_check_always_returns_to_implementing(self):
        for attempts in (0, 5, 6):
            with self.subTest(attempts=attempts):
                state = self.state("checking", attempts)
                WORKFLOW.on_recover(STORE, state)
                self.assertEqual((state["status"], state["resume_status"]), ("paused_error", "implementing"))
                self.assertEqual(state["approvals"], {})

    def test_interrupted_final_review_leads_to_the_limit_question(self):
        # No attempt is left: the next review request asks the human (no retry pause).
        state = self.state("reviewing", 6)
        WORKFLOW.on_recover(STORE, state)
        self.assertEqual((state["status"], state["attempts"][-1]["status"]), ("ready_review", "failed"))
        state = self.state("reviewing", 3)
        WORKFLOW.on_recover(STORE, state)
        self.assertEqual((state["status"], state["resume_status"]), ("paused_error", "ready_review"))

    def test_interrupted_review_keeps_other_approvals(self):
        # Like any reviewer failure; an interrupted check still clears them (above).
        for attempts in (3, 6):
            with self.subTest(attempts=attempts):
                state = self.state("reviewing", attempts)
                WORKFLOW.on_recover(STORE, state)
                self.assertEqual(state["approvals"], {"codex": "x"})


if __name__ == "__main__":
    unittest.main()
