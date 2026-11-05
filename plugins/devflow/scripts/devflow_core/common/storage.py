"""Portable, fail-closed atomic artifact storage. Python standard library only."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import time
from uuid import uuid4

from .errors import DevFlowError


#: The largest JSON record DevFlow reads, and so the largest it writes.
JSON_LIMIT = 64 * 1024 * 1024


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_file(path: Path) -> str:
    """The sha256 of a file, read 1 MiB at a time (never whole into memory)."""
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def _refuse_large_json(name: str, size: int) -> None:
    if size > JSON_LIMIT:
        raise DevFlowError(f"Record {name} would be {size} bytes, over the 64 MiB JSON limit; it was not saved")


def check_json_size(name: str, data: bytes) -> bytes:
    """Refuse to write a JSON record that ``read_json`` could not read back."""
    _refuse_large_json(name, len(data))
    return data


def check_json_file(name: str, path: Path) -> None:
    """The same refusal for a record given as a file (its size, without reading it)."""
    _refuse_large_json(name, Path(path).stat().st_size)


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DevFlowError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_json(data: bytes, name: object) -> dict:
    """One JSON object, read as every record is: strict UTF-8, no duplicate keys or NaN."""
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique,
                           parse_constant=lambda x: (_ for _ in ()).throw(DevFlowError(f"Invalid JSON constant: {x}")))
    except (UnicodeError, ValueError) as exc:
        raise DevFlowError(f"Cannot read JSON {name}: {exc}") from exc
    except RecursionError as exc:
        # json.loads raises this, not ValueError, for deeply nested input.
        raise DevFlowError(f"Cannot read JSON {name}: nested too deeply") from exc
    if not isinstance(value, dict):
        raise DevFlowError(f"Expected a JSON object: {name}")
    return value


def read_json(path: Path, limit: int = JSON_LIMIT) -> dict:
    no_symlinks(path)
    try:
        if path.stat().st_size > limit:
            raise DevFlowError(f"JSON exceeds {limit} bytes: {path}")
        data = path.read_bytes()
    except OSError as exc:
        raise DevFlowError(f"Cannot read JSON {path}: {exc}") from exc
    return parse_json(data, path)


def no_symlinks(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    for candidate in (absolute, *absolute.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise DevFlowError(f"Cannot inspect path: {candidate}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode) or (getattr(info, "st_file_attributes", 0) & 0x400):
            raise DevFlowError(f"Symlink/reparse paths are not allowed: {candidate}")


def within(path: Path, base: Path) -> Path:
    no_symlinks(path)
    root, resolved = base.resolve(), path.resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        raise DevFlowError(f"Path must be below {root}: {path}")
    return resolved


def _temporary(path: Path, data, prefix: str) -> Path:
    """A complete temporary file next to ``path``, on disk (fsync) before it is used.

    ``data`` is bytes or a sequence of byte chunks, so a large file is never
    held whole in memory.
    """
    no_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=prefix, dir=str(path.parent))
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            for chunk in ((data,) if isinstance(data, (bytes, bytearray)) else data):
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def write_new(path: Path, data) -> None:
    """Publish a complete immutable artifact exclusively, using a hard link.

    ``data`` is bytes or a sequence of byte chunks.
    """
    temporary = _temporary(path, data, ".flow-new-")
    try:
        os.link(temporary, path)
    except FileExistsError as exc:
        raise DevFlowError(f"Refusing to replace immutable artifact: {path}") from exc
    except OSError as exc:
        raise DevFlowError(f"Cannot publish {path}; a local filesystem with hard links is required: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def replace_file(path: Path, data: bytes) -> None:
    """Replace a file atomically (temporary file, fsync, rename)."""
    temporary = _temporary(path, data, ".flow-")
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: object) -> None:
    """Replace a JSON file atomically; never one that ``read_json`` could not read back."""
    replace_file(path, check_json_size(Path(path).name, json_bytes(value)))


atomic_json = atomic_write_json


@contextmanager
def file_lock(path: Path, wait: float = 0.0):
    """Exclusive lock file; waits at most ``wait`` seconds for a short holder.

    A lock is never removed because of its age: a stale lock needs a human to
    confirm that its owning process has stopped.
    """
    token = uuid4().hex
    record = json_bytes({"token": token, "pid": os.getpid(), "host": socket.gethostname(), "created_at": now()})
    deadline = time.monotonic() + max(0.0, wait)
    delay = 0.02
    while True:
        try:
            write_new(path, record)
            break
        except DevFlowError as exc:
            if not path.exists() or time.monotonic() >= deadline:
                raise DevFlowError(f"Run is locked: {path}. Verify the owning process before human recovery.") from exc
            time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
            delay = min(delay * 2, 0.25)
    try:
        yield
    finally:
        if path.exists() and read_json(path).get("token") == token:
            path.unlink()


def checked_text(path: Path, limit: int = 2 * 1024 * 1024) -> str:
    no_symlinks(path)
    try:
        if not path.is_file() or path.stat().st_size > limit:
            raise DevFlowError(f"Input must be a file no larger than {limit} bytes: {path}")
        value = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise DevFlowError(f"Input must be readable UTF-8: {path}: {exc}") from exc
    if not value.strip() or "\x00" in value:
        raise DevFlowError(f"Input must be nonempty text without NUL: {path}")
    return value
