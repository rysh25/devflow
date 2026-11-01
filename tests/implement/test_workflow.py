"""Implement workflow rules that need no run on disk."""
import unittest

from devflow_core.implement import WORKFLOW


class RecoverTests(unittest.TestCase):
    def state(self, status, attempts):
        return {"status": status, "attempts": [{"number": i + 1, "status": "succeeded"} for i in range(attempts)],
                "config": {"max_reviews": 6}, "approvals": {"codex": "x"}}

    def test_interrupted_check_always_returns_to_implementing(self):
        for attempts in (0, 5, 6):
            with self.subTest(attempts=attempts):
                state = self.state("checking", attempts)
                WORKFLOW.on_recover(None, state)
                self.assertEqual((state["status"], state["resume_status"]), ("paused_error", "implementing"))
                self.assertEqual(state["approvals"], {})

    def test_interrupted_final_review_exhausts_the_budget(self):
        state = self.state("reviewing", 6)
        WORKFLOW.on_recover(None, state)
        self.assertEqual((state["status"], state["attempts"][-1]["status"]), ("exhausted", "failed"))
        state = self.state("reviewing", 3)
        WORKFLOW.on_recover(None, state)
        self.assertEqual((state["status"], state["resume_status"]), ("paused_error", "ready_review"))


if __name__ == "__main__":
    unittest.main()
