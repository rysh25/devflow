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
            "__pycache__", "*.pyc", "dist", ".git", ".devflow", ".buildflow", ".planflow")))

    def test_static_validate_current_bundle(self):
        result = package.validate(ROOT)
        self.assertEqual(result["marketplace"], "devflow-marketplace")
        self.assertEqual(result["plugins"], {"devflow": "0.1.0"})

    def test_release_versions_are_consistent(self):
        manifest = json.loads((ROOT / "plugins/devflow/.claude-plugin/plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], "0.1.0")
        paths = (
            "plugins/devflow/scripts/devflow_core/__init__.py",
            "plugins/devflow/components/planflow/scripts/planflow_core/__init__.py",
            "plugins/devflow/components/buildflow/scripts/buildflow_core/__init__.py",
        )
        for relative in paths:
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
        obsolete = ("旧版", "移行手順", "0.2.0", "/planflow:", "/buildflow:",
                    ".planflow/", ".buildflow/", "grill-me")
        for path in paths:
            text = path.read_text(encoding="utf-8")
            for phrase in obsolete:
                with self.subTest(document=str(path.relative_to(ROOT)), phrase=phrase):
                    self.assertNotIn(phrase, text)
        self.assertFalse(any((ROOT / "docs/planflow").glob("*.md")))
        self.assertFalse((ROOT / "examples/buildflow.nextjs.json").exists())
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
            self.assertFalse(any(name.startswith(("devflow/plugins/planflow/", "devflow/plugins/buildflow/")) for name in names))
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

    def test_divergent_vendored_copy_is_rejected(self):
        cloned = self.clone()
        source = cloned / "plugins" / "devflow" / "components" / "buildflow" / "scripts" / "buildflow_core" / "_vendor" / "flow_common" / "storage.py"
        source.write_bytes(source.read_bytes() + b"\n# unsynced local edit\n")
        with self.assertRaisesRegex(package.PackagingError, "Generated shared code differs"):
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
        self.assertEqual(package.validate(release)["version"], "0.1.0")
        independent = self.base / "devflow-only"
        shutil.copytree(release / "plugins" / "devflow", independent)
        self.assertFalse((independent / "shared").exists())
        result = subprocess.run([sys.executable, "-B", str(independent / "scripts" / "devflow.py"), "--version"],
            cwd=self.base, capture_output=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("0.1.0", result.stdout)
        for flow in ("plan", "implement"):
            with self.subTest(flow=flow):
                result = subprocess.run([sys.executable, "-B", str(independent / "scripts" / "devflow.py"), flow, "--help"],
                    cwd=self.base, capture_output=True, encoding="utf-8", timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)

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
