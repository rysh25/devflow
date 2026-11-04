"""Deterministic, bounded snapshots of an explicitly scoped local worktree.

No checkout, reset, add, commit, Git filters, or external diff driver is used.
Snapshots contain local file bytes, including changes present before a run.
"""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

from ..common.git import run_git
from ..common.errors import DevFlowError


DEFAULT_EXCLUDES = [
    ".git/", ".devflow/", "devflow.config.json", ".env", ".env.*",
    ".ssh/", ".aws/", ".azure/", "secrets/", "*.pem", "*.key",
    "*.p12", "*.pfx", "node_modules/", ".venv/", "venv/", "__pycache__/",
    ".pytest_cache/", ".mypy_cache/", ".ruff_cache/", ".next/", ".nuxt/",
    "Library/", "Temp/", "Obj/", "Logs/", "UserSettings/", "bin/", "obj/",
]
_ALWAYS_EXCLUDED = {".git", ".devflow", "devflow.config.json"}
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$", re.I)


def validate_relative(relative: str) -> str:
    """Accept a portable, normalised project-relative file path only."""
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
        raise DevFlowError("File paths must be portable project-relative POSIX paths.")
    if relative.startswith("/") or any(ord(char) < 32 for char in relative):
        raise DevFlowError(f"Unsafe file path: {relative!r}")
    parts = relative.split("/")
    if any(part in {"", ".", ".."} or part.endswith((".", " ")) or _RESERVED.match(part) for part in parts):
        raise DevFlowError(f"Non-portable file path: {relative!r}")
    return relative


def _settings(config: dict) -> dict:
    value = config.get("snapshot", config)
    if not isinstance(value, dict):
        raise DevFlowError("snapshot must be an object.")
    result = {
        "mode": "git", "include": ["**"], "exclude": [],
        "max_files": 5000, "max_file_bytes": 1_000_000,
        "max_total_bytes": 20_000_000, "max_packet_bytes": 2_000_000,
        "context_paths": [],
    }
    result.update(value)
    if result["mode"] not in {"git", "files"}:
        raise DevFlowError("snapshot.mode must explicitly be git or files.")
    for name in ("include", "exclude", "context_paths"):
        if not isinstance(result[name], list) or any(not isinstance(item, str) or not item for item in result[name]):
            raise DevFlowError(f"snapshot.{name} must be an array of nonempty strings.")
    if not result["include"]:
        raise DevFlowError("snapshot.include cannot be empty.")
    for pattern in [*result["include"], *result["exclude"]]:
        if pattern.startswith("/") or "\\" in pattern or ":" in pattern or ".." in pattern.split("/") or any(ord(c) < 32 for c in pattern):
            raise DevFlowError(f"Unsafe snapshot glob: {pattern!r}")
    for context in result["context_paths"]:
        validate_relative(context)
    for name in ("max_files", "max_file_bytes", "max_total_bytes", "max_packet_bytes"):
        if type(result[name]) is not int or result[name] < 1:
            raise DevFlowError(f"snapshot.{name} must be a positive integer.")
    return result


def _matches(path: str, pattern: str, *, insensitive: bool = False) -> bool:
    """Globs use '/', with '**/' also allowing zero directory components.

    A bare directory pattern ending in '/' matches that directory at any depth.
    A non-directory bare pattern matches any path component (e.g. '.env').
    """
    if insensitive:
        path, pattern = path.casefold(), pattern.casefold()
    parts = path.split("/")
    if pattern.endswith("/"):
        pattern = pattern[:-1]
        if "/" not in pattern:
            return any(fnmatch.fnmatchcase(part, pattern) for part in parts)
        return any(_matches("/".join(parts[:i]), pattern) for i in range(1, len(parts) + 1))
    if "/" not in pattern:
        return fnmatch.fnmatchcase(path, pattern) or any(fnmatch.fnmatchcase(part, pattern) for part in parts)
    if fnmatch.fnmatchcase(path, pattern):
        return True
    if pattern.startswith("**/"):
        return _matches(path, pattern[3:])
    return False


def _excluded_reason(relative: str, settings: dict) -> str | None:
    if any(part.casefold() in _ALWAYS_EXCLUDED for part in relative.split("/")):
        return "internal_metadata"
    for pattern in DEFAULT_EXCLUDES:
        if _matches(relative, pattern, insensitive=True):
            return f"built_in_exclude:{pattern}"
    for pattern in settings["exclude"]:
        if _matches(relative, pattern, insensitive=True):
            return f"configured_exclude:{pattern}"
    return None


