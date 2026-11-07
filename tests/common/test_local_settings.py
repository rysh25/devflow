"""Layered settings, project discovery and private-file boundaries."""
from pathlib import Path
import tempfile
import unittest

from devflow_core.common.errors import DevFlowError
from devflow_core.common.paths import project_root
from devflow_core.common.storage import atomic_json
from devflow_core.workflows import WORKFLOWS


class LocalSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name).resolve()
        self.shared = self.project / "devflow.config.json"
        self.local = self.project / "devflow.config.local.json"

    def test_precedence_arrays_and_member_merges(self):
        reviewers = WORKFLOWS["plan"].load_config(self.project)["reviewers"]
        reviewers[0]["model"] = "local-model"
        atomic_json(self.shared, {"max_reviews": 3, "plan": {"max_reviews": 4,
                    "publish": {"directory": "plans", "filename": "shared-{run_id}"}},
                    "implement": {"snapshot": {"exclude": ["private/"]}}})
        atomic_json(self.local, {"max_reviews": 5, "reviewers": reviewers,
                    "plan": {"max_reviews": 6, "publish": {"filename": "local-{run_id}"}},
                    "implement": {"snapshot": {"include": ["src/**"]}}})
        plan = WORKFLOWS["plan"].load_config(self.project)
        implement = WORKFLOWS["implement"].load_config(self.project)
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
                    WORKFLOWS["plan"].load_config(self.project)

    def test_nested_local_file_marks_project_without_creating_files(self):
        (self.project / ".git").mkdir()
        nested = self.project / "nested"
        nested.mkdir()
        atomic_json(nested / "devflow.config.local.json", {})
        child = nested / "src"
        child.mkdir()
        self.assertEqual(project_root(child), nested)
        before = sorted(p.relative_to(nested) for p in nested.rglob("*"))
        WORKFLOWS["implement"].load_config(nested)
        self.assertEqual(before, sorted(p.relative_to(nested) for p in nested.rglob("*")))

    def test_explicit_local_configuration_context_is_validated_by_snapshot_capture(self):
        atomic_json(self.shared, {"implement": {"snapshot": {
            "context_paths": ["nested/DEVFLOW.CONFIG.LOCAL.JSON"]}}})
        config = WORKFLOWS["implement"].load_config(self.project)
        self.assertEqual(config["snapshot"]["context_paths"], ["nested/DEVFLOW.CONFIG.LOCAL.JSON"])
