"""The single child-process runner shared by checks, reviewers and Git."""
import io
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from devflow_core.common.errors import DevFlowError, ProcessCleanupError
from devflow_core.common import process as process_module
from devflow_core.common.process import run_bounded

MIB = 1024 * 1024
# A descendant that keeps the output pipes open after the command exits. It runs outside the
# test's directory: Windows cannot stop it once its parent has exited, and it would keep that
# directory in use.
RETAIN = ("import subprocess, sys, tempfile; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'], "
          "cwd=tempfile.gettempdir())")


class SlowFile(io.BytesIO):
    """A log file whose every write takes ``delay`` seconds."""

    def __init__(self, delay):
        super().__init__()
        self.delay = delay

    def write(self, data):
        time.sleep(self.delay)
        return super().write(data)


class FailingFile(io.BytesIO):
    """A log file whose writes fail, like a full disk."""

    def write(self, data):
        raise OSError(28, "No space left on device")


class RunBoundedTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="devflow process ")
        self.addCleanup(temporary.cleanup)
        self.cwd = Path(temporary.name)

    def python(self, code, **options):
        return run_bounded([sys.executable, "-c", code], cwd=self.cwd, **options)

    def test_completes_with_exit_code_and_both_streams(self):
        result = self.python("import sys; print('out'); print('err', file=sys.stderr); raise SystemExit(3)",
                             timeout=20)
        self.assertEqual((result.exit_code, result.stdout.strip(), result.stderr.strip()), (3, b"out", b"err"))
        self.assertFalse(result.timed_out or result.retained_pipes)
        self.assertIsNone(result.output_error)

    def test_output_goes_to_the_files_the_caller_opened(self):
        out, err = self.cwd / "out.log", self.cwd / "err.log"
        with out.open("xb") as stdout, err.open("xb") as stderr:
            result = self.python("import sys; print('out'); print('err', file=sys.stderr)",
                                 timeout=20, stdout=stdout, stderr=stderr)
            self.assertFalse(stdout.closed or stderr.closed)  # The caller closes them.
        self.assertEqual((result.stdout, result.stderr), (b"", b""))
        self.assertEqual((out.read_bytes().strip(), err.read_bytes().strip()), (b"out", b"err"))

    def test_output_is_never_limited_by_size(self):
        code = "import sys; sys.stdout.buffer.write(b'a' * (17 * 1024 * 1024)); sys.stderr.write('b' * 3000)"
        kept = self.python(code, timeout=60)
        self.assertEqual((len(kept.stdout), len(kept.stderr)), (17 * MIB, 3000))
        path = self.cwd / "big.log"
        with path.open("xb") as stdout:
            written = self.python(code, timeout=60, stdout=stdout)
        self.assertEqual((path.stat().st_size, len(written.stderr), written.exit_code), (17 * MIB, 3000, 0))

    def test_a_failed_write_is_recorded_and_the_child_still_runs_to_the_end(self):
        marker = self.cwd / "finished"
        code = (f"import sys, pathlib; sys.stdout.buffer.write(b'x' * (4 * 1024 * 1024)); sys.stdout.flush(); "
                f"pathlib.Path({str(marker)!r}).write_text('done')")
        result = self.python(code, timeout=60, stdout=FailingFile())
        self.assertIn("No space left on device", result.output_error)
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(marker.exists())
        self.assertFalse(result.retained_pipes)

    def test_a_slow_log_file_is_not_a_retained_pipe(self):
        slow = SlowFile(1.0)
        result = self.python("import sys; [print(i, flush=True) for i in range(3)]", timeout=60, stdout=slow)
        self.assertFalse(result.retained_pipes)
        self.assertIsNone(result.output_error)
        self.assertEqual(slow.getvalue().split(), [b"0", b"1", b"2"])

    def test_a_descendant_keeping_the_pipes_is_retained_with_fast_or_slow_logs(self):
        for stdout in (None, SlowFile(0.5)):
            with self.subTest(slow=stdout is not None):
                started = time.monotonic()
                result = self.python(RETAIN, timeout=10, stdout=stdout)
                self.assertTrue(result.retained_pipes)
                self.assertLess(time.monotonic() - started, 8)

    def test_timeout_stops_the_process(self):
        result = self.python("import time; time.sleep(30)", timeout=0.3)
        self.assertTrue(result.timed_out)
        self.assertLess(result.duration_seconds, 10)

    def test_stdin_data_is_delivered(self):
        result = self.python("import sys; print(sys.stdin.read().upper())", timeout=20, data=b"packet")
        self.assertEqual(result.stdout.strip(), b"PACKET")

    def test_a_child_that_exits_before_reading_stdin_is_not_retained(self):
        # The buffered ``stdin`` close can retry a flush and raise ``BrokenPipeError``
        # after the child exits; the feeder's completion event must still be set so
        # cleanup treats the finished feeding as done, not as a retained pipe.
        result = self.python("import sys; sys.exit(0)", timeout=20, data=b"x" * 200000)
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.retained_pipes)

    def test_an_interrupt_during_feeder_start_is_a_cleanup_error(self):
        # The feeder is registered before ``Thread.start`` returns, so an interrupt
        # during startup still waits on it: the stop cannot be confirmed and the
        # interrupt is kept as the cleanup error's cause.
        real_start = threading.Thread.start
        starts = {"n": 0}

        def flaky_start(thread):
            real_start(thread)
            starts["n"] += 1
            if starts["n"] == 3:  # the two stream readers, then the stdin feeder
                raise KeyboardInterrupt()

        with patch.object(process_module, "stop_tree", lambda process: None):
            with patch.object(threading.Thread, "start", flaky_start):
                with self.assertRaises(ProcessCleanupError) as caught:
                    self.python("import time; time.sleep(12)", timeout=20, data=b"data")
        self.assertIsInstance(caught.exception.__cause__, KeyboardInterrupt)

    def test_launch_failure_is_a_devflow_error(self):
        with self.assertRaisesRegex(DevFlowError, "Could not launch"):
            run_bounded([str(self.cwd / "missing-program")], cwd=self.cwd, timeout=5)

    def test_a_child_that_never_stops_is_a_cleanup_error(self):
        # With the stop made a no-op, both post-loop waits expire: the child may still run.
        with patch.object(process_module, "stop_tree", lambda process: None):
            with self.assertRaises(ProcessCleanupError) as caught:
                self.python("import time; time.sleep(12)", timeout=0.2)
        self.assertIn("did not terminate", str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, subprocess.TimeoutExpired)

    def test_an_interrupt_reraises_once_the_child_and_pipes_end(self):
        real_sleep, calls = time.sleep, []

        def suspicious(seconds):
            calls.append(seconds)
            if len(calls) == 3:
                raise RuntimeError("monitor interrupted")
            return real_sleep(seconds)

        with patch.object(process_module.time, "sleep", suspicious):
            with self.assertRaisesRegex(RuntimeError, "monitor interrupted"):
                self.python("import time; time.sleep(30)", timeout=20)

    def test_an_interrupt_with_a_child_that_wont_stop_is_a_cleanup_error(self):
        class DeadThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("no threads")

        # The stop is a no-op and the child is still sleeping after the five-second wait.
        with patch.object(process_module, "stop_tree", lambda process: None), \
                patch.object(process_module.threading, "Thread", DeadThread):
            with self.assertRaises(ProcessCleanupError) as caught:
                self.python("import time; time.sleep(6)", timeout=20)
        self.assertIn("did not terminate", str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)

    def test_an_interrupt_with_pipes_held_open_is_a_cleanup_error(self):
        # The child reports its pipe-holding descendant's pid, then keeps running.
        pidfile = self.cwd / "descendant.pid"
        code = ("import subprocess, sys, tempfile, time; "
                "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'], "
                "cwd=tempfile.gettempdir()); "
                f"open(r'{pidfile}', 'w').write(str(p.pid)); time.sleep(30)")
        real_sleep, fired = time.sleep, []

        def suspicious(seconds):
            # Interrupt the monitor once, only once the descendant holds the pipes.
            if not fired and pidfile.exists():
                fired.append(1)
                raise RuntimeError("monitor interrupted")
            return real_sleep(seconds)

        try:
            with patch.object(process_module.time, "sleep", suspicious), \
                    patch.object(process_module, "stop_tree", lambda process: process.kill()):
                with self.assertRaises(ProcessCleanupError) as caught:
                    self.python(code, timeout=20)
            self.assertIn("kept its output pipes open", str(caught.exception))
            self.assertIsInstance(caught.exception.__cause__, RuntimeError)
        finally:
            if pidfile.exists():
                try:
                    os.kill(int(pidfile.read_text().strip()), signal.SIGTERM)
                except (OSError, ValueError):
                    pass

    def test_an_interrupt_in_the_exit_wait_still_stops_the_child(self):
        # An interrupt landing on the post-monitor exit confirmation goes
        # through the shared cleanup once, then propagates.
        real_wait, calls, stops = process_module.subprocess.Popen.wait, [], []
        real_stop = process_module.stop_tree

        def suspicious(process, timeout=None):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("exit wait interrupted")
            return real_wait(process, timeout=timeout)

        with patch.object(process_module.subprocess.Popen, "wait", suspicious), \
                patch.object(process_module, "stop_tree", lambda process: (stops.append(1), real_stop(process))):
            with self.assertRaisesRegex(RuntimeError, "exit wait interrupted"):
                self.python("import sys; sys.stdout.write('done'); sys.stdout.flush()", timeout=20)
        self.assertEqual(len(stops), 1)  # the cleanup ran, and exactly once

    def test_an_interrupt_waiting_on_held_pipes_is_a_cleanup_error(self):
        # The child reports its pipe-holding descendant's pid, then exits.
        pidfile = self.cwd / "descendant.pid"
        code = ("import subprocess, sys, tempfile; "
                "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'], "
                "cwd=tempfile.gettempdir()); "
                f"open(r'{pidfile}', 'w').write(str(p.pid))")
        real_wait, fired = process_module.threading.Event.wait, []

        def suspicious(event, timeout=None):
            # Interrupt the pipe-grace wait once, only once the descendant holds the pipes.
            if not fired and pidfile.exists():
                fired.append(1)
                raise RuntimeError("pipe wait interrupted")
            return real_wait(event, timeout)

        try:
            with patch.object(process_module.threading.Event, "wait", suspicious), \
                    patch.object(process_module, "stop_tree", lambda process: process.kill()):
                with self.assertRaises(ProcessCleanupError) as caught:
                    self.python(code, timeout=20)
            self.assertIn("kept its output pipes open", str(caught.exception))
            self.assertIsInstance(caught.exception.__cause__, RuntimeError)
        finally:
            if pidfile.exists():
                try:
                    os.kill(int(pidfile.read_text().strip()), signal.SIGTERM)
                except (OSError, ValueError):
                    pass

    def test_a_later_thread_start_failure_with_pipes_held_is_a_cleanup_error(self):
        # The child reports its pipe-holding descendant's pid, then keeps running.
        pidfile = self.cwd / "descendant.pid"
        code = ("import subprocess, sys, tempfile, time; "
                "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'], "
                "cwd=tempfile.gettempdir()); "
                f"open(r'{pidfile}', 'w').write(str(p.pid)); time.sleep(30)")
        calls = []

        class FlakyThread(process_module.threading.Thread):
            def start(self):
                calls.append(self)
                if len(calls) == 3:
                    # A later worker's start fails after both output readers
                    # started, once the child has spawned its descendant.
                    deadline = time.monotonic() + 10
                    while not pidfile.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    raise RuntimeError("start failed")
                super().start()

        try:
            with patch.object(process_module.threading, "Thread", FlakyThread), \
                    patch.object(process_module, "stop_tree", lambda process: process.kill()):
                with self.assertRaises(ProcessCleanupError) as caught:
                    self.python(code, timeout=20, data=b"x")
            self.assertIn("kept its output pipes open", str(caught.exception))
            self.assertIsInstance(caught.exception.__cause__, RuntimeError)
        finally:
            if pidfile.exists():
                try:
                    os.kill(int(pidfile.read_text().strip()), signal.SIGTERM)
                except (OSError, ValueError):
                    pass

    def test_a_later_worker_start_failure_still_drains_started_writers(self):
        # The last worker's start fails after both writers started, once the
        # child's output is on its way; started writers finish draining before
        # the exception propagates, so the caller's logs are complete when it
        # closes them.
        marker = self.cwd / "emitted"
        code = (f"import sys, time, pathlib; print('queued-line', flush=True); "
                f"pathlib.Path(r'{marker}').write_text('x'); time.sleep(30)")
        started, calls = [], []

        class FlakyThread(process_module.threading.Thread):
            def start(self):
                calls.append(self)
                if len(calls) == 5:  # the feeder: after both readers and both writers
                    deadline = time.monotonic() + 10
                    while not marker.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    raise RuntimeError("start failed")
                super().start()
                started.append(self)

        out, err = self.cwd / "out.log", self.cwd / "err.log"
        with out.open("xb") as stdout, err.open("xb") as stderr:
            with patch.object(process_module.threading, "Thread", FlakyThread):
                with self.assertRaisesRegex(RuntimeError, "start failed"):
                    self.python(code, timeout=20, stdout=stdout, stderr=stderr, data=b"x")
        self.assertEqual(out.read_bytes().strip(), b"queued-line")
        for thread in started[2:4]:  # the writers
            self.assertFalse(thread.is_alive())

    def test_a_child_that_fails_to_stop_still_drains_started_writers(self):
        # The last worker's start fails once the child's output is on its way;
        # the child does not stop within the wait, and the started writers
        # still finish draining before the error propagates.
        marker = self.cwd / "emitted2"
        pidfile = self.cwd / "child.pid"
        code = (f"import os, sys, time, pathlib; print('queued-line', flush=True); "
                f"pathlib.Path(r'{marker}').write_text('x'); "
                f"pathlib.Path(r'{pidfile}').write_text(str(os.getpid())); time.sleep(60)")
        started, calls = [], []

        class FlakyThread(process_module.threading.Thread):
            def start(self):
                calls.append(self)
                if len(calls) == 5:  # the feeder: after both readers and both writers
                    deadline = time.monotonic() + 10
                    while not marker.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    raise RuntimeError("start failed")
                super().start()
                started.append(self)

        out, err = self.cwd / "out2.log", self.cwd / "err2.log"
        try:
            # The stop is a no-op: the child is still running after the five-second wait.
            with out.open("xb") as stdout, err.open("xb") as stderr:
                with patch.object(process_module.threading, "Thread", FlakyThread), \
                        patch.object(process_module, "stop_tree", lambda process: None):
                    with self.assertRaisesRegex(ProcessCleanupError, "did not terminate"):
                        self.python(code, timeout=20, stdout=stdout, stderr=stderr, data=b"x")
            self.assertEqual(out.read_bytes().strip(), b"queued-line")
            for thread in started[2:4]:  # the writers
                self.assertFalse(thread.is_alive())
        finally:
            if pidfile.exists():
                try:
                    os.kill(int(pidfile.read_text().strip()), signal.SIGTERM)
                except (OSError, ValueError):
                    pass

    def test_without_data_the_standard_input_is_closed(self):
        result = self.python("import sys; print(repr(sys.stdin.read()))", timeout=20)
        self.assertEqual(result.stdout.strip(), b"''")
        self.assertFalse(result.timed_out)

    @unittest.skipIf(os.name == "nt", "process groups are POSIX; Windows stops the tree with taskkill /T")
    def test_children_get_their_own_group_and_are_stopped_as_a_group(self):
        own = self.python("import os; print(os.getpgid(0) == os.getpid())", timeout=20)
        self.assertEqual(own.stdout.strip(), b"True")
        with patch("devflow_core.common.process.os.killpg", wraps=os.killpg) as killpg:
            result = self.python("import time; time.sleep(30)", timeout=0.3)
        self.assertTrue(result.timed_out)
        killpg.assert_called()


if __name__ == "__main__":
    unittest.main()
