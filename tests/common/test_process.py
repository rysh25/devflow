"""The single bounded child-process runner shared by checks and reviewers."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from devflow_core.common.errors import DevFlowError
from devflow_core.common.process import run_bounded


class RunBoundedTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="devflow process ")
        self.addCleanup(temporary.cleanup)
        self.cwd = Path(temporary.name)

    def python(self, code, **options):
        return run_bounded([sys.executable, "-c", code], cwd=self.cwd, **options)

    def test_completes_with_exit_code_and_both_streams(self):
        result = self.python("import sys; print('out'); print('err', file=sys.stderr); raise SystemExit(3)",
                             timeout=20, limit=4096)
        self.assertEqual((result.exit_code, result.stdout.strip(), result.stderr.strip()), (3, b"out", b"err"))
        self.assertFalse(result.timed_out or result.output_limit_exceeded or result.retained_pipes)

    def test_the_limit_covers_stdout_and_stderr_together(self):
        result = self.python("import sys; sys.stdout.write('a' * 3000); sys.stdout.flush(); sys.stderr.write('b' * 3000)",
                             timeout=20, limit=4096)
        self.assertTrue(result.output_limit_exceeded)
        self.assertLessEqual(len(result.stdout) + len(result.stderr), 4096)

    def test_a_per_stream_limit_counts_each_stream_separately(self):
        code = "import sys; sys.stdout.write('a' * 3000); sys.stdout.flush(); sys.stderr.write('b' * 3000)"
        result = self.python(code, timeout=20, limit=4096, per_stream=True)
        self.assertFalse(result.output_limit_exceeded)
        self.assertEqual((len(result.stdout), len(result.stderr)), (3000, 3000))

    def test_timeout_stops_the_process(self):
        result = self.python("import time; time.sleep(30)", timeout=0.3, limit=4096)
        self.assertTrue(result.timed_out)
        self.assertLess(result.duration_seconds, 10)

    def test_stdin_data_is_delivered(self):
        result = self.python("import sys; print(sys.stdin.read().upper())", timeout=20, limit=4096, data=b"packet")
        self.assertEqual(result.stdout.strip(), b"PACKET")

    def test_launch_failure_is_a_devflow_error(self):
        with self.assertRaisesRegex(DevFlowError, "Could not launch"):
            run_bounded([str(self.cwd / "missing-program")], cwd=self.cwd, timeout=5, limit=1024)


    def test_without_data_the_standard_input_is_closed(self):
        result = self.python("import sys; print(repr(sys.stdin.read()))", timeout=20, limit=4096)
        self.assertEqual(result.stdout.strip(), b"''")
        self.assertFalse(result.timed_out)

    @unittest.skipIf(os.name == "nt", "process groups are POSIX; Windows stops the tree with taskkill /T")
    def test_children_get_their_own_group_and_are_stopped_as_a_group(self):
        own = self.python("import os; print(os.getpgid(0) == os.getpid())", timeout=20, limit=4096)
        self.assertEqual(own.stdout.strip(), b"True")
        with patch("devflow_core.common.process.os.killpg", wraps=os.killpg) as killpg:
            result = self.python("import time; time.sleep(30)", timeout=0.3, limit=4096)
        self.assertTrue(result.timed_out)
        killpg.assert_called()


if __name__ == "__main__":
    unittest.main()
