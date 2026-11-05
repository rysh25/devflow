"""The single child-process runner shared by checks, reviewers and Git."""
import io
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from devflow_core.common.errors import DevFlowError
from devflow_core.common.process import run_bounded

MIB = 1024 * 1024
RETAIN = "import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])"


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

    def test_launch_failure_is_a_devflow_error(self):
        with self.assertRaisesRegex(DevFlowError, "Could not launch"):
            run_bounded([str(self.cwd / "missing-program")], cwd=self.cwd, timeout=5)

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
