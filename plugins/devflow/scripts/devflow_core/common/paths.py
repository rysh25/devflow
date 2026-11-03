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
RUNNER = PLUGIN_ROOT / "scripts" / "devflow.py"
#: What marks a project root.
PROJECT_MARKERS = (".devflow", "devflow.config.json", ".git")
_MSYS_DRIVE = re.compile(r"/([A-Za-z])(?=/|\Z)")


def same_path(left, right) -> bool:
    """Whether two paths name the same location, compared the way the OS does
    (both resolved; case-folded on Windows)."""
    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(str(Path(right).resolve()))


def project_root(start: Path) -> Path:
    """The nearest directory at or above ``start`` holding a project marker."""
    current = Path(start).resolve()
    if not current.is_dir():
        raise DevFlowError("Project directory does not exist")
    for directory in (current, *current.parents):
        if any((directory / marker).exists() for marker in PROJECT_MARKERS):
            return directory
    return current


def hook_project(cwd: Path, environ) -> Path:
    """The project a hook acts for.

    Claude Code sets ``CLAUDE_PROJECT_DIR`` to the directory where the session
    started; it stays fixed after a ``cd`` or a move into a worktree, so the
    hook keeps finding the run that session works on. When it is unset or empty,
    the project is found from the hook's ``cwd`` (older behaviour). A non-empty
    value that is not an absolute existing directory is an error.
    """
    value = environ.get("CLAUDE_PROJECT_DIR") if environ is not None else None
    if not value:
        return project_root(Path(cwd))
    if os.name == "nt":
        value = msys_path(value)
    start = Path(value)
    if not start.is_absolute():
        raise DevFlowError("CLAUDE_PROJECT_DIR must be an absolute directory")
    if not start.is_dir():
        raise DevFlowError("CLAUDE_PROJECT_DIR does not name an existing directory")
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
