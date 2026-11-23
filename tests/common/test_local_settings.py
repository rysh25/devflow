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


def _reviewer(workflow="plan", **changes):
    base = {"id": "main", "kind": "codex", "command": ["codex", "review"],
            "model": "gpt-test", "effort": "high", "timeout_seconds": 600}
    base.update(changes)
    return base


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

    def test_host_layers_apply_only_to_the_launch_host(self):
        for host in HOSTS:
            with self.subTest(host=host):
                other = next(name for name in HOSTS if name != host)
                atomic_json(self.shared, {"max_reviews": 3, "plan": {"max_reviews": 4,
                            "publish": {"directory": "plans", "filename": "shared-{run_id}"}},
                            "implement": {"max_reviews": 5},
                            "hosts": {host: {"max_reviews": 6, "plan": {"max_reviews": 7,
                                             "publish": {"filename": f"{host}-{{run_id}}"}},
                                             "implement": {"snapshot": {"exclude": [f"{host}/"]},
                                                           "checks": [{"id": "shared-check", "argv": ["true"]}]}},
                                      other: {"max_reviews": 90, "plan": {"max_reviews": 91},
                                              "implement": {"snapshot": {"exclude": ["other/"]}}}}})
                # Shared document alone: the host flow layer beats the host common layer.
                self.assertEqual(self.config("plan", host)["max_reviews"], 7)
                self.assertEqual(self.config("implement", host)["max_reviews"], 6)
                atomic_json(self.local, {"max_reviews": 8, "plan": {"max_reviews": 9},
                            "implement": {"max_reviews": 10},
                            "hosts": {host: {"max_reviews": 11, "plan": {"max_reviews": 13},
                                             "implement": {"max_reviews": 12,
                                                           "checks": [{"id": "local-check", "argv": ["true"]}]}},
                                      other: {"implement": {"max_reviews": 92}}}})
                plan = self.config("plan", host)
                implement = self.config("implement", host)
                # host common → host flow wins inside each document, local beats shared.
                self.assertEqual(plan["max_reviews"], 13)
                self.assertEqual(plan["publish"]["filename"], host + "-{run_id}")
                self.assertEqual(plan["publish"]["directory"], "plans")
                self.assertEqual(implement["max_reviews"], 12)
                self.assertEqual(implement["snapshot"]["exclude"], [f"{host}/"])
                # A checks array replaces wholesale; the shared one does not survive.
                self.assertEqual([check["id"] for check in implement["checks"]], ["local-check"])
                # The other host's values never apply; the launch host's blocks do.
                self.assertEqual(self.config("plan", other)["max_reviews"], 9)
                self.assertEqual(self.config("implement", other)["max_reviews"], 92)
                self.shared.unlink()
                self.local.unlink()

    def test_selected_host_common_beats_the_root_flow_layer(self):
        # The host common layer sits above the root flow layer inside each document;
        # with no host flow or later value, the host common value decides.
        for path in (self.shared, self.local):
            with self.subTest(path=path.name):
                atomic_json(path, {"max_reviews": 3, "plan": {"max_reviews": 4},
                                   "hosts": {"codex": {"max_reviews": 5}}})
                self.assertEqual(self.config("plan", "codex")["max_reviews"], 5)
                self.assertEqual(self.config("implement", "codex")["max_reviews"], 5)
                # A host without a block keeps the root flow value.
                self.assertEqual(self.config("plan", "devin")["max_reviews"], 4)
                path.unlink()

    def test_empty_and_unselected_host_blocks_mean_no_override(self):
        atomic_json(self.shared, {"hosts": {"codex": {}, "claude": {"plan": {}}}})
        for host in HOSTS:
            self.assertEqual(self.config("plan", host)["max_reviews"], 6)
        # An unknown host name is a structural error even for a host it never selects.
        atomic_json(self.shared, {"hosts": {"bogus": {}}})
        with self.assertRaisesRegex(DevFlowError, "unknown launch host"):
            self.config("plan", "devin")

    def test_unselected_host_and_flow_value_errors_do_not_apply(self):
        # A bad value in a host that did not launch DevFlow, or in a different flow,
        # never reaches the selected configuration (unlike structural errors, which
        # always fail because they are properties of the file itself).
        atomic_json(self.shared, {"plan": {"max_reviews": 0},
                                  "hosts": {"claude": {"max_reviews": 0,
                                                       "implement": {"max_reviews": 0}}}})
        self.assertEqual(self.config("implement", "codex")["max_reviews"], 6)
        self.assertEqual(self.config("implement", "devin")["max_reviews"], 6)
        with self.assertRaisesRegex(DevFlowError, "devflow.config.json: hosts.claude"):
            self.config("implement", "claude")
        with self.assertRaisesRegex(DevFlowError, "devflow.config.json: plan"):
            self.config("plan", "codex")

    def test_old_host_specific_names_are_never_read_and_are_not_root_markers(self):
        (self.project / ".git").mkdir()
        nested = self.project / "nested"
        nested.mkdir()
        # Broken or hostile files with the former dedicated names are not even opened.
        for name in ("devflow.config.claude.json", "devflow.config.claude.local.json",
                     "devflow.config.codex.json", "devflow.config.codex.local.json",
                     "devflow.config.devin.json", "devflow.config.devin.local.json"):
            (nested / name).write_text("not JSON", encoding="utf-8")
            (self.project / name).write_text("not JSON", encoding="utf-8")
        self.assertEqual(project_root(nested), self.project)   # found via .git only
        for host in HOSTS:
            self.assertEqual(self.config("plan", host)["max_reviews"], 6)

    def test_bad_values_name_the_file_and_the_layer(self):
        for path in (self.shared, self.local):
            for bad, error in (("{", path.name),
                               ('{"schema_version": true}', path.name),
                               ('{"unknown": {}}', f"Unknown {path.name} keys"),
                               ('{"hosts": []}', f"{path.name}: hosts"),
                               ('{"hosts": null}', f"{path.name}: hosts"),
                               ('{"hosts": {"bogus": {}}}', "unknown launch host"),
                               ('{"hosts": {"codex": []}}', f"{path.name}: hosts.codex"),
                               ('{"hosts": {"codex": {"schema_version": 1}}}',
                                "schema_version is not allowed under hosts"),
                               ('{"hosts": {"codex": {"bogus": 1}}}', "unknown keys: bogus"),
                               ('{"hosts": {"codex": {"plan": []}}}', "hosts.codex.plan"),
                               ('{"hosts": {"codex": {"plan": null}}}', "hosts.codex.plan"),
                               ('{"hosts": {"codex": {"plan": {"schema_version": 1}}}}',
                                "schema_version is not allowed under hosts"),
                               ('{"hosts": {"claude": {}, "claude": {}}}', "Duplicate JSON key"),
                               ('{"hosts": {"codex": {"plan": {}, "plan": {}}}}',
                                "Duplicate JSON key"),
                               ('{"plan": []}', f"{path.name}: plan"),
                               (json.dumps({"plan": {"grill_skill": "\ud800"}}), "UTF-8"),
                               (json.dumps({"hosts": {"codex": {"plan": {"grill_skill": "\ud800"}}}}),
                                "hosts.codex.plan"),
                               (json.dumps({"\ud800": 1}), "UTF-8"),
                               (json.dumps({"reviewers": [{**self.config("plan")["reviewers"][0],
                                                           "kind": []}]}), "reviewer"),
                               ('{"implement": {"snapshot": {"mode": {}}}}', "snapshot.mode")):
                with self.subTest(path=path.name, bad=bad):
                    path.write_text(bad, encoding="utf-8")
                    with self.assertRaisesRegex(DevFlowError, error):
                        self.config("implement", "codex")
                    path.unlink()

    def test_selected_host_layer_errors_name_their_position(self):
        atomic_json(self.shared, {"hosts": {"codex": {"implement": {"max_reviews": 0}}}})
        with self.assertRaisesRegex(DevFlowError, "devflow.config.json: hosts.codex.implement"):
            self.config("implement", "codex")
        self.shared.unlink()
        atomic_json(self.local, {"hosts": {"codex": {"max_reviews": 0}}})
        with self.assertRaisesRegex(DevFlowError, "devflow.config.local.json: hosts.codex"):
            self.config("implement", "codex")

    def test_reviewer_arrays_are_checked_when_written_and_against_the_effective_limit(self):
        one = [_reviewer()]
        two = [*_reviewer_batch(2)]
        hundred = [*_reviewer_batch(100)]
        # A replaced array's own errors cannot be hidden by the replacement.
        atomic_json(self.shared, {"hosts": {"devin": {"reviewers": [{"id": "bad"}]}}})
        atomic_json(self.local, {"reviewers": one})
        with self.assertRaisesRegex(DevFlowError, "devflow.config.json: hosts.devin"):
            self.config("plan", "devin")
        # A 100-entry array fails on its own even when a later layer replaces it.
        atomic_json(self.shared, {"reviewers": hundred})
        with self.assertRaisesRegex(DevFlowError, "devflow.config.json"):
            self.config("plan", "devin")
        # An inherited limit is matched against the array a later stage writes.
        atomic_json(self.shared, {"max_reviews": 1, "reviewers": one,
                                  "hosts": {"devin": {"reviewers": two}}})
        with self.assertRaisesRegex(DevFlowError, "devflow.config.json: Configure"):
            self.config("plan", "devin")
        # Limit and array may be written at different stages of one document.
        atomic_json(self.shared, {"reviewers": two, "hosts": {"devin": {"max_reviews": 1}}})
        with self.assertRaisesRegex(DevFlowError, "devflow.config.json: Configure"):
            self.config("plan", "devin")
        # The effective values of the local document are matched once, at its end.
        atomic_json(self.shared, {"max_reviews": 1, "reviewers": one})
        atomic_json(self.local, {"max_reviews": 2, "reviewers": two})
        self.assertEqual(self.config("plan", "devin")["max_reviews"], 2)
        # A later document's limit can legitimately constrain a host layer's array.
        atomic_json(self.local, {"plan": {"max_reviews": 1}})
        atomic_json(self.shared, {"hosts": {"codex": {"reviewers": one}}})
        config = self.config("plan", "codex")
        self.assertEqual(len(config["reviewers"]), 1)
        self.assertEqual(config["max_reviews"], 1)
        # A pair over budget at an early stage is valid once a later stage of the
        # same document replaces the array: count-vs-limit runs on the effective value.
        atomic_json(self.local, {})
        atomic_json(self.shared, {"max_reviews": 1, "reviewers": two,
                                  "hosts": {"codex": {"reviewers": one}}})
        config = self.config("plan", "codex")
        self.assertEqual(len(config["reviewers"]), 1)
        self.assertEqual(config["max_reviews"], 1)
        atomic_json(self.shared, {})
        atomic_json(self.local, {"max_reviews": 1, "reviewers": two})
        with self.assertRaisesRegex(DevFlowError, "devflow.config.local.json: Configure"):
            self.config("plan", "devin")

    def test_host_snapshot_patterns_are_validated_before_a_local_overlay_but_root_stays_compatible(self):
        valid = {"include": ["**"], "exclude": [], "context_paths": []}
        atomic_json(self.local, {"implement": {"snapshot": valid}})
        (self.project / "source.py").write_text("value = 1\n", encoding="utf-8")
        for field in valid:
            with self.subTest(field=field):
                bad = {"implement": {"snapshot": {"mode": "files", field: ["../outside"]}}}
                atomic_json(self.shared, {"hosts": {"codex": bad}})
                with self.assertRaisesRegex(DevFlowError, "devflow.config.json: hosts.codex.implement"):
                    self.config("implement", "codex")
                # The same value at the root stays compatible when a later array replaces it.
                atomic_json(self.shared, bad)
                result = capture(self.project, self.config("implement", "codex"))
                self.assertIn("source.py", result["files"])

    def test_root_flow_schema_version_one_stays_compatible(self):
        atomic_json(self.shared, {"plan": {"schema_version": 1, "max_reviews": 3},
                                  "implement": {"schema_version": 1}})
        self.assertEqual(self.config("plan")["max_reviews"], 3)
        self.assertEqual(self.config("implement")["max_reviews"], 6)

    def test_directory_reparse_and_read_errors_name_the_file(self):
        for path in (self.shared, self.local):
            with self.subTest(path=path.name):
                path.mkdir()
                with self.assertRaisesRegex(DevFlowError, f"{path.name}:.*regular file"):
                    self.config("plan", "codex")
                path.rmdir()
                atomic_json(path, {})
                real_lstat = Path.lstat
                reparse = SimpleNamespace(st_mode=stat.S_IFREG,
                                          st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
                with patch.object(Path, "lstat",
                                  lambda p: reparse if p == path else real_lstat(p)):
                    with self.assertRaisesRegex(DevFlowError, f"{path.name}:.*reparse"):
                        self.config("plan", "codex")
                with patch.object(Path, "read_bytes", side_effect=PermissionError("fixture unreadable")):
                    with self.assertRaisesRegex(DevFlowError, f"{path.name}:.*fixture unreadable"):
                        self.config("plan", "codex")
                path.unlink()

    def test_symlink_and_dangling_symlink_are_rejected(self):
        for path in (self.shared, self.local):
            with self.subTest(path=path.name):
                target = self.project / "other.json"
                atomic_json(target, {})
                try:
                    path.symlink_to(target)
                except (OSError, NotImplementedError) as exc:
                    self.skipTest(f"File symlink support required: {exc}")
                with self.assertRaisesRegex(DevFlowError, path.name):
                    self.config("plan", "codex")
                target.unlink()
                with self.assertRaisesRegex(DevFlowError, path.name):
                    self.config("plan", "codex")
                path.unlink()

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_windows_junction_is_rejected(self):
        target = self.project / "target"
        target.mkdir()
        for name in ("devflow.config.json", "devflow.config.local.json"):
            with self.subTest(name=name):
                path = self.project / name
                subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(path), str(target)],
                               check=True, capture_output=True, timeout=30)
                with patch("devflow_core.common.settings.read_json") as reading:
                    with self.assertRaisesRegex(DevFlowError, f"{path.name}:.*reparse"):
                        self.config("plan", "codex")
                reading.assert_not_called()
                path.rmdir()


def _reviewer_batch(count):
    return [{"id": f"r{i:03}", "kind": "codex", "command": ["codex", "review"],
             "model": "gpt-test", "effort": "high", "timeout_seconds": 600}
            for i in range(count)]
