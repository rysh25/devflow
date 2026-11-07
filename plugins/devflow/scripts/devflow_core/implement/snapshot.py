"""Deterministic snapshots of an explicitly scoped local worktree.

No checkout, reset, add, commit, Git filters, or external diff driver is used.
A snapshot lists each file's path, sha256, size, mode and whether it is
binary; it never holds file contents. Files are hashed while they are read,
so their size never stops a snapshot. The start-time snapshot keeps a copy of
only the text files whose exact bytes Git does not already hold (changes
present before the run, untracked files, every text file in files mode), so a
checkpoint's diff starts from the worktree as it was, not from HEAD.
"""

from __future__ import annotations

import codecs
import difflib
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

from ..common.git import content_timeout, run_git
from ..common.errors import DevFlowError
from ..common.storage import write_new

#: How much of a file is read at a time.
CHUNK = 64 * 1024


DEFAULT_EXCLUDES = [
    ".git/", ".devflow/", "devflow.config.json", "devflow.config.local.json", ".env", ".env.*",
    ".ssh/", ".aws/", ".azure/", "secrets/", "*.pem", "*.key",
    "*.p12", "*.pfx", "node_modules/", ".venv/", "venv/", "__pycache__/",
    ".pytest_cache/", ".mypy_cache/", ".ruff_cache/", ".next/", ".nuxt/",
    "Library/", "Temp/", "Obj/", "Logs/", "UserSettings/", "bin/", "obj/",
]
_ALWAYS_EXCLUDED = {".git", ".devflow", "devflow.config.json", "devflow.config.local.json"}
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
    result = {"mode": "git", "include": ["**"], "exclude": [], "max_packet_bytes": 2_000_000, "context_paths": []}
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
    if type(result["max_packet_bytes"]) is not int or result["max_packet_bytes"] < 1:
        raise DevFlowError("snapshot.max_packet_bytes must be a positive integer.")
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


