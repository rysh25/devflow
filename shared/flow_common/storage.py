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
from uuid import uuid4


class FlowError(Exception):
    """An actionable workflow failure. Never interpret an error as approval."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise FlowError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path: Path, limit: int = 64 * 1024 * 1024) -> dict:
    no_symlinks(path)
    try:
        if path.stat().st_size > limit:
            raise FlowError(f"JSON exceeds {limit} bytes: {path}")
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique,
                           parse_constant=lambda x: (_ for _ in ()).throw(FlowError(f"Invalid JSON constant: {x}")))
    except (OSError, UnicodeError, ValueError) as exc:
        raise FlowError(f"Cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise FlowError(f"Expected a JSON object: {path}")
    return value


def no_symlinks(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    for candidate in (absolute, *absolute.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise FlowError(f"Cannot inspect path: {candidate}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode) or (getattr(info, "st_file_attributes", 0) & 0x400):
            raise FlowError(f"Symlink/reparse paths are not allowed: {candidate}")


def within(path: Path, base: Path) -> Path:
    no_symlinks(path)
    root, resolved = base.resolve(), path.resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        raise FlowError(f"Path must be below {root}: {path}")
    return resolved


def write_new(path: Path, data: bytes) -> None:
    """Publish a complete immutable artifact exclusively, using a hard link."""
    no_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".flow-new-", dir=str(path.parent))
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    except FileExistsError as exc:
        raise FlowError(f"Refusing to replace immutable artifact: {path}") from exc
    except OSError as exc:
        raise FlowError(f"Cannot publish {path}; a local filesystem with hard links is required: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: object) -> None:
    no_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".flow-", dir=str(path.parent))
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


atomic_json = atomic_write_json


@contextmanager
def file_lock(path: Path):
    token = uuid4().hex
    try:
        write_new(path, json_bytes({"token": token, "pid": os.getpid(), "host": socket.gethostname(), "created_at": now()}))
    except FlowError as exc:
        raise FlowError(f"Run is locked: {path}. Verify the owning process before human recovery.") from exc
    try:
        yield
    finally:
        if path.exists() and read_json(path).get("token") == token:
            path.unlink()


def checked_text(path: Path, limit: int = 2 * 1024 * 1024) -> str:
    no_symlinks(path)
    try:
        if not path.is_file() or path.stat().st_size > limit:
            raise FlowError(f"Input must be a file no larger than {limit} bytes: {path}")
        value = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise FlowError(f"Input must be readable UTF-8: {path}: {exc}") from exc
    if not value.strip() or "\x00" in value:
        raise FlowError(f"Input must be nonempty text without NUL: {path}")
    return value
