"""PlanFlow paths plus the private, vendored shared storage primitives.

Imports never reach another installed plugin or the repository's shared/ tree.
"""
from pathlib import Path
import re
from ._vendor.flow_common.storage import (FlowError, atomic_json, checked_text,
    digest, file_lock, json_bytes, no_symlinks, now, read_json, within, write_new)

RUN_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[a-f0-9]{12}$")


def valid_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise FlowError("Invalid run ID. Use the exact ID shown by PlanFlow status.")
    return run_id


def project_root(start: Path) -> Path:
    current = start.resolve()
    if not current.is_dir():
        raise FlowError(f"Project directory does not exist: {current}")
    for candidate in [current, *current.parents]:
        if (candidate / ".devflow").exists():
            return candidate
        if (candidate / "devflow.config.json").exists() or (candidate / ".git").exists():
            return candidate
    return current