def path_in_scope(relative: str, config: dict) -> bool:
    relative = validate_relative(relative)
    settings = _settings(config)
    return _excluded_reason(relative, settings) is None and any(_matches(relative, pattern) for pattern in settings["include"])


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _safe_path(root: Path, relative: str) -> Path:
    validate_relative(relative)
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        if _is_link(info):
            raise DevFlowError(f"Symlinks and junctions are not snapshot inputs: {relative}")
    if not current.resolve().is_relative_to(root):
        raise DevFlowError(f"Path leaves the project: {relative}")
    return root.joinpath(*PurePosixPath(relative).parts)


def _scoped_status(raw: bytes, settings: dict) -> list[dict]:
    """Parse porcelain-v1 -z, including the extra source path for renames."""
    entries = raw.split(b"\0")
    result = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        if len(entry) < 4 or entry[2:3] != b" ":
            raise DevFlowError("Cannot parse Git status safely.")
        code = entry[:2].decode("ascii", "strict")
        path = entry[3:].decode("utf-8", "strict")
        paths = [path]
        if any(letter in code for letter in "RC"):
            if index >= len(entries) or not entries[index]:
                raise DevFlowError("Missing Git rename source path.")
            paths.append(entries[index].decode("utf-8", "strict"))
            index += 1
        in_scope = False
        for relative in paths:
            relative = relative.rstrip("/")
            if relative and path_in_scope(relative, settings):
                in_scope = True
        if in_scope:
            result.append({"code": code, "path": path, "source": paths[1] if len(paths) > 1 else None})
    return result


def _git_inventory(root: Path, settings: dict) -> tuple[list[tuple[str, str | None]], dict]:
    try:
        top = run_git(root, "rev-parse", "--show-toplevel").decode("utf-8", "strict").strip()
        if Path(top).resolve() != root:
            raise DevFlowError("snapshot.mode=git requires the exact repository root; parent repositories are not selected implicitly.")
        head_raw = run_git(root, "rev-parse", "--verify", "HEAD", ok_codes=(0, 128))
        head = head_raw.decode("ascii", "strict").strip() or None
        candidates: dict[str, str | None] = {}
        for entry in run_git(root, "ls-files", "--stage", "-z").split(b"\0"):
            if not entry:
                continue
            prefix, raw_path = entry.split(b"\t", 1)
            mode, _oid, stage = prefix.decode("ascii").split(" ")
            if stage != "0":
                raise DevFlowError("Unmerged Git index entries require human resolution before an implementation run starts.")
            candidates[raw_path.decode("utf-8", "strict")] = mode
        for raw_path in run_git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0"):
            if raw_path:
                candidates[raw_path.decode("utf-8", "strict")] = None
        status = _scoped_status(run_git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all"), settings)
    except (UnicodeError, ValueError) as exc:
        raise DevFlowError("Git paths must have valid UTF-8 names and a readable index/status.") from exc
    return sorted(candidates.items()), {
        "head": head, "dirty": bool(status), "status": status,
        "scope": "Dirty status and file contents cover included tracked and non-ignored untracked paths only; ignored untracked paths are not scanned.",
    }


def _file_inventory(root: Path, settings: dict, excluded: list[dict]) -> list[tuple[str, str | None]]:
    candidates: list[tuple[str, str | None]] = []
    todo = [root]
    visited = 0
    while todo:
        directory = todo.pop()
        try:
            with os.scandir(directory) as scanned:
                entries = []
                for entry in scanned:
                    if visited + len(entries) >= max(settings["max_files"] * 20, 1000):
                        raise DevFlowError("Project traversal exceeds its bounded entry budget; narrow include/exclude scope.")
                    entries.append(entry)
                entries.sort(key=lambda entry: entry.name)
        except OSError as exc:
            raise DevFlowError(f"Cannot enumerate project directory: {directory}") from exc
        for entry in entries:
            visited += 1
            if visited > max(settings["max_files"] * 20, 1000):
                raise DevFlowError("Project traversal exceeds its bounded entry budget; narrow include/exclude scope.")
            relative = Path(entry.path).relative_to(root).as_posix()
            validate_relative(relative)
            reason = _excluded_reason(relative, settings)
            if reason:
                excluded.append({"path": relative, "reason": reason})
                continue
            info = entry.stat(follow_symlinks=False)
            if _is_link(info):
                raise DevFlowError(f"Symlinks and junctions are not snapshot inputs: {relative}")
            if stat.S_ISDIR(info.st_mode):
                if (Path(entry.path) / ".git").exists():
                    raise DevFlowError(f"Nested repositories require separate implementation runs: {relative}")
                todo.append(Path(entry.path))
            elif stat.S_ISREG(info.st_mode):
                candidates.append((relative, None))
            else:
                raise DevFlowError(f"Only regular files are supported: {relative}")
    return sorted(candidates)


def _fingerprint(info: os.stat_result) -> tuple:
    """Everything that shows a change, for two results of the same call (lstat or fstat)."""
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode)


