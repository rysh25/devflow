"""Reproducible ZIP, manifest integrity, and one self-contained plugin."""
import ast
import hashlib
import importlib.util
import json
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

from devflow_core.common.environment import review_timeout_ms

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("devflow_packaging", ROOT / "tools" / "package.py")
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


REMOVED_NAMES = ("guard", "allowlist", "ShellPolicy", "ShellPolicyError", "shell_tokens", "split_words",
                 "powershell_tokens", "check_command", "restricts_tools", "allow_tool", "check_write",
                 "NON_WRITING_TOOLS", "PROTECTED_WRITE_NAMES", "GuardError", "permissionDecision", "SubagentHandback",
                 "SKILL_NAME", "name_key", "CLAUDE_KINDS", "HUMAN_KINDS", "READ_ONLY_COMMANDS", "human_actions",
                 # Reviewer isolation, its settings and the packet's file contents (stage 6).
                 "protocol_version", "OPENCODE_PROTOCOL", "PROTOCOL_ONE_MESSAGE", "with_web_default", "web_enabled",
                 "OPENCODE_WEB_TOOLS", "CODEX_DUPLICATE_ID_ITEMS", "web_search", "runtime_config", "_agent_id",
                 "system_prompt", "SYSTEM_PACKET_ONLY", "SYSTEM_WITH_WEB", "_program", "SHELLS", "mcp_servers",
                 "external_directory", "repository_context", "context_files", "file_manifest", "context_file",
                 "new_session", "kill_group", "extra_env", "project_doc_max_bytes", "OPENCODE_PROJECT_OFF",
                 "OPENCODE_DISABLE_PROJECT_CONFIG", "OPENCODE_CONFIG_PROJECT_DISABLE", "OPENCODE_DISABLE_CLAUDE_CODE",
                 # The read server (removed in stage 0) and the plan's other names of the limits.
                 "READ_SERVER", "OPENCODE_READ_ONLY_TOOLS", "CODEX_RESOURCE_TOOLS", "OTHER_CALLS", "reviewer_read",
                 "reviewer_tools", "READ_TOOLS", "NON_WRITING", "PROTECTED",
                 # Checks of Claude Code's Bash limit, which DevFlow no longer depends on.
                 "bash_limit", "limit_warnings", "timeout_warnings", "bash_needs", "LIMIT_WARNING",
                 "BASH_MAX_DEFAULT_MS", "BASH_DEFAULT_DEFAULT_MS")
REMOVED_WORDING = ("deny(", "onFailure", "--strict-config", "--ignore-user-config", "--ignore-rules", "--ephemeral",
                   "--sandbox", "--yolo", "--dangerously", "--context-file", "context.md", "read-only",
                   "review-read", "reads.jsonl", "2.2.3", '"web":')
#: Wording of the removed limits in the Japanese documents.
JAPANESE_WORDING = ("ガード", "許可されたコマンド", "書き込まないツール", "読み取り専用", "ランナーだけを通", "拒否")


def removed_patterns(extra_wording=()):
    """Removed code names (whole words) and wording (any case)."""
    names = re.compile(r"\b(?:" + "|".join(map(re.escape, REMOVED_NAMES)) + r")\b")
    texts = re.compile("|".join(map(re.escape, (*REMOVED_WORDING, *extra_wording))), re.IGNORECASE)
    return names, texts


class DistributionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="devflow distribution ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()

    def clone(self):
        # A new directory for every copy, so a failed case never leaves one in the next one's way.
        destination = Path(tempfile.mkdtemp(dir=self.base)) / "repository"
        return Path(shutil.copytree(ROOT, destination, ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", "dist", ".git", ".devflow")))

    def test_static_validate_current_bundle(self):
        result = package.validate(ROOT)
        self.assertEqual(result["marketplace"], "devflow-marketplace")
        self.assertEqual(result["plugins"], {"devflow": "0.7.0"})

    def test_release_versions_are_consistent(self):
        manifest = json.loads((ROOT / "plugins/devflow/.claude-plugin/plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], "0.7.0")
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

    def test_default_reviewer_time_limit_is_one_value_and_documented(self):
        # Every default reviewer of both workflows has the same limit, and the
        # documents give the review wait that limit needs.
        core = ROOT / "plugins" / "devflow" / "scripts" / "devflow_core"
        limits = {reviewer["timeout_seconds"] for flow in ("plan", "implement")
                  for reviewer in json.loads((core / flow / "defaults.json").read_text(encoding="utf-8"))["reviewers"]}
        self.assertEqual(limits, {1800})
        wait = str(review_timeout_ms(1800))
        self.assertEqual(wait, "1900000")
        for relative in ("README.md", "docs/installation.md"):
            with self.subTest(document=relative):
                self.assertIn(wait, (ROOT / relative).read_text(encoding="utf-8"))

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

    def test_the_rules_for_every_reviewer_ship_in_one_place(self):
        cloned = self.clone()
        (cloned / "plugins" / "devflow" / "scripts" / "devflow_core" / "common" / "reviewer-rules.md").unlink()
        with self.assertRaisesRegex(package.PackagingError, "Missing common/reviewer-rules.md"):
            package.validate(cloned)

    def test_no_workflow_repeats_the_rules_for_every_reviewer(self):
        # common/reviewer-rules.md is the one place for what every reviewer is asked;
        # a workflow's reviewer.md holds only its own instructions.
        core = ROOT / "plugins" / "devflow" / "scripts" / "devflow_core"
        rules = " ".join((core / "common" / "reviewer-rules.md").read_text(encoding="utf-8").split())
        sentences = [part.strip() for part in re.split(r"(?<=[.:])\s+", rules) if len(part.strip()) >= 30]
        self.assertGreaterEqual(len(sentences), 5)
        for flow in ("plan", "implement"):
            text = " ".join((core / flow / "reviewer.md").read_text(encoding="utf-8").split())
            self.assertIn("The rules for every DevFlow reviewer come after these instructions.", text)
            for sentence in sentences:
                with self.subTest(flow=flow, sentence=sentence[:40]):
                    self.assertNotIn(sentence, text)

    def test_removed_mechanisms_leave_no_names_in_the_plugin_code(self):
        # The tool limits, the reviewer isolation and their helpers were removed in
        # 0.4.0; none of their names may remain in the plugin's code, skills or
        # references. Code names are matched as whole words, flags and the other
        # wording in any case ("Read-only" too).
        names, texts = removed_patterns()
        plugin = ROOT / "plugins" / "devflow"
        self.assertFalse((plugin / "scripts" / "devflow_core" / "common" / "guard.py").exists())
        self.assertFalse((plugin / "scripts" / "devflow_core" / "common" / "shell.py").exists())
        for path in sorted(plugin.rglob("*")):
            if path.is_file() and path.suffix in {".py", ".md", ".json"} and "__pycache__" not in path.parts:
                with self.subTest(path=str(path.relative_to(ROOT))):
                    text = path.read_text(encoding="utf-8")
                    self.assertEqual(names.findall(text) + texts.findall(text), [])

    def test_removed_mechanisms_leave_nothing_in_the_documents(self):
        """README, docs, examples, CI and tools name a removed mechanism only where the
        0.4.0 plan allows it, and only the allowed words there: the migration notes,
        the per-tool settings for secrets, the recorded CLI facts, and the packaging
        check that hooks never block. Every section is searched."""
        allowed = {("README.md", "## 0.4.0 への更新"): {"protocol_version"},
                   ("docs/security.md", "## 秘密を守りたいとき"): {"拒否"},
                   ("docs/validation.md", "## 実際の CLI で確かめたこと"): {"read-only", "読み取り専用"},
                   ("tools/package.py", None): {"onfailure"}}
        names, texts = removed_patterns(JAPANESE_WORDING)
        paths = [ROOT / "README.md", *(ROOT / "docs").glob("*.md"), *(ROOT / "examples").iterdir(),
                 *(path for path in (ROOT / ".github").rglob("*") if path.is_file()), *(ROOT / "tools").glob("*.py")]
        for path in sorted(paths):
            relative = path.relative_to(ROOT).as_posix()
            text = path.read_text(encoding="utf-8")
            headings = [(m.start(), m.group(0)) for m in re.finditer(r"(?m)^## .*$", text)]
            found = []
            for match in [*names.finditer(text), *texts.finditer(text)]:
                section = next((heading for at, heading in reversed(headings) if at < match.start()), None)
                if match.group(0).casefold() not in allowed.get((relative, section), set()):
                    found.append((section, match.group(0)))
            with self.subTest(path=relative):
                self.assertEqual(found, [])

    def test_hooks_that_block_or_run_for_every_tool_are_rejected(self):
        # Hooks only record and add context: they never block, and the tool hooks
        # run only for AskUserQuestion.
        cases = (
            ("onFailure", lambda hooks: hooks["PreToolUse"][0]["hooks"][0].update(onFailure="block"),
             "never block on failure: PreToolUse"),
            ("onFailure on the prompt hook", lambda hooks: hooks["UserPromptSubmit"][0]["hooks"][0].update(
                onFailure="block"), "never block on failure: UserPromptSubmit"),
            ("every tool", lambda hooks: hooks["PreToolUse"][0].update(matcher="*"),
             "PreToolUse hook must match only AskUserQuestion"),
            ("no matcher", lambda hooks: hooks["PostToolUse"][0].pop("matcher"),
             "PostToolUse hook must match only AskUserQuestion"),
            ("missing event", lambda hooks: hooks.pop("PostToolUse"), "Hooks must be exactly"),
            ("an event that records nothing", lambda hooks: hooks.update(Stop=hooks["UserPromptSubmit"]),
             "Hooks must be exactly UserPromptSubmit, PreToolUse and PostToolUse"),
        )
        for name, change, message in cases:
            with self.subTest(case=name):
                cloned = self.clone()
                path = cloned / "plugins" / "devflow" / "hooks" / "hooks.json"
                package.validate(cloned)  # The shipped configuration itself is valid.
                document = json.loads(path.read_text(encoding="utf-8"))
                change(document["hooks"])
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(package.PackagingError, message):
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
        self.assertEqual(package.validate(release)["version"], "0.7.0")
        independent = self.base / "devflow-only"
        shutil.copytree(release / "plugins" / "devflow", independent)
        self.assertFalse((independent / "shared").exists())
        result = subprocess.run([sys.executable, "-B", str(independent / "scripts" / "devflow.py"), "--version"],
            cwd=self.base, capture_output=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("0.7.0", result.stdout)
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
                "import json; print(json.dumps(sorted(str(w.resource_dir) for w in WORKFLOWS.values())))")
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(independent / "scripts")],
                                cwd=self.base, capture_output=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        for directory in json.loads(result.stdout):
            self.assertTrue(Path(directory).is_relative_to(independent), directory)

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