def _git_inventory(root: Path, settings: dict) -> tuple[list[tuple[str, str | None]], dict, dict[str, str]]:
    """Candidate paths with their Git modes, the Git facts, and each tracked path's index ID."""
    try:
        top = run_git(root, "rev-parse", "--show-toplevel").decode("utf-8", "strict").strip()
        if Path(top).resolve() != root:
            raise DevFlowError("snapshot.mode=git requires the exact repository root; parent repositories are not selected implicitly.")
        head_raw = run_git(root, "rev-parse", "--verify", "HEAD", ok_codes=(0, 128))
        head = head_raw.decode("ascii", "strict").strip() or None
        candidates: dict[str, str | None] = {}
        index: dict[str, str] = {}
        for entry in run_git(root, "ls-files", "--stage", "-z").split(b"\0"):
            if not entry:
                continue
            prefix, raw_path = entry.split(b"\t", 1)
            mode, oid, stage = prefix.decode("ascii").split(" ")
            if stage != "0":
                raise DevFlowError("Unmerged Git index entries require human resolution before an implementation run starts.")
            path = raw_path.decode("utf-8", "strict")
            candidates[path] = mode
            index[path] = oid
        for raw_path in run_git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0"):
            if raw_path:
                candidates[raw_path.decode("utf-8", "strict")] = None
        status = _scoped_status(run_git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all"), settings)
    except (UnicodeError, ValueError) as exc:
        raise DevFlowError("Git paths must have valid UTF-8 names and a readable index/status.") from exc
    return sorted(candidates.items()), {
        "head": head, "dirty": bool(status), "status": status,
        "scope": "Dirty status and file contents cover included tracked and non-ignored untracked paths only; ignored untracked paths are not scanned.",
    }, index


def _file_inventory(root: Path, settings: dict, excluded: list[dict]) -> list[tuple[str, str | None]]:
    candidates: list[tuple[str, str | None]] = []
    todo = [root]
    while todo:
        directory = todo.pop()
        try:
            with os.scandir(directory) as scanned:
                entries = sorted(scanned, key=lambda entry: entry.name)
        except OSError as exc:
            raise DevFlowError(f"Cannot enumerate project directory: {directory}") from exc
        for entry in entries:
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


def _read_file(path: Path, git_mode: str | None) -> dict:
    """Hash one file while reading it in chunks, and decide whether it is binary.

    A file is binary when it contains NUL or is not valid UTF-8, the same rule
    as before; its contents are never kept.
    """
    before = path.lstat()
    if _is_link(before) or not stat.S_ISREG(before.st_mode):
        raise DevFlowError(f"Snapshot requires a regular non-link file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    hasher = hashlib.sha256()
    decoder = codecs.getincrementaldecoder("utf-8")()
    size, text = 0, True
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if _content(opened) != _content(before):
            raise DevFlowError(f"File changed while snapshot opened it: {path}")
        while chunk := stream.read(CHUNK):
            hasher.update(chunk)
            size += len(chunk)
            if text:
                try:
                    text = b"\0" not in chunk and decoder.decode(chunk) is not None
                except UnicodeDecodeError:
                    text = False
        after_read = os.fstat(stream.fileno())
    if text:
        try:
            decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            text = False
    after = path.lstat()
    # Each call is compared with itself in full, and across the two calls by content only.
    if (size != after_read.st_size or _fingerprint(opened) != _fingerprint(after_read)
            or _fingerprint(before) != _fingerprint(after) or _content(after_read) != _content(after)):
        raise DevFlowError(f"File changed while snapshot read it: {path}")
    mode = "100755" if before.st_mode & 0o111 else "100644"
    if os.name == "nt" and git_mode in {"100644", "100755"}:
        mode = git_mode
    return {"sha256": hasher.hexdigest(), "size": size, "mode": mode, "binary": not text, "_stat": _fingerprint(after)}


def _chunks(path: Path, hasher):
    """A file's bytes in chunks, hashed as they are read."""
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK):
            hasher.update(chunk)
            yield chunk


def _git_held(root: Path, paths: list[str], files: dict[str, dict], index: dict[str, str]) -> dict[str, str]:
    """The tracked paths whose exact bytes are the blob in Git's index, with that blob's ID.

    Git computes the IDs itself (``hash-object --no-filters``, so no filter or
    line-ending conversion runs). Each path is passed as ``./path`` so a name
    that starts with a double quote is taken literally.
    """
    if not paths:
        return {}
    data = "".join(f"./{path}\n" for path in paths).encode("utf-8")
    try:
        output = run_git(root, "hash-object", "--no-filters", "--stdin-paths", data=data,
                         timeout=content_timeout(sum(files[path]["size"] for path in paths)))
        ids = output.decode("ascii", "strict").split()
    except UnicodeError as exc:
        raise DevFlowError("Cannot read the object IDs Git computed") from exc
    if len(ids) != len(paths):
        raise DevFlowError("Git computed a different number of object IDs than files")
    return {path: oid for path, oid in zip(paths, ids) if index.get(path) == oid}


def _keep_copies(root: Path, settings: dict, files: dict[str, dict], candidates: dict[str, str | None],
                 index: dict[str, str], copies: Path) -> None:
    """Record where each text file's start-time bytes can be read back (``source``).

    Git holds the bytes: ``{"git": ID}``. Otherwise a copy is written once to
    ``copies/<sha256>`` and recorded as ``{"copy": "<copies name>/<sha256>"}``.
    Binary files need no source: their diff never shows contents.
    """
    text = [path for path, record in files.items() if not record["binary"]]
    tracked = [path for path in text if settings["mode"] == "git" and candidates.get(path) is not None]
    held = _git_held(root, tracked, files, index)
    for path in text:
        record = files[path]
        if path in held:
            record["source"] = {"git": held[path]}
            continue
        target = copies / record["sha256"]
        if not target.exists():
            hasher = hashlib.sha256()
            write_new(target, _chunks(_safe_path(root, path), hasher))
            if hasher.hexdigest() != record["sha256"]:
                raise DevFlowError(f"File changed during snapshot: {path}")
        record["source"] = {"copy": f"{copies.name}/{record['sha256']}"}


def capture(project: Path, config: dict, copies: Path | None = None) -> dict:
    """List every included file without its contents; the size of the scope never stops it.

    ``copies`` (the start of a run only) is a directory for the start-time
    copies of text files Git does not hold; each file then names its
    ``source``.
    """
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
    index: dict[str, str] = {}
    if settings["mode"] == "git":
        candidates, git, index = _git_inventory(root, settings)
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
            value = _read_file(path, git_mode)
        except FileNotFoundError:
            if git_mode is not None and not path.exists():
                continue  # Tracked file deleted from local worktree.
            raise DevFlowError(f"File disappeared during snapshot: {relative}")
        except OSError as exc:
            raise DevFlowError(f"Cannot read snapshot input {relative}: {exc}") from exc
        fingerprints[relative] = value.pop("_stat")
        files[relative] = value
        total_bytes += value["size"]
    for relative in settings["context_paths"]:
        if relative not in files:
            raise DevFlowError(f"Requested context file is not in the captured scope: {relative}")
    if copies is not None:
        try:
            _keep_copies(root, settings, files, dict(candidates), index, copies)
        except OSError as exc:
            raise DevFlowError(f"Cannot keep the start-time copies: {exc}") from exc
    # Detect files added, removed, renamed, or touched while this capture was
    # being assembled (copies included). This is optimistic consistency, not a
    # filesystem lock.
    if settings["mode"] == "git":
        final_candidates, final_git, final_index = _git_inventory(root, settings)
        if final_git != git or final_index != index:
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
    return {
        "schema_version": 2, "snapshot_id": snapshot_id(files), "mode": settings["mode"],
        "git": git, "files": files, "excluded": sorted(excluded, key=lambda item: item["path"]),
        "file_count": len(files), "total_bytes": total_bytes,
        "context_paths": list(settings["context_paths"]),
        "scope_note": "Excluded paths and Git-ignored untracked files are outside this snapshot's coverage.",
    }


def snapshot_id(files: dict[str, dict]) -> str:
    """The snapshot ID: the sha256 of every path's sha256, size and mode (nothing else)."""
    signature = {path: {key: value[key] for key in ("sha256", "size", "mode")} for path, value in sorted(files.items())}
    return hashlib.sha256(json.dumps(signature, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _file_diff(path: str, before: dict | None, after: dict | None, binary: bool,
               before_text: str | None, after_text: str | None) -> str:
    """One file's change in Git's unified diff form, with its header even when the body is empty.

    The records carry no contents: the texts are passed in (``None`` for a
    side that does not exist and for a binary change).
    """
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
    body = difflib.unified_diff((before_text or "").splitlines(keepends=True),
                                (after_text or "").splitlines(keepends=True),
                                fromfile=old_name, tofile=new_name)
    lines += [line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in body]
    return "".join(lines)


def _brief(record: dict | None, keys=("mode", "size", "sha256", "binary")) -> dict | None:
    return None if record is None else {key: record[key] for key in keys}


def _change(before: dict | None, after: dict | None) -> str | None:
    """How one file changed between two listings (None: unchanged); never reads contents."""
    if before is None and after is None:
        return None
    if before is None:
        return "added"
    if after is None:
        return "deleted"
    return None if (before["sha256"], before["mode"]) == (after["sha256"], after["mode"]) else "modified"


def changes(baseline: dict, current: dict) -> dict:
    """The record of a checkpoint: every change since the start, without contents.

    Applying these changes to the start-time listing gives the current
    listing, whose ID is ``snapshot_id``.
    """
    old, new = baseline["files"], current["files"]
    result, binary_changes, unchanged = [], [], 0
    for path in sorted(set(old) | set(new)):
        before, after = old.get(path), new.get(path)
        change = _change(before, after)
        if change is None:
            unchanged += 1
            continue
        if any(item is not None and item["binary"] for item in (before, after)):
            binary_changes.append(path)
        result.append({"path": path, "change": change, "before": _brief(before), "after": _brief(after)})
    return {"schema_version": 1, "baseline_id": baseline["snapshot_id"], "snapshot_id": current["snapshot_id"],
            "changes": result, "binary_changes": binary_changes, "unchanged_count": unchanged,
            "file_count": current["file_count"], "total_bytes": current["total_bytes"]}


def between(earlier: dict, later: dict) -> list[dict]:
    """The files added, modified and deleted from one checkpoint record to another.

    Both records list changes since the same start, so a path one record does not
    list is in its start state, which the other record holds as ``before``.
    Compared as ``changes`` compares; no contents are read.
    """
    first = {change["path"]: change for change in earlier["changes"]}
    second = {change["path"]: change for change in later["changes"]}
    result = []
    for path in sorted(set(first) | set(second)):
        start = (first.get(path) or second[path])["before"]
        before = first[path]["after"] if path in first else start
        after = second[path]["after"] if path in second else start
        change = _change(before, after)
        if change is not None:
            result.append({"path": path, "change": change})
    return result


def packet_changes(record: dict) -> list[dict]:
    """The review packet's change list (format 2): mode, size and hash before and after, never contents."""
    binary = set(record["binary_changes"])
    return [{"path": change["path"], "change": change["change"], "binary": change["path"] in binary,
             "before": _brief(change["before"], ("mode", "size", "sha256")),
             "after": _brief(change["after"], ("mode", "size", "sha256"))} for change in record["changes"]]


def _from_git(root: Path, path: str, record: dict) -> bytes:
    """A start-time blob, read with replace refs ignored (they apply to cat-file, not hash-object)."""
    oid = record["source"]["git"]
    output = run_git(root, "--no-replace-objects", "cat-file", "--batch", data=f"{oid}\n".encode("ascii"),
                     timeout=content_timeout(record["size"]))
    header, _, rest = output.partition(b"\n")
    fields = header.decode("ascii", "replace").split()
    if fields[1:] == ["missing"]:
        raise DevFlowError(f"The start-time version of {path} is no longer in Git (it was staged again and removed "
                           "by git gc); cancel this run and start a new one")
    if len(fields) != 3 or fields[1] != "blob" or not fields[2].isdigit():
        raise DevFlowError(f"Cannot read the start-time version of {path} from Git")
    return rest[:int(fields[2])]


def _start_text(root: Path, run_dir: Path, path: str, record: dict) -> str:
    source = record.get("source") or {}
    if "copy" in source:
        data = (run_dir / source["copy"]).read_bytes()
    elif "git" in source:
        data = _from_git(root, path, record)
    else:
        raise DevFlowError(f"The start-time version of {path} was not kept; cancel this run and start a new one")
    if hashlib.sha256(data).hexdigest() != record["sha256"]:
        raise DevFlowError(f"The start-time version of {path} does not match the baseline record; "
                           "cancel this run and start a new one")
    return data.decode("utf-8")


def _current_text(root: Path, path: str, record: dict) -> str:
    data = _safe_path(root, path).read_bytes()
    if hashlib.sha256(data).hexdigest() != record["sha256"]:
        raise DevFlowError(f"File changed during checkpoint: {path}")
    return data.decode("utf-8")


def diff(root: Path, run_dir: Path, baseline: dict, current: dict, record: dict) -> str:
    """Every change of ``record`` as one Git-style unified diff, one file at a time.

    Only one file's start-time and current texts are held at once.
    """
    root = Path(root).resolve(strict=True)
    binary = set(record["binary_changes"])
    parts = []
    for change in record["changes"]:
        path, before, after = change["path"], change["before"], change["after"]
        before_text = after_text = None
        if path not in binary:
            if before is not None:
                before_text = _start_text(root, run_dir, path, baseline["files"][path])
            if after is not None:
                after_text = _current_text(root, path, current["files"][path])
        parts.append(_file_diff(path, before, after, path in binary, before_text, after_text))
    return "".join(parts)
