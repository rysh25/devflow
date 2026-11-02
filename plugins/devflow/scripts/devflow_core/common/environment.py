"""Read-only facts about the environment DevFlow runs in.

Claude Code ends a foreground Bash call at its own limit. A reviewer or the
checks must finish inside that call, or the run is left in flight and needs a
human ``recover`` (the attempt stays consumed). DevFlow reports the Bash
timeout each long action needs and warns when Claude Code's limit is shorter.
"""
from __future__ import annotations

import os
import sys
from typing import Mapping

#: Time the runner itself needs around a reviewer or the checks.
MARGIN_SECONDS = 60
#: Claude Code's defaults (BASH_MAX_TIMEOUT_MS, BASH_DEFAULT_TIMEOUT_MS).
BASH_MAX_DEFAULT_MS = 600000
BASH_DEFAULT_DEFAULT_MS = 120000
LIMIT_WARNING = ("Claude Code's Bash limit ({limit} ms = max of BASH_MAX_TIMEOUT_MS={max_value} and "
                 "BASH_DEFAULT_TIMEOUT_MS={default_value}) is shorter than DevFlow needs for {action} ({needed} ms). "
                 "Set BASH_MAX_TIMEOUT_MS to at least {needed} in Claude Code settings (env), or lower timeout_seconds.")


def bash_timeout_ms(seconds: int) -> int:
    """The Bash timeout for a runner action whose external process may take ``seconds``."""
    return (int(seconds) + MARGIN_SECONDS) * 1000


def _milliseconds(value: str | None) -> int | None:
    return int(value) if isinstance(value, str) and value.isdigit() else None


def bash_limit(environ: Mapping[str, str] | None = None) -> tuple[int, str, str]:
    """Claude Code's effective Bash limit: the larger of its two settings."""
    env = os.environ if environ is None else environ
    raw_max, raw_default = env.get("BASH_MAX_TIMEOUT_MS"), env.get("BASH_DEFAULT_TIMEOUT_MS")
    max_value, default_value = _milliseconds(raw_max), _milliseconds(raw_default)
    limit = max(BASH_MAX_DEFAULT_MS if max_value is None else max_value,
                BASH_DEFAULT_DEFAULT_MS if default_value is None else default_value)
    return limit, raw_max if raw_max is not None else "unset", raw_default if raw_default is not None else "unset"


def limit_warnings(needs: Mapping[str, int], environ: Mapping[str, str] | None = None) -> list[str]:
    """One warning per long action whose Bash timeout exceeds Claude Code's limit."""
    limit, max_value, default_value = bash_limit(environ)
    return [LIMIT_WARNING.format(limit=limit, max_value=max_value, default_value=default_value,
                                 action=action, needed=needed)
            for action, needed in sorted(needs.items()) if needed > limit]


def python_errors(platform: str | None = None, executable: str | None = None) -> list[str]:
    """MSYS2/Cygwin Python on Windows reads paths differently; DevFlow needs a native one."""
    platform = sys.platform if platform is None else platform
    if platform in ("cygwin", "msys"):
        return [f"On Windows, DevFlow needs a native Windows Python; uv selected {platform} Python at "
                f"{executable or sys.executable}"]
    return []
