"""Configuration and real entrypoint behavior without live model access."""
from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "devflow" / "components" / "buildflow"
SCRIPT = PLUGIN / "scripts" / "buildflow.py"
sys.path.insert(0, str(PLUGIN / "scripts"))

from buildflow_core.cli import main
from buildflow_core.config import load_config, validate_config
from buildflow_core.storage import BuildFlowError, atomic_write_json, read_json


class ConfigCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="buildflow cli ")
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name).resolve()
        self.config = read_json(PLUGIN / "config" / "defaults.json")
        # An explicit root avoids inheriting any unrelated temp-parent state.
        (self.project / ".git").mkdir()

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--project", str(self.project), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def process(self, *args, payload=""):
        return subprocess.run([sys.executable, "-B", str(SCRIPT), "--project", str(self.project), *args],
                              input=payload, capture_output=True, encoding="utf-8", timeout=20)

    def test_defaults_have_requested_order_models_efforts_and_ceiling(self):
        config = validate_config(self.config)
        self.assertEqual(config["max_reviews"], 6)
        self.assertEqual([(r["kind"], r["model"], r["effort"]) for r in config["reviewers"]], [
            ("codex", "gpt-6.1-sol", "xhigh"),
            ("opencode", "opencode-go/deepseek-v4.1-flash", "high")])
        self.assertEqual(config["checks"], [])

    def test_config_returns_independent_copy(self):
        config = validate_config(self.config)
        config["reviewers"][0]["model"] = "changed"
        self.assertNotEqual(config, self.config)

    def test_unknown_fields_and_boolean_schema_rejected(self):
        for field, value in (("schema_version", True), ("max_reviews", True), ("max_reviews", 7), ("unexpected", 1)):
            config = {**self.config, field: value}
            with self.subTest(field=field, value=value):
                with self.assertRaises(BuildFlowError):
                    validate_config(config)

    def test_exactly_two_different_reviewer_kinds_required(self):
        config = copy.deepcopy(self.config)
        config["reviewers"][1]["kind"] = "codex"
        with self.assertRaises(BuildFlowError):
            validate_config(config)
        config["reviewers"] = config["reviewers"][:1]
        with self.assertRaises(BuildFlowError):
            validate_config(config)

    def test_invalid_check_contract_fails_at_config_load(self):
        invalid = [
            {"id": "UnitTests", "argv": [sys.executable]},
            {"id": "test", "argv": "python test.py"},
            {"id": "test", "argv": [sys.executable], "cwd": "../outside"},
            {"id": "test", "argv": [sys.executable], "cwd": "/absolute"},
            {"id": "test", "argv": [sys.executable], "required": False},
            {"id": "test", "argv": [sys.executable], "timeout_seconds": True},
        ]
        for check in invalid:
            with self.subTest(check=check):
                with self.assertRaises(BuildFlowError):
                    validate_config({**self.config, "checks": [check]})

    def test_overlay_merges_snapshot_but_replaces_arrays(self):
        overlay = {"snapshot": {"mode": "files", "include": ["src/**"]}, "checks": [
            {"id": "tests", "argv": [sys.executable, "-m", "unittest"]}]}
        atomic_write_json(self.project / "devflow.config.json", {"schema_version": 1, "implement": overlay})
        with patch("subprocess.Popen") as processes:
            config = load_config(self.project, PLUGIN)
        processes.assert_not_called()
        self.assertEqual(config["snapshot"]["mode"], "files")
        self.assertEqual(config["snapshot"]["max_files"], self.config["snapshot"]["max_files"])
        self.assertEqual(config["checks"], overlay["checks"])
        self.assertFalse((self.project / ".devflow").exists())

    def test_doctor_reports_offline_limits_and_missing_checks(self):
        with patch("buildflow_core.cli.adapters.preflight", return_value={"offline": True}) as probe:
            code, output, error = self.invoke("doctor")
        self.assertEqual((code, error), (0, ""))
        result = json.loads(output)
        self.assertTrue(result["ready"])
        self.assertEqual(probe.call_count, 2)
        self.assertTrue(result["warnings"])
        self.assertIn("CLI authentication and model access", result["not_verified"])
        self.assertFalse((self.project / ".devflow").exists())

    def test_shared_settings_merge_without_plan_workflow_dependency(self):
        reviewers = copy.deepcopy(self.config["reviewers"])
        reviewers[0]["model"] = "example-codex-model"
        atomic_write_json(self.project / "devflow.config.json", {
            "schema_version": 1, "reviewers": reviewers, "max_reviews": 4,
            "plan": {"grill_skill": "some-independent-interview", "publish": {"enabled": False}},
            "implement": {"snapshot": {"mode": "files"}, "max_check_attempts": 2},
        })
        result = load_config(self.project, PLUGIN)
        self.assertEqual(result["reviewers"], reviewers)
        self.assertEqual(result["max_reviews"], 4)
        self.assertEqual(result["max_check_attempts"], 2)
        self.assertNotIn("grill_skill", result)
        self.assertNotIn("publish", result)

    def test_legacy_configuration_is_not_automatically_imported(self):
        atomic_write_json(self.project / "buildflow.config.json", {
            "snapshot": {"mode": "files"}, "max_reviews": 1,
        })
        self.assertEqual(load_config(self.project, PLUGIN), self.config)

    def test_unknown_top_level_unified_config_field_is_rejected(self):
        atomic_write_json(self.project / "devflow.config.json", {
            "schema_version": 1, "implment": {"snapshot": {"mode": "files"}},
        })
        with self.assertRaisesRegex(BuildFlowError, "Unknown devflow.config.json keys"):
            load_config(self.project, PLUGIN)

    def test_workflow_review_budget_stays_bounded_when_inherited(self):
        atomic_write_json(self.project / "devflow.config.json", {"schema_version": 1, "max_reviews": 7})
        with self.assertRaises(BuildFlowError):
            load_config(self.project, PLUGIN)

    def test_doctor_reports_missing_cli_not_a_false_ready(self):
        with patch("buildflow_core.cli.adapters.preflight", side_effect=BuildFlowError("CLI missing")):
            code, output, error = self.invoke("doctor")
        self.assertEqual((code, error), (0, ""))
        self.assertFalse(json.loads(output)["ready"])

    def test_status_requires_explicit_run_without_creating_a_run(self):
        code, output, error = self.invoke("status")
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("--run RUN_ID", error)
        self.assertFalse((self.project / ".devflow").exists())

    def test_real_entrypoint_help_and_version(self):
        for args, expected in ((["--help"], "bounded independent reviews"), (["--version"], "BuildFlow 0.1.2")):
            with self.subTest(args=args):
                result = self.process(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, result.stdout)

    def test_real_unrelated_hook_is_silent_and_inert(self):
        payload = {"hook_event_name": "UserPromptSubmit", "session_id": "cli-test-session", "cwd": str(self.project), "prompt": "ordinary unrelated conversation"}
        result = self.process("hook", payload=json.dumps(payload))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertFalse((self.project / ".devflow").exists())

    def test_real_malformed_hook_fails_closed(self):
        result = self.process("hook", payload="not json")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("BuildFlow:", result.stderr)


if __name__ == "__main__":
    unittest.main()
