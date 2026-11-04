"""Actual offline uv smoke tests; no provider or model is called."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "devflow"
UV = shutil.which("uv")


@unittest.skipUnless(UV, "uv is not installed; static and Python entrypoint tests still apply")
class UvEntrypointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="devflow uv smoke ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.project = self.base / "project"
        self.project.mkdir()
        self.environment = {key: value for key, value in os.environ.items()
                            if not key.startswith("UV_") and key not in {"VIRTUAL_ENV", "CONDA_PREFIX"}}
        self.environment["UV_CACHE_DIR"] = str(self.base / "cache")
        self.environment["PYTHONDONTWRITEBYTECODE"] = "1"

    def run_cli(self, *arguments, plugin=PLUGIN, data=None):
        return subprocess.run(
            [UV, "run", "--offline", "--no-project", "--no-config", "--no-python-downloads",
             str(plugin / "scripts" / "devflow.py"), *arguments],
            cwd=self.project, env=self.environment,
            input=json.dumps(data) if data is not None else None,
            capture_output=True, text=True, encoding="utf-8", timeout=45, shell=False,
        )

    def test_real_uv_entrypoint_without_exact_python_pin(self):
        result = self.run_cli("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "devflow 0.5.1")

    def test_unrelated_hook_does_not_create_project_state(self):
        before = sorted(str(path.relative_to(self.project)) for path in self.project.rglob("*"))
        result = self.run_cli("hook", data={
            "hook_event_name": "UserPromptSubmit", "cwd": str(self.project),
            "session_id": "synthetic-smoke-only", "prompt": "Explain a function.",
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before, sorted(str(path.relative_to(self.project)) for path in self.project.rglob("*")))
        self.assertIn(result.stdout.strip(), ("", "{}"))

    def test_malformed_hook_exits_1_without_stdout_and_a_failed_stop_exits_0(self):
        # Exit status 2 would make Claude Code block the tool or the prompt.
        for raw, expected in (("not json", 1), (json.dumps({"hook_event_name": "Stop"}), 0)):
            with self.subTest(raw=raw):
                result = subprocess.run(
                    [UV, "run", "--offline", "--no-project", "--no-config", "--no-python-downloads",
                     str(PLUGIN / "scripts" / "devflow.py"), "hook"],
                    cwd=self.project, env=self.environment, input=raw,
                    capture_output=True, text=True, encoding="utf-8", timeout=45, shell=False)
                self.assertEqual(result.returncode, expected)
                self.assertEqual(result.stdout, "")
                self.assertIn("DevFlow hook failed", result.stderr)
        self.assertFalse((self.project / ".devflow").exists())

    def test_project_python_pin_does_not_override_plugin_requirement(self):
        (self.project / ".python-version").write_text("3.8\n", encoding="utf-8")
        result = self.run_cli("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "devflow 0.5.1")

    def test_external_plugin_path_with_spaces_and_unicode(self):
        copied = self.base / "DevFlow plugin 日本語"
        shutil.copytree(PLUGIN, copied, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        result = self.run_cli("--version", plugin=copied)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "devflow 0.5.1")
        self.assertFalse(list(copied.rglob("__pycache__")))


if __name__ == "__main__":
    unittest.main()
