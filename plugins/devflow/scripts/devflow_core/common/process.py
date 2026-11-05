"""Child processes for project checks, reviewer CLIs and Git (not a sandbox).

One implementation of the timeout, the output streams and process-tree
cleanup, so the policy cannot drift between callers. Output is never limited
by size; a process is stopped only by its timeout or when a descendant keeps
its pipes open. Callers decide whether a timeout is a recorded result
(checks) or a failure (reviewers, Git).
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import time
from typing import BinaryIO

from .errors import DevFlowError

#: How long the output pipes (and the standard input writer) may stay open
#: after the command exits before a descendant is taken to hold them.
PIPE_GRACE_SECONDS = 0.3


@dataclass(frozen=True)
class Completed:
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    retained_pipes: bool
    output_error: str | None
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


def run_bounded(argv: list[str], *, cwd: Path | None, timeout: float, env: dict | None = None,
                data: bytes | None = None, stdout: BinaryIO | None = None,
                stderr: BinaryIO | None = None) -> Completed:
    """Run argv (never a shell); only ``timeout`` stops it.

    Each output stream is kept in memory (``None``) or written, as it
    arrives, to a binary file the caller opened (the caller creates and
    closes it, so a launch failure still leaves the file). For a file, one
    thread reads the pipe and marks its end, and another writes the chunks,
    so a slow file never delays noticing the end of the pipe.

    The child starts in its own process group, and the tree is stopped on
    timeout or when a descendant keeps the output pipes (or the standard
    input) open after the command exits. Without ``data`` the standard input
    is closed.
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
    sinks = {"stdout": stdout, "stderr": stderr}
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    ended = {"stdout": threading.Event(), "stderr": threading.Event()}
    chunks = {name: queue.SimpleQueue() for name, sink in sinks.items() if sink is not None}
    lock = threading.Lock()
    errors: list[str] = []

    def read(pipe, name: str) -> None:
        try:
            while chunk := os.read(pipe.fileno(), 65536):
                if name in chunks:
                    chunks[name].put(chunk)
                else:
                    with lock:
                        buffers[name].extend(chunk)
        except (OSError, ValueError) as exc:
            errors.append(f"cannot read {name}: {exc}")
        finally:
            ended[name].set()
            if name in chunks:
                chunks[name].put(None)
            pipe.close()

    def write(name: str) -> None:
        failed = False
        while (chunk := chunks[name].get()) is not None:
            if failed:
                continue  # Keep draining so the reader (and the child) never block.
            try:
                sinks[name].write(chunk)
            except (OSError, ValueError) as exc:
                errors.append(f"cannot write {name}: {exc}")
                failed = True
        if not failed:
            try:
                sinks[name].flush()
            except (OSError, ValueError) as exc:
                errors.append(f"cannot write {name}: {exc}")

    def feed() -> None:
        try:
            process.stdin.write(data)
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            process.stdin.close()

    readers = [threading.Thread(target=read, args=(process.stdout, "stdout"), daemon=True),
               threading.Thread(target=read, args=(process.stderr, "stderr"), daemon=True)]
    writers = {name: threading.Thread(target=write, args=(name,), daemon=True) for name in chunks}
    feeder = threading.Thread(target=feed, daemon=True) if data is not None else None
    for thread in [*readers, *writers.values(), *([feeder] if feeder else [])]:
        thread.start()
    timed_out = False
    try:
        while process.poll() is None:
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
    # A descendant that keeps a pipe open is noticed by the pipe not ending,
    # never by DevFlow's own writes being slow.
    deadline = time.monotonic() + PIPE_GRACE_SECONDS
    retained = not all(event.wait(max(0.0, deadline - time.monotonic())) for event in ended.values())
    if feeder is not None:
        feeder.join(max(0.0, deadline - time.monotonic()))
        retained = retained or feeder.is_alive()
    if retained:
        stop_tree(process)
        for thread in [*readers, *([feeder] if feeder else [])]:
            thread.join(timeout=1)
    for name, writer in writers.items():
        if not ended[name].is_set():
            chunks[name].put(None)  # The pipe never ended even after the stop: write what arrived.
        writer.join()
    with lock:
        out, err = bytes(buffers["stdout"]), bytes(buffers["stderr"])
    return Completed(process.returncode, out, err, timed_out, retained,
                     errors[0] if errors else None, round(time.monotonic() - started, 4))
