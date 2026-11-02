"""Reproducible ZIP, manifest integrity, and one self-contained plugin."""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("devflow_packaging", ROOT / "tools" / "package.py")
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


class DistributionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="devflow distribution ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()

    def clone(self):
        destination = self.base / "repository"
        return Path(shutil.copytree(ROOT, destination, ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", "dist", ".git", ".devflow")))

    def test_static_validate_current_bundle(self):
        result = package.validate(ROOT)
        self.assertEqual(result["marketplace"], "devflow-marketplace")
        self.assertEqual(result["plugins"], {"devflow": "0.2.1"})

    def test_release_versions_are_consistent(self):
        manifest = json.loads((ROOT / "plugins/devflow/.claude-plugin/plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], "0.2.1")
        package = ROOT / "plugins/devflow/scripts/devflow_core"
        defining = []
        for source in sorted(package.rglob("*.py")):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            if any(isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "__version__"
                                                         for target in node.targets) for node in tree.body):
                defining.append(source.relative_to(ROOT).as_posix())
        self.assertEqual(defining, ["plugins/devflow/scripts/devflow_core/__init__.py"], "One version source only")
        for relative in defining:
            with self.subTest(path=relative):
                tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
                versions = [ast.literal_eval(node.value) for node in tree.body
                            if isinstance(node, ast.Assign)
                            and any(isinstance(target, ast.Name) and target.id == "__version__"
                                    for target in node.targets)]
                self.assertEqual(versions, [manifest["version"]])
        for relative in ("README.md", "docs/architecture.md", "docs/validation.md"):
            with self.subTest(document=relative):
                self.assertIn("DevFlow " + manifest["version"], (ROOT / relative).read_text(encoding="utf-8"))

    def test_user_documents_only_describe_current_single_plugin(self):
        paths = [ROOT / "README.md"] + sorted((ROOT / "docs").rglob("*.md")) + sorted((ROOT / "examples").rglob("*.md"))
        old_names = ("plan" + "flow", "build" + "flow")
        obsolete = ("旧版", "移行手順", "grill-me", *old_names)
        for path in paths:
            text = path.read_text(encoding="utf-8").casefold()
            for phrase in obsolete:
                with self.subTest(document=str(path.relative_to(ROOT)), phrase=phrase):
                    self.assertNotIn(phrase, text)
        self.assertFalse(any((ROOT / "docs" / old_names[0]).glob("*.md")))
        self.assertFalse((ROOT / "examples" / (old_names[1] + ".nextjs.json")).exists())

    def test_no_tracked_file_mentions_the_old_component_names(self):
        old_names = ("plan" + "flow", "build" + "flow", "flow" + "_common", "_ven" + "dor", "sync" + "_shared")
        listed = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, timeout=30)
        if listed.returncode:
            self.skipTest("Not a Git checkout")
        for name in filter(None, listed.stdout.decode("utf-8").split("\0")):
            path = ROOT / name
            if not path.is_file():
                continue
            text = (name + "\n" + path.read_text(encoding="utf-8", errors="replace")).casefold()
            for old in old_names:
                with self.subTest(file=name, name=old):
                    self.assertNotIn(old, text)
        minimal = json.loads((ROOT / "examples/devflow.minimal.json").read_text(encoding="utf-8"))
        self.assertEqual(minimal["plan"]["grill_skill"], "grilling")

    def test_zip_deterministic_and_every_entry_hashes(self):
        first = package.build(ROOT, self.base / "first.zip")
        second = package.build(ROOT, self.base / "second.zip")
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with zipfile.ZipFile(first) as archive:
            names = archive.namelist()
            self.assertEqual(names, sorted(names))
            self.assertEqual(len(names), len(set(names)))
            self.assertIn("devflow/.claude-plugin/marketplace.json", names)
            self.assertIn("devflow/plugins/devflow/skills/plan/SKILL.md", names)
            self.assertIn("devflow/plugins/devflow/skills/implement/SKILL.md", names)
            self.assertTrue(all(name.startswith("devflow/plugins/devflow/") for name in names if name.startswith("devflow/plugins/")))
            for info in archive.infolist():
                self.assertEqual(info.date_time, (1980, 1, 1, 0, 0, 0))
                self.assertTrue(info.filename.startswith("devflow/"))
            sums = archive.read("devflow/SHA256SUMS").decode("utf-8").splitlines()
            self.assertEqual(len(sums), len(names) - 1)
            for line in sums:
                expected, relative = line.split("  ", 1)
                self.assertEqual(hashlib.sha256(archive.read("devflow/" + relative)).hexdigest(), expected)
            self.assertFalse(any("/__pycache__/" in name or "/dist/" in name or name.endswith(".pyc") for name in names))

    def test_existing_output_is_preserved(self):
        target = self.base / "existing.zip"
        target.write_bytes(b"user-owned-existing-output")
        with self.assertRaises(package.PackagingError):
            package.build(ROOT, target)
        self.assertEqual(target.read_bytes(), b"user-owned-existing-output")

    def test_workflow_cannot_import_another_workflow_or_coordinator(self):
        for target, line, message in (
            ("plan/engine.py", "from ..implement import snapshot\n", "imports another workflow"),
            ("implement/engine.py", "from .. import router\n", "imports a coordinator"),
            ("common/storage.py", "from ..plan import engine\n", "Shared code depends"),
        ):
            with self.subTest(target=target):
                cloned = self.clone()
                source = cloned / "plugins" / "devflow" / "scripts" / "devflow_core" / target
                source.write_text(source.read_text(encoding="utf-8") + line, encoding="utf-8")
                with self.assertRaisesRegex(package.PackagingError, message):
                    package.validate(cloned)
                shutil.rmtree(cloned)

    def test_registry_matches_workflow_packages_and_a_new_workflow_is_validated(self):
        cloned = self.clone()
        package_dir = cloned / "plugins" / "devflow" / "scripts" / "devflow_core"
        dummy = package_dir / "dummy"
        dummy.mkdir()
        (dummy / "__init__.py").write_text("WORKFLOW = None\n", encoding="utf-8")
        for name in ("defaults.json", "reviewer.md", "review.schema.json"):
            (dummy / name).write_text("{}\n" if name.endswith(".json") else "Review.\n", encoding="utf-8")
        with self.assertRaisesRegex(package.PackagingError, "registry"):
            package.validate(cloned)
        registry = package_dir / "workflows.py"
        registry.write_text(registry.read_text(encoding="utf-8") + "from .dummy import WORKFLOW as DUMMY\n", encoding="utf-8")
        package.validate(cloned)
        (package_dir / "router.py").write_text((package_dir / "router.py").read_text(encoding="utf-8")
                                               + "from .plan import WORKFLOW as DIRECT\n", encoding="utf-8")
        with self.assertRaisesRegex(package.PackagingError, "Only the workflow registry"):
            package.validate(cloned)

    def test_every_workflow_ships_its_resources(self):
        cloned = self.clone()
        (cloned / "plugins" / "devflow" / "scripts" / "devflow_core" / "implement" / "reviewer.md").unlink()
        with self.assertRaisesRegex(package.PackagingError, "Missing implement/reviewer.md"):
            package.validate(cloned)

    def test_potential_secret_file_is_never_packaged(self):
        cloned = self.clone()
        (cloned / ".env").write_text("SYNTHETIC_TEST_DATA_ONLY=1\n", encoding="utf-8")
        with self.assertRaisesRegex(package.PackagingError, "Potential secret"):
            package.build(cloned, self.base / "must-not-exist.zip")
        self.assertFalse((self.base / "must-not-exist.zip").exists())

    def test_symlink_input_is_rejected(self):
        cloned = self.clone()
        try:
            (cloned / "alias.py").symlink_to(cloned / "tools" / "package.py")
        except (OSError, NotImplementedError):
            self.skipTest("Creating symlinks requires OS permission")
        with self.assertRaisesRegex(package.PackagingError, "symlink/reparse"):
            package.validate(cloned)

    def test_extracted_plugin_runs_without_repository_shared(self):
        archive_path = package.build(ROOT, self.base / "release.zip")
        extracted = self.base / "extracted"
        with zipfile.ZipFile(archive_path) as archive:
            # All paths were generated and checked in this test, not untrusted ZIP input.
            archive.extractall(extracted)
        release = extracted / "devflow"
        self.assertEqual(package.validate(release)["version"], "0.2.1")
        independent = self.base / "devflow-only"
        shutil.copytree(release / "plugins" / "devflow", independent)
        self.assertFalse((independent / "shared").exists())
        result = subprocess.run([sys.executable, "-B", str(independent / "scripts" / "devflow.py"), "--version"],
            cwd=self.base, capture_output=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("0.2.1", result.stdout)
        for flow in ("plan", "implement"):
            with self.subTest(flow=flow):
                result = subprocess.run([sys.executable, "-B", str(independent / "scripts" / "devflow.py"), flow, "--help"],
                    cwd=self.base, capture_output=True, encoding="utf-8", timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
        project = self.base / "extracted project"
        project.mkdir()
        for command in ("status", "doctor"):
            with self.subTest(command=command):
                result = subprocess.run([sys.executable, "-B", str(independent / "scripts" / "devflow.py"),
                                         "--project", str(project), command],
                    cwd=self.base, capture_output=True, encoding="utf-8", timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout)["plugin"], "devflow")
        self.assertFalse((project / ".devflow").exists())
        # Every workflow's resources are read from the extracted package itself.
        code = ("import sys; sys.path.insert(0, sys.argv[1]); from devflow_core.workflows import WORKFLOWS; "
                "[w.resource(n).read_bytes() for w in WORKFLOWS.values() "
                "for n in ('defaults.json', 'reviewer.md', 'review.schema.json')]; "
                "print(sorted(str(w.resource_dir) for w in WORKFLOWS.values()))")
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(independent / "scripts")],
                                cwd=self.base, capture_output=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(independent), result.stdout)

    def test_runtime_history_is_excluded_even_when_present(self):
        cloned = self.clone()
        private = cloned / ".devflow" / "runs" / "plan" / "private"
        private.mkdir(parents=True)
        (private / "interview.md").write_text("DO NOT DISTRIBUTE PRIVATE INTERVIEW", encoding="utf-8")
        package_path = package.build(cloned, self.base / "without-runtime.zip")
        with zipfile.ZipFile(package_path) as archive:
            self.assertFalse(any("/.devflow/" in name for name in archive.namelist()))


if __name__ == "__main__":
    unittest.main()
