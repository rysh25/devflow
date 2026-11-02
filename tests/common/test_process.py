"""The single bounded child-process runner shared by checks and reviewers."""
import os
import subprocess
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


@unittest.skipIf(os.name == "nt", "process groups are POSIX; Windows stops the tree with taskkill /T")
class CallerGroupTests(unittest.TestCase):
    """new_session=False: the child joins the caller's group and is killed alone."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="devflow process group ")
        self.addCleanup(temporary.cleanup)
        self.cwd = Path(temporary.name)

    def python(self, code, **options):
        return run_bounded([sys.executable, "-c", code], cwd=self.cwd, new_session=False, **options)

    def test_the_child_stays_in_the_callers_group(self):
        result = self.python("import os; print(os.getpgid(0))", timeout=20, limit=4096)
        self.assertEqual(int(result.stdout), os.getpgid(0))

    def test_default_children_get_their_own_group_and_are_stopped_as_a_group(self):
        with patch("devflow_core.common.process.os.killpg", wraps=os.killpg) as killpg:
            result = run_bounded([sys.executable, "-c", "import time; time.sleep(30)"], cwd=self.cwd,
                                 timeout=0.3, limit=4096)
        self.assertTrue(result.timed_out)
        killpg.assert_called()

    def test_no_kill_path_calls_killpg(self):
        cases = {
            "timeout": ("import time; time.sleep(30)", {"timeout": 0.3, "limit": 4096}),
            "overflow": ("import sys, time; sys.stdout.write('a' * 9000); sys.stdout.flush(); time.sleep(30)",
                         {"timeout": 20, "limit": 4096}),
            # The parent exits; a grandchild keeps the output pipes open.
            "retained": ("import subprocess, sys; subprocess.Popen([sys.executable, '-c', "
                         "'import time; time.sleep(3)'])", {"timeout": 20, "limit": 4096}),
        }
        for name, (code, options) in cases.items():
            with self.subTest(path=name), patch("devflow_core.common.process.os.killpg") as killpg:
                self.python(code, **options)
                killpg.assert_not_called()

    def test_an_exception_while_waiting_kills_only_the_child(self):
        started = []
        real_popen = subprocess.Popen

        def popen(*args, **kwargs):
            started.append(real_popen(*args, **kwargs))
            return started[-1]

        with patch("devflow_core.common.process.os.killpg") as killpg, \
                patch("devflow_core.common.process.subprocess.Popen", side_effect=popen), \
                patch("devflow_core.common.process.time.sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.python("import time; time.sleep(30)", timeout=20, limit=4096)
        killpg.assert_not_called()
        # The child was killed (not left sleeping); reaping it is the test's cleanup.
        self.assertEqual(len(started), 1)
        self.assertNotEqual(started[0].wait(timeout=5), 0)

    def test_a_wait_timeout_kills_only_the_child(self):
        real_wait = subprocess.Popen.wait
        calls = []

        def wait(process, timeout=None):
            calls.append(timeout)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(process.args, timeout)
            return real_wait(process, timeout=timeout)

        with patch("devflow_core.common.process.os.killpg") as killpg, patch.object(subprocess.Popen, "wait", wait):
            self.python("pass", timeout=20, limit=4096)
        killpg.assert_not_called()


if __name__ == "__main__":
    unittest.main()
