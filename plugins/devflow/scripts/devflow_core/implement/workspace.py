"""Where an implementation run's code lives: the project or a worktree of its repository.

The workspace is chosen once, at start, and frozen in the run state. Every
later use re-checks it, including its directory identity, so a worktree that
was removed and recreated at the same path is noticed.
"""
from __future__ import annotations

import os
from pathlib import Path
import re

from ..common.errors import DevFlowError
from ..common.git import git_environment, git_executable, run_git
from ..common.paths import msys_path, same_path
from ..common.process import run_bounded
from ..common.storage import no_symlinks

#: ``rev-parse --path-format`` appeared in Git 2.31.
MINIMUM_GIT = (2, 31)
FIELDS = frozenset({"path", "git_common_dir", "git_dir", "identity"})


def git_version() -> tuple[int, ...]:
    result = run_bounded([git_executable(), "--version"], cwd=None, timeout=30, limit=65536,
                         env=git_environment())
    match = re.search(r"(\d+)\.(\d+)", result.stdout.decode("utf-8", "replace"))
    if result.exit_code != 0 or not match:
        raise DevFlowError("Cannot establish the Git version")
    return tuple(int(part) for part in match.groups())


def _rev_parse(root: Path, *args: str) -> list[str]:
    """One ``git rev-parse`` call; one non-empty UTF-8 line per requested value."""
    raw = run_git(root, "rev-parse", *args)
    try:
        lines = raw.decode("utf-8", "strict").splitlines()
    except UnicodeError as exc:  # Reported like every other workspace problem.
        raise DevFlowError("Git reported a path that is not valid UTF-8") from exc
    expected = sum(1 for arg in args if not arg.startswith("--path-format"))
    if len(lines) != expected or not all(lines):
        raise DevFlowError(f"Unexpected git rev-parse output for {' '.join(args)}")
    return lines


def _describe(path: Path) -> tuple[str, str, str]:
    """Top directory, common git directory and git directory, in one git process."""
    top, common, git_dir = _rev_parse(path, "--show-toplevel", "--path-format=absolute", "--git-common-dir",
                                      "--absolute-git-dir")
    return top, common, git_dir


def identity(path: Path) -> list[int] | None:
    """``[st_dev, st_ino]`` of a directory, or None where the file system has no stable inode."""
    info = os.stat(path)
    return [info.st_dev, info.st_ino] if info.st_ino else None


def _directory(project: Path, raw: str | None) -> Path:
    """The workspace directory named by ``raw`` (default: the project), resolved."""
    if raw in (None, ""):
        return project
    value = msys_path(raw) if os.name == "nt" else raw
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = project / candidate
    candidate = Path(os.path.abspath(candidate))
    try:
        no_symlinks(candidate)
    except DevFlowError as exc:
        raise DevFlowError(f"The workspace path must not pass through a symlink or junction: {raw}") from exc
    if not candidate.is_dir():
        raise DevFlowError(f"The workspace is not an existing directory: {raw}")
    return candidate.resolve(strict=True)


def _inspect(project: Path, path: Path, mode: str) -> dict:
    """The checks made at start and again at every later use.

    In files mode the workspace is the project itself; in git mode it is the
    top directory of the project or of a worktree of the same repository.
    Both are checked against the real project, never only against the state.
    """
    if mode == "files":
        if not same_path(path, project):
            raise DevFlowError("In files mode the workspace must be the project itself")
        return {"path": str(path), "git_common_dir": None, "git_dir": None}
    top, common, git_dir = _describe(path)
    if not same_path(top, path):
        raise DevFlowError("The workspace must be the top directory of the project or of one of its worktrees")
    (project_common,) = _rev_parse(project, "--path-format=absolute", "--git-common-dir")
    if not same_path(common, project_common):
        raise DevFlowError("The workspace must be the project or a worktree of the same repository")
    return {"path": str(path), "git_common_dir": str(Path(common).resolve()), "git_dir": str(Path(git_dir).resolve())}


def resolve_workspace(project: Path, raw: str | None, mode: str) -> dict:
    """Validate and describe the workspace named at start (default: the project)."""
    project = Path(project).resolve(strict=True)
    path = _directory(project, raw)
    # The Git version is checked once, here; later checks rely on it.
    if mode != "files" and git_version() < MINIMUM_GIT:
        raise DevFlowError("Implementation workspaces need Git 2.31 or later (git rev-parse --path-format)")
    return {**_inspect(project, path, mode), "identity": identity(path)}


def check_frozen(value: object) -> None:
    """Shape of the frozen workspace record in the run state."""
    if (not isinstance(value, dict) or set(value) != FIELDS or not isinstance(value["path"], str)
            or not all(value[key] is None or isinstance(value[key], str) for key in ("git_common_dir", "git_dir"))
            or not (value["identity"] is None or (isinstance(value["identity"], list) and len(value["identity"]) == 2
                                                  and all(type(part) is int for part in value["identity"])))):
        raise DevFlowError("Corrupt implementation workspace record in the run state")


def verify_workspace(project: Path, frozen: dict, mode: str) -> Path:
    """Repeat the start checks against the real project and return the path.

    Any difference from the frozen record is an error. Two git processes at
    most (``rev-parse`` in the workspace and in the project).
    """
    check_frozen(frozen)
    try:
        project = Path(project).resolve(strict=True)
        path = _directory(project, frozen["path"])
        current = _inspect(project, path, mode)
    except (DevFlowError, OSError) as exc:
        raise DevFlowError(f"The implementation workspace is no longer valid: {exc}") from exc
    for key in ("path", "git_common_dir", "git_dir"):
        expected, actual = frozen[key], current[key]
        if (expected is None) != (actual is None) or (expected is not None and not same_path(expected, actual)):
            raise DevFlowError(f"The implementation workspace changed since start ({key}); start a new run")
    if frozen["identity"] is not None and identity(path) != frozen["identity"]:
        raise DevFlowError("The implementation workspace was removed and recreated since start; start a new run")
    return path
