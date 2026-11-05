from __future__ import annotations

from pathlib import Path
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

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
        source = "import sys; sys.stdout.buffer.write(b'hello\\n'); sys.stderr.buffer.write(b'warning\\n')"
        result = run_checks(self.project, [self.check(source)], self.logs)
        self.assertEqual(result["status"], "passed")
        check = result["results"][0]
        self.assertEqual(check["exit_code"], 0)
        self.assertEqual(Path(check["stdout_path"]).read_text(), "hello\n")
        self.assertEqual(Path(check["stderr_path"]).read_text(), "warning\n")
        # The output is only in the logs; the result names them.
        self.assertFalse({"stdout", "stderr", "output_limit_exceeded"} & set(check))

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

    def test_large_output_passes_and_is_logged_in_full(self):
        source = "import sys; sys.stdout.buffer.write(b'x' * (3 * 1024 * 1024))"
        result = run_checks(self.project, [self.check(source)], self.logs)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(Path(result["results"][0]["stdout_path"]).stat().st_size, 3 * 1024 * 1024)

    def assert_error_with_two_empty_logs(self, result):
        record = result["results"][0]
        self.assertEqual(result["status"], "error")
        self.assertIsNone(record["exit_code"])
        for key in ("stdout_path", "stderr_path"):
            self.assertEqual(Path(record[key]).read_bytes(), b"")

    def test_missing_executable_is_error_not_pass(self):
        result = run_checks(self.project, [{"id": "missing", "argv": ["devflow-nonexistent-executable-92841"]}], self.logs)
        self.assert_error_with_two_empty_logs(result)

    def test_every_launch_failure_leaves_two_empty_logs(self):
        shim = run_checks(self.project, [{"id": "shim", "argv": ["npm.cmd"]}], self.base / "shim")
        self.assert_error_with_two_empty_logs(shim)
        with patch("devflow_core.common.process.subprocess.Popen", side_effect=OSError("cannot start")):
            failed = run_checks(self.project, [self.check("pass")], self.base / "popen")
        self.assert_error_with_two_empty_logs(failed)
        self.assertIn("cannot start", failed["results"][0]["error"])

    def test_command_argv_is_literal_not_shell(self):
        marker = self.project / "UNEXPECTED"
        value = f"hello; touch {marker}"
        result = run_checks(self.project, [{"id": "argv", "argv": [sys.executable, "-c", "import sys; print(sys.argv[1])", value]}], self.logs)
        self.assertEqual(Path(result["results"][0]["stdout_path"]).read_text().strip(), value)
        self.assertFalse(marker.exists())

    def test_native_shim_rejected(self):
        for executable in ("npm.cmd", "tool.BAT", "opencode.ps1"):
            with self.subTest(executable=executable):
                with self.assertRaises(DevFlowError):
                    bounded_run([executable], self.project, 1, io.BytesIO(), io.BytesIO())

    def test_cwd_is_project_relative(self):
        child = self.project / "tests"
        child.mkdir()
        result = run_checks(self.project, [self.check("import os; print(os.getcwd())", cwd="tests")], self.logs)
        self.assertEqual(Path(Path(result["results"][0]["stdout_path"]).read_text().strip()), child)

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
            [self.check("pass", shell=True)],
        ]
        for checks in invalid:
            with self.subTest(checks=checks):
                with self.assertRaises(DevFlowError):
                    validate_checks(checks)

    def test_unknown_fields_are_named(self):
        with self.assertRaisesRegex(DevFlowError, r"^Invalid check fields \(unknown: max_output_bytes, shell\); "
                                                  "see the project settings in the DevFlow README$"):
            validate_checks([self.check("pass", shell=True, max_output_bytes=1024)])

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
        result = bounded_run([sys.executable, "-c", source], self.project, 2, io.BytesIO(), io.BytesIO())
        self.assertLess(time.monotonic() - started, 5)
        self.assertIn("retained output pipes", result["error"])


if __name__ == "__main__":
    unittest.main()
