"""Where the installed plugin, its runner and a project live.

Nothing here creates files or directories.
"""
from __future__ import annotations

from pathlib import Path
import re

from .errors import DevFlowError

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PACKAGE_ROOT.parents[1]
RUNNER = PLUGIN_ROOT / "scripts" / "devflow.py"
#: What marks a project root; guarded writes may never create these (see guard).
PROJECT_MARKERS = (".devflow", "devflow.config.json", ".git")
_MSYS_DRIVE = re.compile(r"/([A-Za-z])(?=/|\Z)")


def project_root(start: Path) -> Path:
    """The nearest directory at or above ``start`` holding a project marker."""
    current = Path(start).resolve()
    if not current.is_dir():
        raise DevFlowError("Project directory does not exist")
    for directory in (current, *current.parents):
        if any((directory / marker).exists() for marker in PROJECT_MARKERS):
            return directory
    return current


def msys_path(value: str) -> str:
    """Git Bash's ``/c/Users/x`` as ``C:/Users/x``; other text is unchanged.

    Pure string handling, usable on every OS; callers apply it only to Windows
    paths.
    """
    match = _MSYS_DRIVE.match(value)
    if not match:
        return value
    return match.group(1).upper() + ":" + (value[match.end():] or "/")
