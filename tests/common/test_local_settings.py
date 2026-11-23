"""Layered settings, project discovery and private-file boundaries."""
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from devflow_core.common.errors import DevFlowError
from devflow_core.common.hosts import HOSTS
from devflow_core.common.paths import project_root
from devflow_core.common.settings import read_project_settings
from devflow_core.common.storage import atomic_json
from devflow_core.implement.snapshot import capture
from devflow_core.workflows import WORKFLOWS


class LocalSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name).resolve()
        self.shared = self.project / "devflow.config.json"
        self.local = self.project / "devflow.config.local.json"

    def config(self, workflow, host="claude"):
        return WORKFLOWS[workflow].load_config(read_project_settings(self.project, host, WORKFLOWS))

    def test_precedence_arrays_and_member_merges(self):
        reviewers = self.config("plan")["reviewers"]
        reviewers[0]["model"] = "local-model"
        atomic_json(self.shared, {"max_reviews": 3, "plan": {"max_reviews": 4,
                    "publish": {"directory": "plans", "filename": "shared-{run_id}"}},
                    "implement": {"snapshot": {"exclude": ["private/"]}}})
        atomic_json(self.local, {"max_reviews": 5, "reviewers": reviewers,
                    "plan": {"max_reviews": 6, "publish": {"filename": "local-{run_id}"}},
                    "implement": {"snapshot": {"include": ["src/**"]}}})
        plan = self.config("plan")
        implement = self.config("implement")
        self.assertEqual((plan["max_reviews"], implement["max_reviews"]), (6, 5))
        self.assertEqual(plan["reviewers"], reviewers)
        self.assertEqual(plan["publish"], {"enabled": True, "directory": "plans", "filename": "local-{run_id}"})
        self.assertEqual(implement["snapshot"]["exclude"], ["private/"])
        self.assertEqual(implement["snapshot"]["include"], ["src/**"])
        self.assertEqual(len(plan["reviewers"]), 2)

    def test_invalid_file_cannot_be_hidden_by_the_other_file(self):
        for filename, bad in ((self.shared, {"plan": {"max_reviews": True}}),
                              (self.local, {"plan": {"publish": {"filename": "../bad"}}})):
            with self.subTest(filename=filename.name):
                atomic_json(self.shared, {"plan": {"max_reviews": 6}})
                atomic_json(self.local, {"plan": {"max_reviews": 4}})
                atomic_json(filename, bad)
                with self.assertRaisesRegex(DevFlowError, filename.name):
                    self.config("plan")

    def test_nested_local_file_marks_project_without_creating_files(self):
        (self.project / ".git").mkdir()
        nested = self.project / "nested"
        nested.mkdir()
        atomic_json(nested / "devflow.config.local.json", {})
        child = nested / "src"
        child.mkdir()
        self.assertEqual(project_root(child), nested)
        before = sorted(p.relative_to(nested) for p in nested.rglob("*"))
        WORKFLOWS["implement"].load_config(read_project_settings(nested, "claude", WORKFLOWS))
        self.assertEqual(before, sorted(p.relative_to(nested) for p in nested.rglob("*")))

    def test_explicit_local_configuration_context_is_validated_by_snapshot_capture(self):
        atomic_json(self.shared, {"implement": {"snapshot": {
            "context_paths": ["nested/DEVFLOW.CONFIG.LOCAL.JSON"]}}})
        config = self.config("implement")
        self.assertEqual(config["snapshot"]["context_paths"], ["nested/DEVFLOW.CONFIG.LOCAL.JSON"])

    def test_each_layer_selects_its_candidate_independently_for_each_host(self):
        for host in HOSTS:
            shared = self.project / f"devflow.config.{host}.json"
            local = self.project / f"devflow.config.{host}.local.json"
            with self.subTest(host=host):
                self.assertEqual(self.config("plan", host)["max_reviews"], 6)
                atomic_json(self.shared, {"plan": {"max_reviews": 12, "publish": {"directory": "common"}},
                                          "implement": {"max_reviews": 13}})
                atomic_json(self.local, {"implement": {"max_reviews": 14}})
                self.assertEqual(self.config("plan", host)["max_reviews"], 12)
                self.assertEqual(self.config("implement", host)["max_reviews"], 14)

                atomic_json(shared, {"max_reviews": 15})
                self.assertEqual(self.config("plan", host)["max_reviews"], 15)
                self.assertEqual(self.config("plan", host)["publish"]["directory"], "docs/plans")
                self.assertEqual(self.config("implement", host)["max_reviews"], 14)

                atomic_json(local, {"max_reviews": 16, "implement": {"max_reviews": 17}})
                self.assertEqual(self.config("plan", host)["max_reviews"], 16)
                self.assertEqual(self.config("implement", host)["max_reviews"], 17)
                shared.unlink()
                self.assertEqual(self.config("plan", host)["publish"]["directory"], "common")
                self.assertEqual(self.config("implement", host)["max_reviews"], 17)
                for path in (self.shared, self.local, local):
                    path.unlink()

    def test_ignored_candidates_are_not_read_and_dedicated_name_is_not_a_root_marker(self):
        (self.project / ".git").mkdir()
        nested = self.project / "nested"
        nested.mkdir()
        for path in (self.shared, self.local, nested / "devflow.config.codex.json",
                     self.project / "devflow.config.devin.json"):
            path.write_text("not JSON", encoding="utf-8")
        atomic_json(self.project / "devflow.config.codex.json", {"max_reviews": 11})
        atomic_json(self.project / "devflow.config.codex.local.json", {})
        self.assertEqual(project_root(nested), self.project)
        self.assertEqual(self.config("plan", "codex")["max_reviews"], 11)

    def test_bad_dedicated_values_name_the_file_and_do_not_use_the_fallback(self):
        for fallback, dedicated in ((self.shared, "devflow.config.codex.json"),
                                    (self.local, "devflow.config.codex.local.json")):
            atomic_json(fallback, {"max_reviews": 12})
            candidate = self.project / dedicated
            for bad in ("{", '{"schema_version": true}', '{"unknown": {}}', '{"plan": []}',
                        json.dumps({"plan": {"grill_skill": "\ud800"}}),
                        json.dumps({"\ud800": 1}),
                        json.dumps({"reviewers": [{**self.config("plan")["reviewers"][0], "kind": []}]}),
                        '{"implement": {"snapshot": {"mode": {}}}}'):
                with self.subTest(dedicated=dedicated, bad=bad):
                    candidate.write_text(bad, encoding="utf-8")
                    with self.assertRaisesRegex(DevFlowError, dedicated):
                        self.config("implement", "codex")
            candidate.unlink()

    def test_dedicated_patterns_are_validated_before_local_overlay_but_common_stays_compatible(self):
        dedicated = self.project / "devflow.config.codex.json"
        valid = {"include": ["**"], "exclude": [], "context_paths": []}
        atomic_json(self.local, {"implement": {"snapshot": valid}})
        (self.project / "source.py").write_text("value = 1\n", encoding="utf-8")
        for field in valid:
            with self.subTest(field=field):
                settings = {"implement": {"snapshot": {"mode": "files", field: ["../outside"]}}}
                atomic_json(dedicated, settings)
                with self.assertRaisesRegex(DevFlowError, dedicated.name):
                    self.config("implement", "codex")
                dedicated.unlink()
                atomic_json(self.shared, settings)
                result = capture(self.project, self.config("implement", "codex"))
                self.assertIn("source.py", result["files"])

    def test_dedicated_directory_reparse_and_read_error_do_not_fall_back(self):
        atomic_json(self.shared, {"max_reviews": 12})
        path = self.project / "devflow.config.codex.json"
        path.mkdir()
        with self.assertRaisesRegex(DevFlowError, f"{path.name}:.*regular file"):
            self.config("plan", "codex")
        path.rmdir()
        atomic_json(path, {})
        with patch.object(Path, "lstat", return_value=SimpleNamespace(
                st_mode=stat.S_IFREG, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)):
            with self.assertRaisesRegex(DevFlowError, f"{path.name}:.*reparse"):
                self.config("plan", "codex")
        with patch.object(Path, "read_bytes", side_effect=PermissionError("fixture unreadable")):
            with self.assertRaisesRegex(DevFlowError, f"{path.name}:.*fixture unreadable"):
                self.config("plan", "codex")

    def test_dedicated_symlink_and_dangling_symlink_do_not_fall_back(self):
        atomic_json(self.shared, {"max_reviews": 12})
        target = self.project / "other.json"
        atomic_json(target, {})
        path = self.project / "devflow.config.codex.json"
        try:
            path.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"File symlink support required: {exc}")
        with self.assertRaisesRegex(DevFlowError, path.name):
            self.config("plan", "codex")
        target.unlink()
        with self.assertRaisesRegex(DevFlowError, path.name):
            self.config("plan", "codex")

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_windows_dedicated_junction_does_not_fall_back(self):
        atomic_json(self.shared, {"max_reviews": 12})
        target = self.project / "target"
        target.mkdir()
        path = self.project / "devflow.config.codex.json"
        subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(path), str(target)],
                       check=True, capture_output=True, timeout=30)
        self.addCleanup(path.rmdir)
        with patch("devflow_core.common.settings.read_json") as reading:
            with self.assertRaisesRegex(DevFlowError, f"{path.name}:.*reparse"):
                self.config("plan", "codex")
        reading.assert_not_called()