def _content(info: os.stat_result) -> tuple:
    """Size and modification time: what lstat and fstat both report the same way.

    The other fields can differ between the two calls for an unchanged file
    (for example on Windows with Python 3.12 and later).
    """
    return (info.st_size, info.st_mtime_ns)


def _read_file(path: Path, limit: int, git_mode: str | None) -> dict:
    before = path.lstat()
    if _is_link(before) or not stat.S_ISREG(before.st_mode):
        raise DevFlowError(f"Snapshot requires a regular non-link file: {path}")
    if before.st_size > limit:
        raise DevFlowError(f"Snapshot file exceeds {limit} bytes: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if _content(opened) != _content(before):
            raise DevFlowError(f"File changed while snapshot opened it: {path}")
        data = stream.read(limit + 1)
        after_read = os.fstat(stream.fileno())
    after = path.lstat()
    # Each call is compared with itself in full, and across the two calls by content only.
    if (len(data) > limit or _fingerprint(opened) != _fingerprint(after_read)
            or _fingerprint(before) != _fingerprint(after) or _content(after_read) != _content(after)):
        raise DevFlowError(f"File changed while snapshot read it: {path}")
    try:
        text = data.decode("utf-8") if b"\0" not in data else None
    except UnicodeDecodeError:
        text = None
    mode = "100755" if before.st_mode & 0o111 else "100644"
    if os.name == "nt" and git_mode in {"100644", "100755"}:
        mode = git_mode
    return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "mode": mode, "text": text, "binary": text is None, "_stat": _fingerprint(after)}


