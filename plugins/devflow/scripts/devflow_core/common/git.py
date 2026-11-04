"""One bounded way to run DevFlow's own git commands, shared by every caller.

DevFlow's git commands see the repository as the user's own git does: the
user's system, global and repository configuration all apply (for example
``core.autocrlf``, ``core.excludesFile`` and ``safe.directory``), including
configuration files the user selects with ``GIT_CONFIG_GLOBAL``,
``GIT_CONFIG_SYSTEM`` or ``GIT_CONFIG_NOSYSTEM``. The other ``GIT_*``
variables are dropped, so one such as ``GIT_DIR`` (set inside git hooks, for
example) cannot point a command at another repository than the one named.
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


#: The variables that choose the user's configuration files; they are kept.
CONFIG_FILE_VARIABLES = frozenset({"GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"})


def git_environment() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_") or key in CONFIG_FILE_VARIABLES}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"})
    return env


def git_executable() -> str:
    executable = shutil.which("git")
    if not executable:
        raise DevFlowError("Cannot inspect Git repository: git executable not found")
    if Path(executable).suffix.casefold() in {".cmd", ".bat", ".ps1"}:
        raise DevFlowError(f"Cannot inspect Git repository: PATH resolved git to a shell shim: {executable}")
    return executable


def run_git(root: Path, *args: str, ok_codes: tuple[int, ...] = (0,), timeout: float = 30,
            limit: int = DEFAULT_LIMIT) -> bytes:
    """Run ``git -C root ARGS`` and return stdout; any failure raises DevFlowError."""
    command = [git_executable(), *BASE_ARGS, "-C", str(root), *args]
    result = run_bounded(command, cwd=Path(root), timeout=timeout, limit=limit, env=git_environment())
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
