"""Where the installed plugin, its runner and a project live.

Nothing here creates files or directories.
"""
from __future__ import annotations

import os
from pathlib import Path
import re

from .errors import DevFlowError

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PACKAGE_ROOT.parents[1]
#: What marks a project root.
PROJECT_MARKERS = (".devflow", "devflow.config.json", "devflow.config.local.json", ".git")
_MSYS_DRIVE = re.compile(r"/([A-Za-z])(?=/|\Z)")


def path_key(path) -> str:
    """A path as the OS compares it: resolved, and case-folded on Windows."""
    return os.path.normcase(str(Path(path).resolve()))


def same_path(left, right) -> bool:
    """Whether two paths name the same location, compared the way the OS does
    (both resolved; case-folded on Windows)."""
    return path_key(left) == path_key(right)


def project_root(start: Path) -> Path:
    """The nearest directory at or above ``start`` holding a project marker."""
    current = Path(start).resolve()
    if not current.is_dir():
        raise DevFlowError("Project directory does not exist")
    for directory in (current, *current.parents):
        if any((directory / marker).exists() for marker in PROJECT_MARKERS):
            return directory
    return current


def hook_project(cwd: str | None, environ, variable: str | None, *, from_cwd: bool = True) -> Path:
    """The project a hook acts for.

    ``variable`` is the host's environment variable naming the project (the
    host table's ``project_variable``). Claude Code sets ``CLAUDE_PROJECT_DIR``
    to the directory where the session started; it stays fixed after a ``cd``
    or a move into a worktree, so the hook keeps finding the run that session
    works on. Devin sets ``DEVIN_PROJECT_DIR`` to the project root and sends no
    ``cwd``. When the variable is unset or empty and ``from_cwd`` allows it, the
    project is found from the hook's ``cwd``. A non-empty value that is not an
    absolute existing directory is an error.
    """
    value = environ.get(variable) if variable and environ is not None else None
    if not value:
        if not from_cwd or not isinstance(cwd, str) or not cwd:
            raise DevFlowError(f"Hook requires {variable or 'its cwd'} to name the project")
        return project_root(Path(cwd))
    if os.name == "nt":
        value = msys_path(value)
    start = Path(value)
    if not start.is_absolute():
        raise DevFlowError(f"{variable} must be an absolute directory")
    if not start.is_dir():
        raise DevFlowError(f"{variable} does not name an existing directory")
    return project_root(start)


def msys_path(value: str) -> str:
    """Git Bash's ``/c/Users/x`` as ``C:/Users/x``; other text is unchanged.

    Pure string handling, usable on every OS; callers apply it only to Windows
    paths.
    """
    match = _MSYS_DRIVE.match(value)
    if not match:
        return value
    return match.group(1).upper() + ":" + (value[match.end():] or "/")