def capture(project: Path, config: dict) -> dict:
    """Capture included bytes, failing instead of silently truncating coverage."""
    settings = _settings(config)
    try:
        if _is_link(Path(project).lstat()):
            raise DevFlowError("The project directory itself cannot be a symlink or junction.")
        root = Path(project).resolve(strict=True)
    except OSError as exc:
        raise DevFlowError(f"Cannot resolve project: {project}") from exc
    if not root.is_dir():
        raise DevFlowError(f"Project is not a directory: {root}")
    excluded: list[dict] = []
    git = None
    if settings["mode"] == "git":
        candidates, git = _git_inventory(root, settings)
    else:
        candidates = _file_inventory(root, settings, excluded)
    files: dict[str, dict] = {}
    fingerprints: dict[str, tuple] = {}
    case_paths: dict[str, str] = {}
    total_bytes = 0
    for relative, git_mode in candidates:
        if relative.endswith("/"):
            if _excluded_reason(relative.rstrip("/"), settings):
                excluded.append({"path": relative, "reason": "excluded_nested_repository"})
                continue
            raise DevFlowError(f"Nested repositories require separate implementation runs: {relative}")
        validate_relative(relative)
        reason = _excluded_reason(relative, settings)
        if reason is None and not any(_matches(relative, pattern) for pattern in settings["include"]):
            reason = "outside_include"
        if reason is not None:
            excluded.append({"path": relative, "reason": reason})
            continue
        folded = relative.casefold()
        if folded in case_paths and case_paths[folded] != relative:
            raise DevFlowError(f"Case-colliding paths cannot be used portably: {case_paths[folded]} and {relative}")
        case_paths[folded] = relative
        if git_mode in {"120000", "160000"}:
            raise DevFlowError(f"Git symlinks and submodules require an explicit separate scope: {relative}")
        path = _safe_path(root, relative)
        try:
            value = _read_file(path, settings["max_file_bytes"], git_mode)
        except FileNotFoundError:
            if git_mode is not None and not path.exists():
                continue  # Tracked file deleted from local worktree.
            raise DevFlowError(f"File disappeared during snapshot: {relative}")
        except OSError as exc:
            raise DevFlowError(f"Cannot read snapshot input {relative}: {exc}") from exc
        fingerprints[relative] = value.pop("_stat")
        files[relative] = value
        total_bytes += value["size"]
        if len(files) > settings["max_files"]:
            raise DevFlowError(f"Snapshot exceeds {settings['max_files']} included files.")
        if total_bytes > settings["max_total_bytes"]:
            raise DevFlowError(f"Snapshot exceeds {settings['max_total_bytes']} included bytes.")
    for relative in settings["context_paths"]:
        if relative not in files:
            raise DevFlowError(f"Requested context file is not in the captured scope: {relative}")
    # Detect files added, removed, renamed, or touched while this bounded capture
    # was being assembled. This is optimistic consistency, not a filesystem lock.
    if settings["mode"] == "git":
        final_candidates, final_git = _git_inventory(root, settings)
        if final_git != git:
            raise DevFlowError("Git state changed during snapshot; stop concurrent edits and retry.")
    else:
        final_candidates = _file_inventory(root, settings, [])
    def selected(items):
        return {(name, mode) for name, mode in items if not name.endswith("/") and path_in_scope(name, settings)}
    if selected(final_candidates) != selected(candidates):
        raise DevFlowError("File inventory changed during snapshot; stop concurrent edits and retry.")
    try:
        for relative, expected in fingerprints.items():
            if _fingerprint(_safe_path(root, relative).lstat()) != expected:
                raise DevFlowError(f"File changed during snapshot: {relative}")
        for relative, git_mode in final_candidates:
            if git_mode is not None and path_in_scope(relative, settings) and relative not in files and _safe_path(root, relative).exists():
                raise DevFlowError(f"Deleted file reappeared during snapshot: {relative}")
    except OSError as exc:
        raise DevFlowError("File tree changed during snapshot; stop concurrent edits and retry.") from exc
    signature = {path: {key: value[key] for key in ("sha256", "size", "mode")} for path, value in sorted(files.items())}
    snapshot_id = hashlib.sha256(json.dumps(signature, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {
        "schema_version": 1, "snapshot_id": snapshot_id, "mode": settings["mode"],
        "git": git, "files": files, "excluded": sorted(excluded, key=lambda item: item["path"]),
        "file_count": len(files), "total_bytes": total_bytes,
        "context_paths": list(settings["context_paths"]),
        "scope_note": "Excluded paths and Git-ignored untracked files are outside this snapshot's coverage.",
    }


def _file_diff(path: str, before: dict | None, after: dict | None, binary: bool) -> str:
    """One file's change in Git's unified diff form, with its header even when the body is empty."""
    lines = [f"diff --git a/{path} b/{path}\n"]
    if before is None:
        lines.append(f"new file mode {after['mode']}\n")
    elif after is None:
        lines.append(f"deleted file mode {before['mode']}\n")
    elif before["mode"] != after["mode"]:
        lines += [f"old mode {before['mode']}\n", f"new mode {after['mode']}\n"]
    old_name = f"a/{path}" if before is not None else "/dev/null"
    new_name = f"b/{path}" if after is not None else "/dev/null"
    if binary:
        if (before or {}).get("sha256") != (after or {}).get("sha256"):
            lines.append(f"Binary files {old_name} and {new_name} differ\n")
        return "".join(lines)
    body = difflib.unified_diff((before or {}).get("text", "").splitlines(keepends=True),
                                (after or {}).get("text", "").splitlines(keepends=True),
                                fromfile=old_name, tofile=new_name)
    lines += [line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in body]
    return "".join(lines)


def compare(baseline: dict, current: dict) -> dict:
    """Changed file records, each with its exact Git-style unified diff."""
    old, new = baseline["files"], current["files"]
    changes = []
    binary_changes = []
    unchanged = 0
    for path in sorted(set(old) | set(new)):
        before, after = old.get(path), new.get(path)
        if before is not None and after is not None and (before["sha256"], before["mode"]) == (after["sha256"], after["mode"]):
            unchanged += 1
            continue
        kind = "added" if before is None else "deleted" if after is None else "modified"
        is_binary = any(item is not None and item.get("binary") for item in (before, after))
        if is_binary:
            binary_changes.append(path)
        changes.append({"path": path, "change": kind, "before": before, "after": after,
                        "diff": _file_diff(path, before, after, is_binary)})
    return {"baseline_id": baseline["snapshot_id"], "snapshot_id": current["snapshot_id"], "changed_files": changes, "binary_changes": binary_changes, "unchanged_count": unchanged}


def diff_text(delta: dict) -> str:
    """All changes of a ``compare`` result as one unified diff."""
    return "".join(change["diff"] for change in delta["changed_files"])


def change_list(delta: dict) -> list[dict]:
    """Each changed path with its mode, size and hash before and after; never the contents."""
    def brief(record: dict | None) -> dict | None:
        return None if record is None else {key: record[key] for key in ("mode", "size", "sha256")}
    return [{"path": change["path"], "change": change["change"], "binary": change["path"] in delta["binary_changes"],
             "before": brief(change["before"]), "after": brief(change["after"])} for change in delta["changed_files"]]
