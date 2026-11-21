"""``implement start`` checks every reviewer CLI in the workspace before it creates a run.

Tests about something else (hooks, workspaces, routing) replace that check, because the
machines that run them need not have the reviewer CLIs (CI installs none). The tests of
the check itself use the real ``preflight`` with a fake CLI.
"""
from unittest.mock import patch


def skip_reviewer_checks(test):
    """Replace ``common.reviewers.preflight`` (start and doctor reach it there) for the rest of ``test``."""
    patcher = patch("devflow_core.common.reviewers.preflight", return_value={})
    patcher.start()
    test.addCleanup(patcher.stop)
