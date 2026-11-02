"""One bounded way to run read-only git commands, shared by every caller.

The environment drops every ``GIT_*`` variable and ignores the system and
global configuration, so the user's environment cannot redirect the
repository or enable programs through those files. Repository configuration
still applies; callers that must not start programs add their own ``-c``
overrides and command flags.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil

from .errors import DevFlowError
from .process import run_bounded

#: Arguments every call gets: no pager, no index refresh, no fsmonitor hook.
BASE_ARGS = ("--no-pager", "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false")
DEFAULT_LIMIT = 8 * 1024 * 1024


def git_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0"})
    env.update(extra or {})
    return env


def git_executable() -> str:
    executable = shutil.which("git")
    if not executable:
        raise DevFlowError("Cannot inspect Git repository: git executable not found")
    if Path(executable).suffix.casefold() in {".cmd", ".bat", ".ps1"}:
        raise DevFlowError(f"Cannot inspect Git repository: PATH resolved git to a shell shim: {executable}")
    return executable


def run_git(root: Path, *args: str, ok_codes: tuple[int, ...] = (0,), timeout: float = 30,
            limit: int = DEFAULT_LIMIT, extra_env: dict[str, str] | None = None,
            config: tuple[str, ...] = (), new_session: bool = True) -> bytes:
    """Run ``git -C root ARGS`` and return stdout; any failure raises DevFlowError.

    ``config`` holds extra ``key=value`` settings passed as ``-c``.
    ``new_session`` is passed to ``run_bounded`` (see there).
    """
    command = [git_executable(), *BASE_ARGS]
    for setting in config:
        command.extend(["-c", setting])
    command.extend(["-C", str(root), *args])
    result = run_bounded(command, cwd=Path(root), timeout=timeout, limit=limit,
                         env=git_environment(extra_env), new_session=new_session)
    if result.timed_out:
        raise DevFlowError(f"Cannot inspect Git repository: git exceeded {timeout:g} seconds")
    if result.output_limit_exceeded:
        raise DevFlowError(f"Cannot inspect Git repository: output exceeded {limit} bytes")
    if result.retained_pipes:
        raise DevFlowError("Cannot inspect Git repository: a descendant retained output pipes")
    if result.read_error:
        raise DevFlowError("Cannot inspect Git repository: " + result.read_error)
    if result.exit_code not in ok_codes:
        message = result.stderr.decode("utf-8", "replace").strip()[:1000]
        raise DevFlowError(f"Git inspection failed ({result.exit_code}): {message}")
    return result.stdout
