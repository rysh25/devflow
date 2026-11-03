"""Bounded child processes for project checks and reviewer CLIs (not a sandbox).

One implementation of the output limit, the timeout and process-tree cleanup,
so the policy cannot drift between callers. Callers decide whether a timeout or
an overflow is a recorded result (checks) or a failure (reviewers).
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import signal
import subprocess
import threading
import time

from .errors import DevFlowError


@dataclass(frozen=True)
class Completed:
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    output_limit_exceeded: bool
    retained_pipes: bool
    read_error: str | None
    duration_seconds: float


def stop_tree(process: subprocess.Popen) -> None:
    """Best-effort termination of the child and its descendants; not an OS security boundary."""
    if os.name == "nt":
        # A separate process group alone does not terminate descendants on Windows.
        taskkill = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe")
        try:
            subprocess.run([taskkill, "/PID", str(process.pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, shell=False)
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


def run_bounded(argv: list[str], *, cwd: Path | None, timeout: float, limit: int,
                env: dict | None = None, data: bytes | None = None, per_stream: bool = False) -> Completed:
    """Run argv (never a shell), capping output at ``limit`` bytes.

    The limit covers stdout and stderr together, or each stream separately
    when ``per_stream`` is set.

    The child starts in its own process group, and the tree is stopped on
    timeout, on overflow, or when a descendant keeps the output pipes open
    after the command exits. Without ``data`` the standard input is closed.
    """
    started = time.monotonic()
    if os.name == "nt":
        options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        options = {"start_new_session": True}
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=env, shell=False,
                                   stdin=subprocess.DEVNULL if data is None else subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
    except OSError as exc:
        raise DevFlowError(f"Could not launch {argv[0]}: {exc}") from exc
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    lock = threading.Lock()
    overflow = threading.Event()
    read_errors: list[str] = []

    def drain(pipe, name: str) -> None:
        try:
            while chunk := os.read(pipe.fileno(), 65536):
                with lock:
                    used = len(buffers[name]) if per_stream else sum(len(value) for value in buffers.values())
                    available = limit - used
                    buffers[name].extend(chunk[:max(0, available)])
                    if len(chunk) > available:
                        overflow.set()
        except (OSError, ValueError) as exc:
            read_errors.append(str(exc))
        finally:
            pipe.close()

    def feed() -> None:
        try:
            process.stdin.write(data)
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            process.stdin.close()

    threads = [threading.Thread(target=drain, args=(process.stdout, "stdout"), daemon=True),
               threading.Thread(target=drain, args=(process.stderr, "stderr"), daemon=True)]
    if data is not None:
        threads.append(threading.Thread(target=feed, daemon=True))
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        while process.poll() is None:
            if overflow.is_set():
                stop_tree(process)
                break
            if time.monotonic() - started >= timeout:
                timed_out = True
                stop_tree(process)
                break
            time.sleep(0.01)
    except BaseException:
        stop_tree(process)
        raise
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        stop_tree(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            raise DevFlowError("A child process did not terminate; stop it manually before continuing") from exc
    for thread in threads:
        thread.join(timeout=0.3)
    retained = any(thread.is_alive() for thread in threads)
    if retained:
        stop_tree(process)
        for thread in threads:
            thread.join(timeout=1)
    with lock:
        stdout, stderr = bytes(buffers["stdout"]), bytes(buffers["stderr"])
    return Completed(process.returncode, stdout, stderr, timed_out, overflow.is_set(), retained,
                     read_errors[0] if read_errors else None, round(time.monotonic() - started, 4))
