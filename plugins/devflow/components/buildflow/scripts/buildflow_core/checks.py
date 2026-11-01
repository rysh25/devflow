"""Run explicitly configured project checks without constructing shell strings.

Checks are trusted project commands, not a sandbox. They run with the caller's
permissions; the engine hashes the worktree before and after every check set.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import threading
import time

from .storage import BuildFlowError


def _terminate_tree(process: subprocess.Popen) -> None:
    """Best-effort cleanup of the new process group; not an OS security boundary."""
    if os.name == "nt":
        # A separate process group alone does not terminate descendants on Windows.
        system = os.environ.get("SystemRoot", r"C:\Windows")
        taskkill = str(Path(system) / "System32" / "taskkill.exe")
        try:
            subprocess.run([taskkill, "/PID", str(process.pid), "/T", "/F"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, shell=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass


def _resolve_executable(argv: list[str]) -> list[str]:
    executable = argv[0]
    if Path(executable).suffix.casefold() in {".cmd", ".bat", ".ps1"}:
        raise BuildFlowError("Check commands require a native executable or an explicit interpreter argv; .cmd/.bat/.ps1 shims are not launched.")
    if not Path(executable).is_absolute() and ("/" in executable or "\\" in executable):
        raise BuildFlowError("Use an absolute executable path or a command resolved through PATH.")
    resolved = executable if Path(executable).is_absolute() else shutil.which(executable)
    if not resolved:
        raise BuildFlowError(f"Executable not found: {executable}")
    if Path(resolved).suffix.casefold() in {".cmd", ".bat", ".ps1"}:
        raise BuildFlowError(f"PATH resolved to a shell shim, not a native executable: {resolved}")
    return [resolved, *argv[1:]]


def bounded_run(argv: list[str], cwd: Path, timeout_seconds: float, max_output_bytes: int = 1_048_576, env: dict | None = None) -> dict:
    """Capture bounded stdout/stderr and terminate on timeout or output overflow.

    Return bytes internally so callers such as Git inventory can decode strictly.
    A child retaining pipes after its parent exits is an error, not a pass.
    """
    started = time.monotonic()
    command = _resolve_executable(argv)
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    try:
        process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, env=env, **options)
    except OSError as exc:
        raise BuildFlowError(f"Could not launch {argv[0]}: {exc}") from exc
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    lock = threading.Lock()
    overflow = threading.Event()
    read_error: list[str] = []

    def drain(pipe, name: str) -> None:
        try:
            while True:
                chunk = os.read(pipe.fileno(), 65536)
                if not chunk:
                    return
                with lock:
                    available = max_output_bytes - sum(len(value) for value in buffers.values())
                    if len(chunk) > available:
                        buffers[name].extend(chunk[:max(0, available)])
                        overflow.set()
                    else:
                        buffers[name].extend(chunk)
        except (OSError, ValueError) as exc:
            read_error.append(str(exc))
        finally:
            pipe.close()

    readers = [threading.Thread(target=drain, args=(process.stdout, "stdout"), daemon=True), threading.Thread(target=drain, args=(process.stderr, "stderr"), daemon=True)]
    for thread in readers:
        thread.start()
    timed_out = False
    while process.poll() is None:
        if overflow.is_set():
            _terminate_tree(process)
            break
        if time.monotonic() - started >= timeout_seconds:
            timed_out = True
            _terminate_tree(process)
            break
        time.sleep(0.01)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _terminate_tree(process)
        process.wait(timeout=5)
    for thread in readers:
        thread.join(timeout=0.3)
    retained_pipes = any(thread.is_alive() for thread in readers)
    if retained_pipes:
        _terminate_tree(process)
        for thread in readers:
            thread.join(timeout=1)
    with lock:
        output = {name + "_bytes": bytes(value) for name, value in buffers.items()}
    error = None
    if timed_out:
        error = f"Check exceeded {timeout_seconds} seconds; process-tree termination was requested."
    elif overflow.is_set():
        error = f"Combined stdout/stderr exceeded {max_output_bytes} bytes; output is incomplete."
    elif retained_pipes:
        error = "A descendant retained output pipes after the command exited; process-tree termination was requested."
    elif read_error:
        error = "Could not capture all command output: " + read_error[0]
    return {**output, "exit_code": process.returncode, "error": error, "timed_out": timed_out, "output_limit_exceeded": overflow.is_set(), "duration_seconds": round(time.monotonic() - started, 4)}


def _safe_directory(path: Path, root: Path | None = None) -> Path:
    absolute = Path(os.path.abspath(path))
    if root is not None and not absolute.is_relative_to(root):
        raise BuildFlowError("Check cwd must remain within the project.")
    for part in [absolute, *absolute.parents]:
        if root is not None and not part.is_relative_to(root):
            break
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise BuildFlowError(f"Symlinks and junctions are not accepted for check directories: {part}")
    return absolute


def validate_checks(checks: list[dict], timeout_default: int = 600) -> list[dict]:
    if not isinstance(checks, list):
        raise BuildFlowError("checks must be an array.")
    ids = set()
    validated = []
    for value in checks:
        fields = {"id", "argv", "timeout_seconds", "required", "cwd", "max_output_bytes"}
        if not isinstance(value, dict) or set(value) - fields:
            raise BuildFlowError("A check contains unknown fields.")
        check = {"timeout_seconds": timeout_default, "required": True, "cwd": ".", "max_output_bytes": 1_048_576, **value}
        identifier = check.get("id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", identifier) or identifier in ids:
            raise BuildFlowError("Each check needs a unique lowercase id.")
        ids.add(identifier)
        argv = check.get("argv")
        if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in argv):
            raise BuildFlowError("Check argv must be a nonempty array of nonempty strings, not a shell string.")
        check["argv"] = list(argv)
        if type(check["required"]) is not bool:
            raise BuildFlowError("Check required must be a boolean.")
        if type(check["timeout_seconds"]) is not int or not 1 <= check["timeout_seconds"] <= 3600:
            raise BuildFlowError("Check timeout_seconds must be an integer from 1 to 3600.")
        if type(check["max_output_bytes"]) is not int or not 1 <= check["max_output_bytes"] <= 16 * 1024 * 1024:
            raise BuildFlowError("Check max_output_bytes must be from 1 byte to 16 MiB.")
        cwd = check["cwd"]
        if not isinstance(cwd, str) or not cwd or "\\" in cwd or ":" in cwd or cwd.startswith("/") or ".." in cwd.split("/") or any(ord(char) < 32 for char in cwd):
            raise BuildFlowError("Check cwd must be a project-relative directory without traversal.")
        validated.append(check)
    return validated


def run_checks(project: Path, checks: list[dict], work_dir: Path, timeout_default: int = 600) -> dict:
    """Execute only configured argv, saving immutable bounded stdout/stderr logs."""
    configured = validate_checks(checks, timeout_default)
    if not configured:
        return {"status": "not_configured", "results": []}
    root = _safe_directory(Path(project).resolve(strict=True))
    logs = _safe_directory(Path(work_dir))
    logs.mkdir(parents=True, exist_ok=True)
    prepared = []
    for check in configured:
        cwd = _safe_directory(root / check["cwd"], root)
        if not cwd.is_dir():
            raise BuildFlowError(f"Check cwd does not exist: {check['cwd']}")
        paths = [logs / f"{check['id']}.{stream}.log" for stream in ("stdout", "stderr")]
        if any(path.exists() or path.is_symlink() for path in paths):
            raise BuildFlowError("Check logs already exist; allocate a new check-attempt directory instead of overwriting history.")
        prepared.append((check, cwd, paths))
    results = []
    for check, cwd, paths in prepared:
        try:
            execution = bounded_run(check["argv"], cwd, check["timeout_seconds"], check["max_output_bytes"])
        except BuildFlowError as exc:
            execution = {"exit_code": None, "stdout_bytes": b"", "stderr_bytes": b"", "error": str(exc), "duration_seconds": 0.0, "timed_out": False, "output_limit_exceeded": False}
        for path, stream in zip(paths, ("stdout", "stderr")):
            with path.open("xb") as output:
                output.write(execution[stream + "_bytes"])
        status = "error" if execution["error"] else "passed" if execution["exit_code"] == 0 else "failed"
        results.append({
            "id": check["id"], "required": check["required"], "status": status,
            "argv": check["argv"], "cwd": check["cwd"],
            "exit_code": execution["exit_code"], "error": execution["error"],
            "stdout": execution["stdout_bytes"].decode("utf-8", "replace"),
            "stderr": execution["stderr_bytes"].decode("utf-8", "replace"),
            "stdout_path": str(paths[0]), "stderr_path": str(paths[1]),
            "duration_seconds": execution["duration_seconds"],
            "timed_out": execution["timed_out"], "output_limit_exceeded": execution["output_limit_exceeded"],
        })
    required_statuses = {result["status"] for result in results if result["required"]}
    overall = "error" if "error" in required_statuses else "failed" if "failed" in required_statuses else "passed"
    return {"status": overall, "results": results}
