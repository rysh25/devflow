"""BuildFlow paths plus privately vendored shared storage primitives."""
from pathlib import Path
import re

from ._vendor.flow_common.storage import (FlowError, now, digest, json_bytes, read_json,
    no_symlinks, within, write_new, atomic_write_json, atomic_json, file_lock, checked_text)

BuildFlowError = FlowError
RUN_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[a-f0-9]{12}$")


def valid_run_id(value: str) -> str:
    if not isinstance(value, str) or not RUN_ID.fullmatch(value):
        raise BuildFlowError("Invalid run ID; use the ID returned by BuildFlow")
    return value


def project_root(start: Path) -> Path:
    current = start.resolve()
    if not current.is_dir():
        raise BuildFlowError(f"Project directory does not exist: {current}")
    for candidate in (current, *current.parents):
        if (candidate / ".devflow").exists() or (candidate / "devflow.config.json").exists() or (candidate / ".git").exists():
            return candidate
    return current


def session_path(project: Path, session_id: str) -> Path:
    if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 512:
        raise BuildFlowError("A real Claude Code session ID is required")
    return project / ".devflow" / "sessions" / "implement" / (digest(session_id.encode("utf-8")) + ".json")
