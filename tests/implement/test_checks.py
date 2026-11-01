from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys
import tempfile
import time
import unittest

from devflow_core.implement.checks import bounded_run, run_checks, validate_checks
from devflow_core.common.errors import DevFlowError


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="devflow-implement-check-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.project = self.base / "project with spaces 日本語"
        self.project.mkdir()
        self.logs = self.base / "logs"

    def check(self, source, **kwargs):
        return {"id": "unit-tests", "argv": [sys.executable, "-c", source], **kwargs}

    def test_success_saves_stdout_and_stderr(self):
        result = run_checks(self.project, [self.check("import sys; print('hello'); print('warning', file=sys.stderr)")], self.logs)
        self.assertEqual(result["status"], "passed")
        check = result["results"][0]
        self.assertEqual(check["exit_code"], 0)
        self.assertEqual(check["stdout"], "hello\n")
        self.assertEqual(Path(check["stderr_path"]).read_text(), "warning\n")

    def test_failure_cannot_pass(self):
        result = run_checks(self.project, [self.check("raise SystemExit(7)")], self.logs)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["results"][0]["exit_code"], 7)

    def test_optional_failure_is_still_visible(self):
        result = run_checks(self.project, [self.check("raise SystemExit(1)", required=False)], self.logs)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["results"][0]["status"], "failed")

    def test_no_config_is_not_a_pass(self):
        self.assertEqual(run_checks(self.project, [], self.logs), {"status": "not_configured", "results": []})

    def test_timeout_fails_and_returns_promptly(self):
        started = time.monotonic()
        result = run_checks(self.project, [self.check("import time; time.sleep(10)", timeout_seconds=1)], self.logs)
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["results"][0]["timed_out"])

    def test_stdout_overflow_is_error_even_if_process_exits_zero(self):
        result = run_checks(self.project, [self.check("print('x' * 200000)", max_output_bytes=100)], self.logs)
        record = result["results"][0]
        self.assertEqual(result["status"], "error")
        self.assertTrue(record["output_limit_exceeded"])
        self.assertLessEqual(len(record["stdout"].encode()) + len(record["stderr"].encode()), 100)

    def test_missing_executable_is_error_not_pass(self):
        result = run_checks(self.project, [{"id": "missing", "argv": ["devflow-nonexistent-executable-92841"]}], self.logs)
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["results"][0]["exit_code"])

    def test_command_argv_is_literal_not_shell(self):
        marker = self.project / "UNEXPECTED"
        value = f"hello; touch {marker}"
        result = run_checks(self.project, [{"id": "argv", "argv": [sys.executable, "-c", "import sys; print(sys.argv[1])", value]}], self.logs)
        self.assertEqual(result["results"][0]["stdout"].strip(), value)
        self.assertFalse(marker.exists())

    def test_native_shim_rejected(self):
        for executable in ("npm.cmd", "tool.BAT", "opencode.ps1"):
            with self.subTest(executable=executable):
                with self.assertRaises(DevFlowError):
                    bounded_run([executable], self.project, 1)

    def test_cwd_is_project_relative(self):
        child = self.project / "tests"
        child.mkdir()
        result = run_checks(self.project, [self.check("import os; print(os.getcwd())", cwd="tests")], self.logs)
        self.assertEqual(Path(result["results"][0]["stdout"].strip()), child)

    def test_all_checks_validated_before_any_launch(self):
        first = self.check("from pathlib import Path; Path('UNEXPECTED').write_text('x')")
        for cwd in ("../outside", "/tmp", "C:/tmp", "a/../../b"):
            with self.subTest(cwd=cwd):
                with self.assertRaises(DevFlowError):
                    run_checks(self.project, [first, {"id": "unsafe", "argv": [sys.executable], "cwd": cwd}], self.logs)
        self.assertFalse((self.project / "UNEXPECTED").exists())

    def test_logs_are_not_overwritten(self):
        run_checks(self.project, [self.check("print('first')")], self.logs)
        with self.assertRaises(DevFlowError):
            run_checks(self.project, [self.check("print('second')")], self.logs)
        self.assertEqual((self.logs / "unit-tests.stdout.log").read_text(), "first\n")

    def test_invalid_config_rejected(self):
        invalid = [
            "pytest", [{"id": "unit", "argv": "pytest -q"}],
            [{"id": "../unit", "argv": ["pytest"]}],
            [self.check("pass"), self.check("pass")],
            [self.check("pass", required="yes")],
            [self.check("pass", timeout_seconds=True)],
            [self.check("pass", timeout_seconds=0)],
            [self.check("pass", max_output_bytes=0)],
            [self.check("pass", shell=True)],
        ]
        for checks in invalid:
            with self.subTest(checks=checks):
                with self.assertRaises(DevFlowError):
                    validate_checks(checks)

    def test_symlink_cwd_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        link = self.project / "link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Symlink creation unavailable: {exc}")
        with self.assertRaises(DevFlowError):
            run_checks(self.project, [self.check("pass", cwd="link")], self.logs)

    @unittest.skipIf(os.name == "nt", "POSIX process-group test; Windows requires live platform validation")
    def test_descendant_retaining_pipes_does_not_hang(self):
        source = "import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])"
        started = time.monotonic()
        result = bounded_run([sys.executable, "-c", source], self.project, 2)
        self.assertLess(time.monotonic() - started, 5)
        self.assertIn("retained output pipes", result["error"])


if __name__ == "__main__":
    unittest.main()
