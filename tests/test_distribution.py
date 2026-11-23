"""Reproducible ZIP, manifest integrity, and one self-contained plugin."""
import ast
import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from devflow_core.common.environment import review_timeout_ms
from devflow_core.common.settings import HOST_CONFIG_FILES

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


def release_files():
    """The files a release ships: tracked files plus untracked files still
    eligible to ship; Git-ignored local work products never enter, and an
    index entry whose file was removed counts for neither. None outside a
    Git checkout, where no tracking can be consulted."""
    listed = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                            cwd=ROOT, capture_output=True, timeout=30)
    if listed.returncode:
        return None
    return sorted(name for name in filter(None, listed.stdout.decode("utf-8").split("\0"))
                  if os.path.lexists(ROOT / name))


def shipped_documents(paths):
    """The repository's documents among ``paths``: exactly the release-files
    set, so the content checks and the archive see the same inputs."""
    names = release_files()
    if names is None:
        return list(paths)
    shipped = {ROOT / name for name in names}
    return [path for path in paths if path in shipped]


class DistributionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="devflow distribution ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()

    def clone(self):
        """A clean release input in a new directory: exactly the release-files
        set, so a preserved local note can stay in the checkout without
        entering the package or the checks that cover the same files."""
        destination = Path(tempfile.mkdtemp(dir=self.base)) / "repository"
        names = release_files()
        if names is None:
            return Path(shutil.copytree(ROOT, destination, ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", "dist", ".git", ".devflow")))
        for name in names:
            source = ROOT / name
            info = source.lstat()
            self.assertFalse(stat.S_ISLNK(info.st_mode)
                             or (getattr(info, "st_file_attributes", 0) & 0x400),
                             f"release input is a symlink/reparse point: {name}")
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target, follow_symlinks=False)
        return destination

    def test_static_validate_current_bundle(self):
        result = package.validate(self.clone())
        self.assertEqual(result["marketplace"], "devflow-marketplace")
        self.assertEqual(result["plugins"], {"devflow": "0.12.13"})

    def test_release_versions_are_consistent(self):
        manifest = json.loads((ROOT / "plugins/devflow/.claude-plugin/plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], "0.12.13")
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
        self.assertEqual(wait, "1915000")
        for relative in ("README.md", "docs/installation.md"):
            with self.subTest(document=relative):
                self.assertIn(wait, (ROOT / relative).read_text(encoding="utf-8"))

    def test_default_reviewer_models_are_documented(self):
        # The review contract table in the README names every default reviewer's
        # model and effort as `model` / `effort`; a forgotten update fails here.
        core = ROOT / "plugins" / "devflow" / "scripts" / "devflow_core"
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for flow in ("plan", "implement"):
            reviewers = json.loads((core / flow / "defaults.json").read_text(encoding="utf-8"))["reviewers"]
            for reviewer in reviewers:
                with self.subTest(flow=flow, reviewer=reviewer["id"]):
                    self.assertIn("`{}` / `{}`".format(reviewer["model"], reviewer["effort"]), readme)

    def test_user_documents_only_describe_current_single_plugin(self):
        docs = ROOT / "docs"
        user_docs = sorted(path for path in docs.rglob("*.md") if not path.is_relative_to(docs / "plans"))
        paths = shipped_documents([ROOT / "README.md", *user_docs, *sorted((ROOT / "examples").rglob("*.md"))])
        old_names = ("plan" + "flow", "build" + "flow")
        # Upgrade instructions are required for 0.8.0's host/record changes;
        # the generic term must not be confused with the removed plugins.
        obsolete = ("旧版", "grill-me", *old_names)
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
        source = self.clone()
        first = package.build(source, self.base / "first.zip")
        second = package.build(source, self.base / "second.zip")
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
        # common/reviewer-rules.md is the one place for what every reviewer is asked
        # and for the design judgment criteria (its Design balance section). A
        # workflow's reviewer.md and the workflow references do not repeat it; the
        # references point to that section instead.
        plugin = ROOT / "plugins" / "devflow"
        core = plugin / "scripts" / "devflow_core"
        rules = " ".join((core / "common" / "reviewer-rules.md").read_text(encoding="utf-8").split())
        sentences = [part.strip() for part in re.split(r"(?<=[.:])\s+", rules) if len(part.strip()) >= 30]
        self.assertGreaterEqual(len(sentences), 5)
        for flow in ("plan", "implement"):
            text = " ".join((core / flow / "reviewer.md").read_text(encoding="utf-8").split())
            self.assertIn("The rules for every DevFlow reviewer come after these instructions.", text)
            for sentence in sentences:
                with self.subTest(flow=flow, sentence=sentence[:40]):
                    self.assertNotIn(sentence, text)
        for name in ("plan-workflow.md", "implement-workflow.md"):
            text = " ".join((plugin / "references" / name).read_text(encoding="utf-8").split())
            self.assertIn("Design balance section of `scripts/devflow_core/common/reviewer-rules.md`", text)
            for sentence in sentences:
                with self.subTest(reference=name, sentence=sentence[:40]):
                    self.assertNotIn(sentence, text)

    def test_the_rules_for_every_reviewer_define_what_to_report_and_severity(self):
        # The What to report section sits before Design balance in the one common
        # rules file, defines each of the four severities exactly once, and the
        # two sentences it replaced are gone from the per-flow reviewer.md files.
        core = ROOT / "plugins" / "devflow" / "scripts" / "devflow_core"
        rules = (core / "common" / "reviewer-rules.md").read_text(encoding="utf-8")
        headings = list(re.finditer(r"(?m)^### What to report$", rules))
        self.assertEqual(len(headings), 1)
        design = re.search(r"(?m)^### Design balance$", rules)
        self.assertIsNotNone(design)
        self.assertLess(headings[0].start(), design.start())
        section = rules[headings[0].end():design.start()]
        for severity in ("critical", "high", "medium", "low"):
            labels = list(re.finditer(r"(?m)^- " + severity + r":", section))
            with self.subTest(severity=severity):
                self.assertEqual(len(labels), 1)
                following = re.search(r"(?m)^- ", section[labels[0].end():])
                body = section[labels[0].end():labels[0].end() + (following.start() if following else len(section))]
                self.assertTrue(body.strip())
        removed = ("Prioritize concrete problems with evidence, not stylistic preferences or speculative rewrites.",
                   "Distinguish demonstrable defects from preferences.")
        for flow in ("plan", "implement"):
            text = " ".join((core / flow / "reviewer.md").read_text(encoding="utf-8").split())
            for sentence in removed:
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
                   # Native CLI observations, not new reviewer restrictions.
                   ("docs/codex-host-validation.md", "## 配布・フック・5操作"): {"読み取り専用"},
                   ("docs/codex-host-validation.md", "## サンドボックスとランナー起動"): {"--sandbox", "読み取り専用", "拒否"},
                   ("docs/validation.md", "## 0.8.0 実装の検証（2026-11-07）"): {"拒否"},
                   ("tools/package.py", None): {"onfailure"}}
        names, texts = removed_patterns(JAPANESE_WORDING)
        paths = shipped_documents(
            [ROOT / "README.md", *(ROOT / "docs").glob("*.md"), *(ROOT / "examples").iterdir(),
             *(path for path in (ROOT / ".github").rglob("*") if path.is_file()), *(ROOT / "tools").glob("*.py")])
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
                path = cloned / "plugins" / "devflow" / "hooks" / "claude.json"
                package.validate(cloned)  # The shipped configuration itself is valid.
                document = json.loads(path.read_text(encoding="utf-8"))
                change(document["hooks"])
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(package.PackagingError, message):
                    package.validate(cloned)

    def test_devin_layout_and_skills_are_checked(self):
        """Devin: the root hooks.json in the measured form, a local-only manifest, and five
        human-only skills that pre-approve nothing (Devin expands no plugin path in a
        permission rule, so any rule would allow more than the runner)."""
        plugin = Path("plugins") / "devflow"

        def edit_json(relative, change):
            def apply(root):
                path = root / plugin / relative
                document = json.loads(path.read_text(encoding="utf-8"))
                change(document)
                path.write_text(json.dumps(document), encoding="utf-8")
            return apply

        def edit_skill(name, old, new, count=1):
            def apply(root):
                path = root / plugin / "devin-skills" / name / "SKILL.md"
                text = path.read_text(encoding="utf-8")
                self.assertIn(old, text)
                path.write_text(text.replace(old, new, count), encoding="utf-8", newline="")
            return apply

        def add_shared_hook_file(root):
            shutil.copyfile(root / plugin / "hooks" / "claude.json", root / plugin / "hooks" / "hooks.json")

        prompt = lambda hooks: hooks["hooks"]["UserPromptSubmit"][0]["hooks"][0]
        cases = (
            ("hooks/hooks.json", add_shared_hook_file, "hooks/hooks.json must not exist"),
            ("Claude manifest hooks", edit_json(".claude-plugin/plugin.json", lambda d: d.pop("hooks")),
             "must name ./hooks/claude.json"),
            ("args", edit_json("hooks.json", lambda d: prompt(d).update(args=["--host", "devin", "hook"])),
             "one command string without args"),
            ("another host", edit_json("hooks.json", lambda d: prompt(d).update(
                command=prompt(d)["command"].replace("--host devin", "--host claude"))), "explicit --host devin"),
            ("every tool", edit_json("hooks.json", lambda d: d["hooks"]["PreToolUse"][0].update(matcher=".*")),
             "match only ask_user_question"),
            ("onFailure", edit_json("hooks.json", lambda d: prompt(d).update(onFailure="block")), "never block"),
            ("Stop", edit_json("hooks.json", lambda d: d["hooks"].update(Stop=d["hooks"]["UserPromptSubmit"])),
             "Devin hooks must be exactly"),
            ("cloud", edit_json(".devin-plugin/plugin.json", lambda d: d.update(platforms=["local", "cloud"])),
             "platforms"),
            ("no platforms", edit_json(".devin-plugin/plugin.json", lambda d: d.pop("platforms")), "platforms"),
            ("version", edit_json(".devin-plugin/plugin.json", lambda d: d.update(version="0.0.1")), "versions"),
            ("manifest hooks", edit_json(".devin-plugin/plugin.json", lambda d: d.update(hooks="./hooks.json")),
             "ignores a manifest hooks path"),
            ("allowed-tools", edit_skill("plan", "triggers: [user]\n", "triggers: [user]\nallowed-tools: [exec]\n"),
             "must not pre-approve tools"),
            ("allow exec", edit_skill("plan", "triggers: [user]\n",
                                      "triggers: [user]\npermissions:\n  allow:\n    - exec\n"),
             "must not pre-approve tools"),
            ("allow uv", edit_skill("implement", "triggers: [user]\n",
                                    'triggers: [user]\npermissions:\n  allow: ["Exec(uv)"]\n'),
             "must not pre-approve tools"),
            ("model trigger", edit_skill("status", "triggers: [user]", "triggers: [user, model]"),
             "invoked by the human only"),
            ("unreadable frontmatter", edit_skill("doctor", 'description: "Check', "description: Check: x"),
             "Devin cannot read the frontmatter"),
            ("Codex wording", edit_skill("resume", "/devflow:resume", "$devflow:resume"), "another host"),
            ("no question tool", edit_skill("plan", "ask_user_question", "a question", -1), "ask_user_question"),
        )
        for name, change, message in cases:
            with self.subTest(case=name):
                cloned = self.clone()
                change(cloned)
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

    def test_a_symlink_in_the_release_input_is_rejected_before_copying(self):
        # A link among the source inputs is refused by ``lstat`` before the copy
        # could follow it and leak out-of-tree bytes into the checked clone.
        link = self.base / "release-link"
        try:
            link.symlink_to(ROOT / "tools" / "package.py")
        except (OSError, NotImplementedError):
            self.skipTest("Creating symlinks requires OS permission")
        global release_files
        original = release_files
        release_files = lambda: [str(link)] + original()
        try:
            with self.assertRaisesRegex(AssertionError, "symlink/reparse"):
                self.clone()
        finally:
            release_files = original

    def test_local_configuration_file_is_never_packaged(self):
        files = package._collect(ROOT)
        files["nested/devflow.config.local.json"] = b"{}"
        with self.assertRaisesRegex(package.PackagingError, "Potential secret"):
            package._validate_files(files)
        cloned = self.clone()
        local = cloned / "nested" / "DEVFLOW.CONFIG.LOCAL.JSON"
        local.parent.mkdir()
        local.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(package.PackagingError, "Potential secret"):
            package.build(cloned, self.base / "private.zip")
        self.assertFalse((self.base / "private.zip").exists())

    def test_root_local_configuration_is_skipped(self):
        cloned = self.clone()
        (cloned / "devflow.config.local.json").write_bytes(b'{"schema_version": 1, "marker": "LOCAL ONLY"}')
        self.assertNotIn("devflow.config.local.json", package._collect(cloned))
        self.assertEqual(package.validate(cloned)["plugins"], package.validate(ROOT)["plugins"])
        archive_path = package.build(cloned, self.base / "with-local.zip")
        with zipfile.ZipFile(archive_path) as archive:
            self.assertFalse(any(name.endswith("devflow.config.local.json") for name in archive.namelist()))
            self.assertNotIn(b"devflow.config.local.json", archive.read("devflow/SHA256SUMS"))

    def test_host_local_names_are_protected_in_collection_and_final_validation(self):
        host_local = {name for name in HOST_CONFIG_FILES if name.endswith(".local.json")}
        self.assertEqual(package.LOCAL_CONFIG_FILES, {"devflow.config.local.json", *host_local})
        distribution = package._collect(ROOT)
        read_bytes = Path.read_bytes
        for name in sorted(host_local):
            with self.subTest(name=name):
                root = Path(tempfile.mkdtemp(dir=self.base))
                local = root / name
                local.write_bytes(b"private fixture, not valid JSON")
                shared = name.removesuffix(".local.json") + ".json"
                (root / shared).write_bytes(b"{}")

                def read(path):
                    self.assertNotEqual(path, local, "Package collection must skip the local file before reading")
                    return read_bytes(path)

                with patch.object(Path, "read_bytes", autospec=True, side_effect=read):
                    files = package._collect(root)
                self.assertEqual(files, {shared: b"{}"})
                distribution.update(files)
                local.unlink()  # Case-only names can refer to the same path on Windows.
                for relative in (f"nested/{name}", name.upper()):
                    path = root / relative
                    path.parent.mkdir(exist_ok=True)
                    path.write_bytes(b"{}")
                    with self.assertRaisesRegex(package.PackagingError, "Potential secret"):
                        package._collect(root)
                    path.unlink()
                for relative in (name, f"nested/{name.upper()}"):
                    with self.assertRaisesRegex(package.PackagingError, "Potential secret"):
                        package._validate_files({relative: b"{}"})
        package._validate_files(distribution)

    def test_extracted_plugin_runs_without_repository_shared(self):
        archive_path = package.build(ROOT, self.base / "release.zip")
        extracted = self.base / "extracted"
        with zipfile.ZipFile(archive_path) as archive:
            # All paths were generated and checked in this test, not untrusted ZIP input.
            archive.extractall(extracted)
        release = extracted / "devflow"
        self.assertEqual(package.validate(release)["version"], "0.12.13")
        independent = self.base / "devflow-only"
        shutil.copytree(release / "plugins" / "devflow", independent)
        self.assertFalse((independent / "shared").exists())
        result = subprocess.run([sys.executable, "-B", str(independent / "scripts" / "devflow.py"), "--version"],
            cwd=self.base, capture_output=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("0.12.13", result.stdout)
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
                                         "--host", "claude", "--project", str(project), command],
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
