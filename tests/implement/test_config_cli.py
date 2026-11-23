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

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "devflow" / "scripts" / "devflow_core" / "implement"

from devflow_core.cli import main
from devflow_core.common.errors import DevFlowError
from devflow_core.common.settings import read_project_settings
from devflow_core.common.storage import atomic_write_json, read_json
from devflow_core.implement import WORKFLOW
from devflow_core.implement import WORKFLOW
from devflow_core.implement.config import validate_config
from devflow_core.workflows import WORKFLOWS


class ConfigCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="devflow implement cli ")
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name).resolve()
        self.assertEqual(WORKFLOW.resource_dir, PLUGIN)
        self.config = read_json(PLUGIN / "defaults.json")
        # An explicit root avoids inheriting any unrelated temp-parent state.
        (self.project / ".git").mkdir()

    def load_config(self):
        return WORKFLOW.load_config(read_project_settings(self.project, "claude", WORKFLOWS))

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--host", "claude", "--project", str(self.project), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_defaults_have_requested_order_models_efforts_and_ceiling(self):
        config = validate_config(self.config)
        self.assertEqual(config["max_reviews"], 6)
        self.assertEqual([(r["kind"], r["model"], r["effort"]) for r in config["reviewers"]], [
            ("codex", "gpt-6.2-sol", "high"),
            ("opencode", "opencode-go/deepseek-v4.1-flash", "high")])
        self.assertEqual(config["checks"], [])

    def test_config_returns_independent_copy(self):
        config = validate_config(self.config)
        config["reviewers"][0]["model"] = "changed"
        self.assertNotEqual(config, self.config)

    def test_unknown_fields_and_boolean_schema_rejected(self):
        for field, value in (("schema_version", True), ("max_reviews", True), ("max_reviews", 0), ("max_reviews", 100), ("unexpected", 1)):
            config = {**self.config, field: value}
            with self.subTest(field=field, value=value):
                with self.assertRaises(DevFlowError):
                    validate_config(config)

    def test_one_or_more_reviewers_of_any_kinds_up_to_max_reviews(self):
        config = copy.deepcopy(self.config)
        config["reviewers"][1]["kind"] = "codex"
        self.assertEqual([r["kind"] for r in validate_config(config)["reviewers"]], ["codex", "codex"])
        config["reviewers"] = config["reviewers"][:1]
        config["max_reviews"] = 1
        self.assertEqual([r["id"] for r in validate_config(config)["reviewers"]], ["codex"])
        config["reviewers"] = []
        with self.assertRaisesRegex(DevFlowError, "one reviewer up to max_reviews"):
            validate_config(config)
        # A third reviewer is allowed, up to max_reviews.
        three = copy.deepcopy(self.config)
        three["reviewers"].append(dict(three["reviewers"][0], id="codex-second"))
        self.assertEqual([r["id"] for r in validate_config(three)["reviewers"]], ["codex", "opencode", "codex-second"])
        three["max_reviews"] = 2
        with self.assertRaises(DevFlowError):
            validate_config(three)
        duplicate = copy.deepcopy(self.config)
        duplicate["reviewers"].append(copy.deepcopy(duplicate["reviewers"][0]))
        with self.assertRaisesRegex(DevFlowError, "unique"):
            validate_config(duplicate)

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
                with self.assertRaises(DevFlowError):
                    validate_config({**self.config, "checks": [check]})

    def test_overlay_merges_snapshot_but_replaces_arrays(self):
        overlay = {"snapshot": {"mode": "files", "include": ["src/**"]}, "checks": [
            {"id": "tests", "argv": [sys.executable, "-m", "unittest"]}]}
        atomic_write_json(self.project / "devflow.config.json", {"schema_version": 1, "implement": overlay})
        with patch("subprocess.Popen") as processes:
            config = self.load_config()
        processes.assert_not_called()
        self.assertEqual(config["snapshot"]["mode"], "files")
        self.assertEqual(config["snapshot"]["max_packet_bytes"], self.config["snapshot"]["max_packet_bytes"])
        self.assertEqual(config["checks"], overlay["checks"])
        self.assertFalse((self.project / ".devflow").exists())

    def test_settings_removed_in_0_6_0_are_errors_that_name_them(self):
        cases = (
            ({"implement": {"snapshot": {"max_files": 9, "max_file_bytes": 9, "max_total_bytes": 9}}},
             r"^devflow.config.json: implement: Invalid snapshot fields \(unknown: max_file_bytes, max_files, max_total_bytes\); "
             r"see the project settings in the DevFlow README$"),
            ({"implement": {"checks": [{"id": "tests", "argv": [sys.executable], "max_output_bytes": 1024}]}},
             r"^devflow.config.json: implement: Invalid check fields \(unknown: max_output_bytes\); see the project settings in the DevFlow README$"),
            ({"reviewers": [{**reviewer, "max_output_bytes": 1024} for reviewer in self.config["reviewers"]]},
             r"unknown: max_output_bytes\).*README"),
        )
        for document, message in cases:
            with self.subTest(document=document):
                atomic_write_json(self.project / "devflow.config.json", {"schema_version": 1, **document})
                with self.assertRaisesRegex(DevFlowError, message):
                    self.load_config()

    def test_a_snapshot_mode_other_than_git_or_files_is_named(self):
        with self.assertRaisesRegex(DevFlowError, r"^snapshot.mode must explicitly be git or files$"):
            validate_config({**self.config, "snapshot": {**self.config["snapshot"], "mode": "auto"}})

    def test_doctor_reports_offline_limits_and_missing_checks(self):
        with patch("devflow_core.common.reviewers.preflight", return_value={"offline": True}) as probe:
            code, output, error = self.invoke("implement", "doctor")
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
        result = self.load_config()
        self.assertEqual(result["reviewers"], reviewers)
        self.assertEqual(result["max_reviews"], 4)
        self.assertEqual(result["max_check_attempts"], 2)
        self.assertNotIn("grill_skill", result)
        self.assertNotIn("publish", result)

    def test_legacy_configuration_is_not_automatically_imported(self):
        atomic_write_json(self.project / ("build" + "flow.config.json"), {
            "snapshot": {"mode": "files"}, "max_reviews": 1,
        })
        self.assertEqual(self.load_config(), self.config)

    def test_unknown_top_level_unified_config_field_is_rejected(self):
        atomic_write_json(self.project / "devflow.config.json", {
            "schema_version": 1, "implment": {"snapshot": {"mode": "files"}},
        })
        # The registry owner (CLI/router) validates top-level keys for every command.
        code, output, _ = self.invoke("implement", "doctor")
        self.assertEqual(code, 2)
        self.assertIn("Unknown devflow.config.json keys", output)

    def test_selected_document_errors_are_reported_on_utf8_stdout(self):
        nested = "[" * 700 + "0" + "]" * 700
        cases = (
            ("devflow.config.json", r'{"\ud800": 1, "\ud800": 2}', "Duplicate JSON key"),
            ("devflow.config.local.json", '{"implement": {"snapshot": {"include": ' + nested + '}}}',
             "nested too deeply"),
            ("devflow.config.local.json", r'{"\ud800": 1}', "UTF-8"),
        )
        runner = PLUGIN.parents[1] / "devflow.py"
        for name, document, error in cases:
            with self.subTest(error=error):
                path = self.project / name
                path.write_text(document, encoding="utf-8")
                try:
                    result = subprocess.run(
                        [sys.executable, str(runner), "--host", "codex", "--project", str(self.project),
                         "implement", "doctor"], capture_output=True, timeout=30)
                finally:
                    path.unlink()
                self.assertEqual((result.returncode, result.stderr), (2, b""))
                diagnostic = json.loads(result.stdout.decode("utf-8"))
                self.assertEqual(diagnostic["error"], "DevFlowError")
                self.assertIn(name, diagnostic["message"])
                self.assertIn(error, diagnostic["message"])

    def test_workflow_review_budget_stays_bounded_when_inherited(self):
        atomic_write_json(self.project / "devflow.config.json", {"schema_version": 1, "max_reviews": 100})
        with self.assertRaises(DevFlowError):
            self.load_config()

    def started_run(self, session="claude-cli-session"):
        """A real files-mode run started through the hook and the runner (reviewer checks mocked)."""
        from devflow_core import router
        from devflow_core.common.paths import PLUGIN_ROOT
        atomic_write_json(self.project / "devflow.config.json",
                          {"schema_version": 1, "implement": {"snapshot": {"mode": "files"}}})
        plan = self.project / "plan.md"
        plan.write_text("# Plan\n\nDo it.\n", encoding="utf-8")
        router.handle_hook({"hook_event_name": "UserPromptSubmit", "cwd": str(self.project), "session_id": session,
                            "prompt": f'/devflow:implement "{plan}"'}, PLUGIN_ROOT, host="claude")
        with patch("devflow_core.common.reviewers.preflight", return_value={}):
            return router.runner_start(self.project, session, "implement", str(plan), host="claude")

    def test_doctor_checks_reviewers_in_the_project_without_a_run(self):
        with patch("devflow_core.common.reviewers.preflight", return_value={"offline": True}) as probe:
            code, output, error = self.invoke("implement", "doctor")
        self.assertEqual((code, error), (0, ""))
        result = json.loads(output)
        self.assertEqual({call.kwargs["cwd"] for call in probe.call_args_list}, {self.project})
        self.assertEqual(result["reviewer_cwd"], str(self.project))

    def test_doctor_checks_reviewers_in_the_selected_runs_verified_workspace(self):
        state = self.started_run()
        for args in (("--run", state["run_id"], "implement", "doctor"),        # an explicit run
                     ("--session", "claude-cli-session", "implement", "doctor")):  # the session's run
            with self.subTest(args=args), \
                    patch("devflow_core.common.reviewers.preflight", return_value={"offline": True}) as probe, \
                    patch("devflow_core.implement.workspace.verify_workspace",
                          return_value=self.project) as verified:
                code, output, error = self.invoke(*args)
                self.assertEqual((code, error), (0, ""))
                result = json.loads(output)
                self.assertEqual(result["run_id"], state["run_id"])
                verified.assert_called_once()
                self.assertEqual({call.kwargs["cwd"] for call in probe.call_args_list}, {self.project})
                self.assertEqual(result["reviewer_cwd"], str(self.project))

    def test_doctor_never_checks_reviewers_in_a_workspace_that_fails_verification(self):
        state = self.started_run()
        with patch("devflow_core.common.reviewers.preflight") as probe, \
                patch("devflow_core.implement.workspace.verify_workspace",
                      side_effect=DevFlowError("The workspace was replaced")):
            code, output, error = self.invoke("--run", state["run_id"], "implement", "doctor")
        self.assertEqual((code, error), (0, ""))
        result = json.loads(output)
        probe.assert_not_called()
        self.assertFalse(result["ready"])
        self.assertEqual({(item["ready"], item["error"]) for item in result["reviewers"]},
                         {(False, "Workspace verification failed before any reviewer CLI ran: "
                                  "The workspace was replaced")})

    def test_doctor_reports_missing_cli_not_a_false_ready(self):
        with patch("devflow_core.common.reviewers.preflight", side_effect=DevFlowError("CLI missing")):
            code, output, error = self.invoke("implement", "doctor")
        self.assertEqual((code, error), (0, ""))
        result = json.loads(output)
        self.assertFalse(result["ready"])
        self.assertEqual({item["error"] for item in result["reviewers"]}, {"CLI missing"})

    def test_status_requires_a_bound_or_explicit_run_without_creating_a_run(self):
        for args in (("implement", "status"), ("--session", "claude-cli-session", "implement", "status")):
            with self.subTest(args=args):
                code, output, error = self.invoke(*args)
                self.assertEqual((code, error), (2, ""))
                result = json.loads(output)
                self.assertEqual(result["error"], "DevFlowError")
                self.assertIn("No implement run is bound", result["message"])
                self.assertIn("/devflow:resume RUN_ID", result["message"])
                self.assertFalse(result["automatic_retry"])
        code, output, error = self.invoke("--run", "20261101T123456Z-123456abcdef", "implement", "status")
        self.assertEqual((code, error), (2, ""))
        self.assertIn("missing", json.loads(output)["message"])
        self.assertFalse((self.project / ".devflow").exists())


if __name__ == "__main__":
    unittest.main()
