"""Facts about the environment DevFlow runs in.

Claude passes the Bash timeout DevFlow reports for ``review`` and ``check``: the
external process's own limit, the work before it, and a margin. When it is
larger than Claude Code's limit for foreground commands, Claude runs the call in
the background with that timeout and waits for its completion notice, so DevFlow
does not depend on that limit.
"""
from __future__ import annotations

import sys

#: Time the runner itself needs around a reviewer or the checks.
MARGIN_SECONDS = 60
#: Limit for each local probe of a reviewer CLI (``--version``, then its subcommand's ``--help``).
PROBE_SECONDS = 15
#: Limit for the git call that describes the implementation workspace for the reviewer.
GIT_FACT_SECONDS = 10
#: Work in the review call before the reviewer starts: two probes and the workspace facts.
REVIEW_SETUP_SECONDS = 2 * PROBE_SECONDS + GIT_FACT_SECONDS


def bash_timeout_ms(seconds: int) -> int:
    """The Bash timeout for a runner action whose external process may take ``seconds``."""
    return (int(seconds) + MARGIN_SECONDS) * 1000


def review_timeout_ms(reviewer_seconds: int) -> int:
    """The Bash timeout for ``review``: the reviewer, the work before it, and the margin."""
    return bash_timeout_ms(int(reviewer_seconds) + REVIEW_SETUP_SECONDS)


def python_errors(platform: str | None = None, executable: str | None = None) -> list[str]:
    """MSYS2/Cygwin Python on Windows reads paths differently; DevFlow needs a native one."""
    platform = sys.platform if platform is None else platform
    if platform in ("cygwin", "msys"):
        return [f"On Windows, DevFlow needs a native Windows Python; uv selected {platform} Python at "
                f"{executable or sys.executable}"]
    return []
