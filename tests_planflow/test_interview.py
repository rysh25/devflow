"""Read-only interview diagnostics with isolated local skill fixtures."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "devflow" / "components" / "planflow"
sys.path.insert(0, str(PLUGIN / "scripts"))

from planflow_core import cli, interview
from planflow_core.config import diagnose
from planflow_core.engine import Engine
from planflow_core.storage import atomic_json, read_json


class InterviewFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="devflow interview ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.project = self.base / "project"
        self.project.mkdir()
        (self.project / ".git").mkdir()
        self.personal = self.base / "personal config"
        self.environment = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.personal)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def skill(self, content="---\nname: grilling\ndisable-model-invocation: false\n---\nAsk the human.\n", *, name="grilling", project=False, command=False):
        root = self.project / ".claude" if project else self.personal
        path = root / "commands" / (name + ".md") if command else root / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def inspect(self, name="grilling"):
        return interview.inspect_interview_skill(self.project, name)


class InterviewInspectionTests(InterviewFixture):
    def test_false_flag_allows_static_check_but_never_claims_runtime_verification(self):
        path = self.skill()
        before = path.read_bytes()
        with patch("subprocess.Popen") as processes:
            result = self.inspect()
        self.assertEqual(result["status"], "available")
        self.assertTrue(result["static_ready"])
        self.assertTrue(result["model_invocable"])
        self.assertFalse(result["runtime_verified"])
        self.assertEqual(result["source"], str(path))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(result["checked_locations"]), 4)
        self.assertFalse((self.project / ".devflow").exists())
        processes.assert_not_called()

    def test_all_documented_true_boolean_spellings_block_model_invocation(self):
        for value in ("true", "TRUE", "Yes", "on", "1", "true # comment"):
            with self.subTest(value=value):
                self.skill("---\ndisable-model-invocation: " + value + "\n---\nInterview.")
                result = self.inspect()
                self.assertEqual(result["status"], "blocked")
                self.assertFalse(result["static_ready"])
                self.assertFalse(result["model_invocable"])

    def test_false_boolean_spellings_and_user_hidden_skill_remain_model_invocable(self):
        for value in ("false", "FALSE", "No", "off", "0"):
            with self.subTest(value=value):
                self.skill("---\ndisable-model-invocation: " + value + "\nuser-invocable: false\n---\nInterview.")
                self.assertTrue(self.inspect()["static_ready"])

    def test_project_skill_and_missing_flag_are_supported(self):
        path = self.skill("---\nname: display-alias\ndescription: Ask the human\n---\nInterview.", project=True)
        result = self.inspect()
        self.assertTrue(result["static_ready"])
        self.assertEqual(result["source"], str(path))  # Directory name remains an invocation alias.

    def test_body_only_skill_does_not_treat_body_text_as_frontmatter(self):
        self.skill("# Interview\n---\ndisable-model-invocation: true\n---\n")
        self.assertTrue(self.inspect()["static_ready"])

    def test_missing_is_limited_to_checked_paths_without_fallback(self):
        self.skill(name="grill-me")
        result = self.inspect()
        self.assertEqual(result["status"], "missing")
        self.assertFalse(result["static_ready"])
        self.assertIsNone(result["model_invocable"])
        self.assertIn("Other runtime sources", result["reason"])

    def test_namespaced_skill_is_unverified_without_filesystem_probe(self):
        with patch.object(interview, "_inspect_candidate") as probe:
            result = self.inspect("interviews:grilling")
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["checked_locations"], [])
        self.assertFalse(result["static_ready"])
        probe.assert_not_called()

    def test_multiple_local_definitions_are_not_silently_selected(self):
        self.skill()
        self.skill(project=True)
        result = self.inspect()
        self.assertEqual(result["status"], "ambiguous")
        self.assertFalse(result["static_ready"])
        self.assertEqual(len(result["candidates"]), 2)
        self.assertIsNone(result["source"])

    def test_command_file_is_supported_and_collision_is_ambiguous(self):
        self.skill(command=True)
        self.assertTrue(self.inspect()["static_ready"])
        self.skill()
        self.assertEqual(self.inspect()["status"], "ambiguous")

    def test_main_conversation_interview_cannot_fork(self):
        self.skill("---\ncontext: fork\n---\nInterview.")
        result = self.inspect()
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["static_ready"])

    def test_malformed_and_unsupported_metadata_never_pass(self):
        examples = [
            "---\ndisable-model-invocation: true\n",  # Unterminated.
            "---\ndisable-model-invocation: false\ndisable-model-invocation: true\n---\n",
            "---\ndisable-model-invocation: perhaps\n---\n",
            "---\n<<: *defaults\n---\n",
            "---\nmetadata:\n  disable-model-invocation: false\n---\n",
            "---\nname: [invalid\n---\n",
            '---\ndisable-model-invocation: "false"\n---\n',
            "---\ndisable-model-invocation: 'on' # comment\n---\n",
            "\ufeff---\ndisable-model-invocation: true\n---\n",
        ]
        for content in examples:
            with self.subTest(content=content):
                self.skill(content)
                result = self.inspect()
                self.assertIn(result["status"], {"invalid", "unverified"})
                self.assertFalse(result["static_ready"])

    def test_bounded_frontmatter_does_not_guess_a_flag_after_the_limit(self):
        self.skill("---\ndescription: " + "x" * interview.MAX_FRONTMATTER_BYTES + "\ndisable-model-invocation: true\n---\n")
        self.assertEqual(self.inspect()["status"], "unverified")
        self.assertFalse(self.inspect()["static_ready"])

    def test_multiline_description_does_not_override_top_level_invocation_flag(self):
        self.skill("---\ndescription: >\n  disable-model-invocation: false\n  Interview.\ndisable-model-invocation: true\n---\n")
        self.assertEqual(self.inspect()["status"], "blocked")

    def test_path_traversal_and_reserved_names_are_not_probed(self):
        for name in ("..", ".", "../grilling", "/grilling", "synced", "anthropic-skills"):
            with self.subTest(name=name), patch.object(interview, "_inspect_candidate") as probe:
                self.assertEqual(self.inspect(name)["status"], "invalid")
                probe.assert_not_called()

    def test_personal_home_default_and_config_dir_override(self):
        self.skill()
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": ""}), patch.object(Path, "home", return_value=self.base):
            result = self.inspect()
        self.assertEqual(result["status"], "missing")
        self.assertIn(str(self.base / ".claude" / "skills" / "grilling" / "SKILL.md"), result["checked_locations"])
        self.assertTrue(self.inspect()["static_ready"])

    def test_directory_symlink_uses_exact_skill_file_without_recursive_scan(self):
        target = self.base / "shared" / "grilling"
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text("---\ndisable-model-invocation: false\n---\nInterview.", encoding="utf-8")
        link = self.personal / "skills" / "grilling"
        link.parent.mkdir(parents=True)
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            self.skipTest("Directory symlinks unavailable: " + str(exc))
        self.assertTrue(self.inspect()["static_ready"])

    def test_differently_named_file_symlink_is_not_read(self):
        target = self.base / "unrelated-private-file.json"
        target.write_text("not skill data", encoding="utf-8")
        link = self.personal / "skills" / "grilling" / "SKILL.md"
        link.parent.mkdir(parents=True)
        try:
            link.symlink_to(target)
        except OSError as exc:
            self.skipTest("File symlinks unavailable: " + str(exc))
        with patch.object(interview.os, "open") as opening:
            result = self.inspect()
        self.assertEqual(result["status"], "unverified")
        opening.assert_not_called()

    def test_missing_skill_makes_component_doctor_not_ready(self):
        with patch("planflow_core.config.shutil.which", return_value="/fixture/tool"):
            report = diagnose(self.project, PLUGIN)
        self.assertEqual(report["configuration_source"], "project_settings")
        self.assertFalse(report["ready"])
        self.assertEqual(report["interview_skill"]["status"], "missing")


class FrozenDoctorTests(InterviewFixture):
    def invoke(self, *args):
        output = io.StringIO()
        with redirect_stdout(output), patch("planflow_core.config.shutil.which", return_value="/fixture/tool"):
            code = cli.main(["--project", str(self.project), *args])
        return code, json.loads(output.getvalue())

    def start_frozen_wrapper(self):
        self.skill("---\ndisable-model-invocation: true\n---\nInvoke the interviewing skill.", name="grill-me")
        self.skill()
        settings = self.project / "devflow.config.json"
        atomic_json(settings, {"schema_version": 1, "plan": {"grill_skill": "grill-me"}})
        engine = Engine(self.project, PLUGIN)
        transcript = self.base / "transcript.jsonl"
        transcript.write_text("", encoding="utf-8")
        state = engine.start("interview-session", "/devflow:plan Human request", str(transcript))
        atomic_json(settings, {"schema_version": 1, "plan": {"grill_skill": "grilling"}})
        return engine, state["run_id"]

    def test_session_doctor_inspects_frozen_skill_not_changed_project_settings(self):
        engine, run_id = self.start_frozen_wrapper()
        state_before = read_json(engine._run(run_id) / "state.json")
        code, report = self.invoke("--session", "interview-session", "doctor")
        self.assertEqual(code, 0)
        self.assertEqual(report["run_id"], run_id)
        self.assertEqual(report["configuration_source"], "frozen_run")
        self.assertEqual(report["interview_skill"]["configured_name"], "grill-me")
        self.assertEqual(report["interview_skill"]["status"], "blocked")
        self.assertFalse(report["ready"])
        self.assertEqual(read_json(engine._run(run_id) / "state.json"), state_before)
        code, report = self.invoke("doctor")
        self.assertEqual(code, 0)
        self.assertEqual(report["configuration_source"], "project_settings")
        self.assertEqual(report["interview_skill"]["configured_name"], "grilling")
        self.assertTrue(report["ready"])

    def test_selected_run_doctor_checks_owner(self):
        _, run_id = self.start_frozen_wrapper()
        code, report = self.invoke("--session", "other-session", "--run", run_id, "doctor")
        self.assertEqual(code, 2)
        self.assertIn("no longer owns", report["message"])

    def test_released_session_uses_project_settings_unless_run_explicitly_selected(self):
        engine, run_id = self.start_frozen_wrapper()
        engine.release_session("interview-session")
        code, report = self.invoke("--session", "interview-session", "doctor")
        self.assertEqual(code, 0)
        self.assertEqual(report["configuration_source"], "project_settings")
        self.assertIsNone(report["run_id"])
        code, report = self.invoke("--session", "interview-session", "--run", run_id, "doctor")
        self.assertEqual(code, 0)
        self.assertEqual(report["configuration_source"], "frozen_run")

    def test_doctor_does_not_repair_pending_transaction(self):
        engine, run_id = self.start_frozen_wrapper()
        transaction = engine._run(run_id) / "transaction.json"
        transaction.write_text('{"fixture":"must remain untouched"}', encoding="utf-8")
        before = transaction.read_bytes()
        code, report = self.invoke("--session", "interview-session", "doctor")
        self.assertEqual(code, 2)
        self.assertIn("read-only", report["message"])
        self.assertEqual(transaction.read_bytes(), before)

    def test_doctor_refuses_changed_frozen_config(self):
        engine, run_id = self.start_frozen_wrapper()
        path = engine._run(run_id) / "config.json"
        value = read_json(path)
        value["grill_skill"] = "grilling"
        atomic_json(path, value)
        code, report = self.invoke("--run", run_id, "doctor")
        self.assertEqual(code, 2)
        self.assertIn("configuration changed", report["message"])


if __name__ == "__main__":
    unittest.main()
