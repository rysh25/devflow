"""Devin as a reviewer CLI and Devin's interview-skill metadata, offline.

The reviewer runs as a fake local process that answers like Devin CLI
3000.12.57 (stdout holds only the final answer; --export writes ATIF-v1.7).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from devflow_core.common import reviewers
from devflow_core.common.errors import ReviewerError
from devflow_core.common.hosts.skills import DEVIN_PROJECT_SKILLS, devin_user_skill_roots
from devflow_core.common.startup import override_reviewers, parse_start
from devflow_core.plan.interview import devin_frontmatter, inspect_interview_skill

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "devin_3000_12_57"
REVIEW = {"schema_version": 1, "verdict": "pass", "summary": "ok", "findings": [], "questions": []}

FAKE_DEVIN = r'''
import json, shutil, sys
from pathlib import Path
args = sys.argv[1:]
mode = args.pop(0)
if args == ["--version"]:
    print("devin 3000.12.57 (96542e930752)"); sys.exit(0)
if args == ["--help"]:
    flags = ["--print", "--prompt-file", "--model", "--export"]
    if mode == "old":
        flags.remove("--export")
    print("Usage: devin [OPTIONS]\n" + "\n".join("  " + f for f in flags)); sys.exit(0)
if mode == "untrusted":
    print("Error: Refusing to run in an untrusted workspace", file=sys.stderr); sys.exit(1)
stdin = sys.stdin.read() if not sys.stdin.closed else ""
values = dict(zip(args[1::2], args[2::2])) if args[0] == "--print" else {}
work = Path(values["--prompt-file"]).parent
(work / "seen.json").write_text(json.dumps({"argv": args, "stdin": stdin}))
fixtures = Path(sys.argv[0]).parent
if mode != "no-export":
    shutil.copyfile(fixtures / "review_export.json", values["--export"])
if mode != "empty":
    print(json.dumps(%s))
''' % json.dumps(REVIEW)


class DevinReviewerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="devflow devin ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.project = self.base / "project"
        self.project.mkdir()
        self.fake = self.base / "fake devin.py"
        self.fake.write_text(FAKE_DEVIN, encoding="utf-8")
        (self.base / "review_export.json").write_bytes((FIXTURES / "review_export.json").read_bytes())
        self.schema = self.base / "schema.json"
        self.schema.write_text("{}", encoding="utf-8")
        self.instructions = self.base / "instructions.md"
        self.instructions.write_text("# Review\n", encoding="utf-8")
        self.work = self.base / "attempt 1"

    def config(self, mode="pass"):
        return {"id": "devin", "kind": "devin", "command": [sys.executable, str(self.fake), mode],
                "model": "swe-2", "effort": "high", "timeout_seconds": 30}

    def run_review(self, mode="pass"):
        config = self.config(mode)
        prepared = reviewers.prepare_review(config, b"packet", self.work, self.schema, self.instructions,
                                            cwd=self.project, probe=reviewers.preflight(config))
        return reviewers.run_review(prepared, lambda value: value), config

    def test_arguments_pass_the_prompt_file_model_effort_and_export(self):
        cli = reviewers.CLIS["devin"]
        self.assertEqual(cli.command, ("devin",))
        self.assertEqual(cli.arguments("swe-2", "high", Path("/w"), Path("/c"), Path("/s")),
                         ["--print", "--prompt-file", str(Path("/w") / "review-input.md"), "--model", "swe-2-high",
                          "--export", str(Path("/w") / "export.json")])
        self.assertIsNone(cli.stdin("prompt"))
        self.assertEqual(cli.flags(), ["--print", "--prompt-file", "--model", "--export"])

    def test_a_review_reads_the_answer_from_stdout_and_activity_and_tokens_from_the_export(self):
        result, config = self.run_review()
        self.assertEqual(result["review"], REVIEW)
        self.assertEqual(result["activity"]["tool_calls"], {"grep": 1, "read": 1})
        seen = json.loads((self.work / "seen.json").read_text())
        self.assertEqual(seen["stdin"], "")
        self.assertEqual(seen["argv"][:2], ["--print", "--prompt-file"])
        self.assertEqual(reviewers.review_tokens(config, self.work),
                         {"total_cached_tokens": 11520, "total_completion_tokens": 120, "total_prompt_tokens": 29094})

    def test_the_measured_stdout_is_the_answer_alone(self):
        self.assertEqual(json.loads((FIXTURES / "review_stdout.txt").read_text()),
                         {"verdict": "pass", "title_found": True})

    def test_unknown_export_shapes_are_ignored_not_counted(self):
        self.work.mkdir()
        output = self.work / "stdout.jsonl"
        output.write_text(json.dumps(REVIEW), encoding="utf-8")
        (self.work / "export.json").write_text(json.dumps({
            "steps": [{"tool_calls": [None, 7, {}, {"function_name": 3}, {"function_name": "read"}]}, "odd"],
            "final_metrics": {"total_prompt_tokens": {"unexpected": 5}, "total_completion_tokens": -1,
                              "total_cached_tokens": True, "other_tokens": 9}}), encoding="utf-8")
        cli = reviewers.CLIS["devin"]
        review, activity = cli.answer(output, self.work)
        self.assertEqual(review, REVIEW)
        self.assertEqual((activity["tool_call_count"], activity["tool_calls"]), (1, {"read": 1}))
        self.assertIsNone(cli.tokens(output, self.work))
        (self.work / "export.json").write_text(json.dumps({"final_metrics": {
            "total_prompt_tokens": 10, "total_completion_tokens": {"x": 1}}}), encoding="utf-8")
        self.assertEqual(cli.tokens(output, self.work), {"total_prompt_tokens": 10})

    def test_a_deeply_nested_export_is_unreadable_but_keeps_the_answer(self):
        self.work.mkdir()
        output = self.work / "stdout.jsonl"
        output.write_text(json.dumps(REVIEW), encoding="utf-8")
        (self.work / "export.json").write_text('{"steps": [], "x": ' + "[" * 100000 + "]" * 100000 + "}",
                                               encoding="utf-8")
        cli = reviewers.CLIS["devin"]
        review, activity = cli.answer(output, self.work)
        self.assertEqual(review, REVIEW)
        self.assertEqual(activity["other_events"], {"(no readable export)": 1})
        self.assertIsNone(cli.tokens(output, self.work))

    def test_a_missing_export_leaves_activity_and_tokens_unknown_but_keeps_the_answer(self):
        result, config = self.run_review("no-export")
        self.assertEqual(result["review"], REVIEW)
        self.assertEqual(result["activity"]["tool_call_count"], 0)
        self.assertEqual(result["activity"]["other_events"], {"(no readable export)": 1})
        self.assertIsNone(reviewers.review_tokens(config, self.work))

    def test_an_empty_answer_fails_and_an_untrusted_workspace_says_how_to_fix_it(self):
        with self.assertRaisesRegex(ReviewerError, "Devin finished without an answer"):
            self.run_review("empty")
        self.work = self.base / "attempt 2"
        with self.assertRaisesRegex(ReviewerError, "trust it"):
            self.run_review("untrusted")

    def test_preflight_requires_every_flag_devflow_passes(self):
        self.assertEqual(reviewers.preflight(self.config())["cli_version"], "devin 3000.12.57 (96542e930752)")
        with self.assertRaisesRegex(ReviewerError, "--export"):
            reviewers.preflight(self.config("old"))

    def test_a_startup_reviewer_without_devin_in_the_settings_uses_the_devin_command(self):
        config = {"max_reviews": 6, "reviewers": [
            {"id": "codex", "kind": "codex", "command": ["codex"], "model": "gpt-6.1-sol", "effort": "high",
             "timeout_seconds": 1800},
            {"id": "opencode", "kind": "opencode", "command": ["opencode"], "model": "m", "effort": "high",
             "timeout_seconds": 1800}]}
        startup = parse_start("--reviewer devin swe-2 high --reviewer codex gpt-6.1-sol high -- feature.md")
        result = override_reviewers(config, startup)
        self.assertEqual([(r["id"], r["command"], r["model"]) for r in result["reviewers"]],
                         [("devin", ["devin"], "swe-2"), ("codex", ["codex"], "gpt-6.1-sol")])


class DevinSkillTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="devflow devin skills ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.project = self.base / "project"
        self.home = self.base / "home"
        self.project.mkdir()
        self.home.mkdir()

    def write(self, directory: Path, text: str) -> Path:
        path = directory / "grilling" / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def environment(self):
        """The user's own variables point into the temporary home on every OS."""
        environ = patch.dict(os.environ, {"APPDATA": str(self.home / "AppData")})
        environ.start()
        self.addCleanup(environ.stop)
        os.environ.pop("COPILOT_HOME", None)

    def inspect(self):
        self.environment()
        return inspect_interview_skill(self.project, "grilling", config_dir=self.home, host="devin")

    def test_each_documented_location_alone_is_found(self):
        self.environment()
        roots = [root for _, root in devin_user_skill_roots(self.home)]
        roots += [self.project / path for path in DEVIN_PROJECT_SKILLS]
        self.assertIn(self.home / ".config" / "cognition" / "skills", roots)
        # Windsurf's global skills depend on Devin's release channel, which nothing offline names.
        self.assertFalse(any(".codeium" in root.parts for root in roots if self.home in root.parents))
        self.assertIn(self.project / ".claude" / "skills", roots)
        self.assertNotIn(self.home / ".claude" / "skills", roots)  # Devin imports no global Claude skills.
        for root in roots:
            with self.subTest(root=root):
                path = self.write(root, "---\nname: grilling\ndescription: \"Interview\"\n---\nAsk.\n")
                report = self.inspect()
                self.assertEqual((report["status"], report["source"]), ("available", str(path)))
                path.unlink()
        report = self.inspect()
        self.assertEqual(report["status"], "missing")
        self.assertIn("checked locations", report["reason"])
        self.assertNotIn("not exist", report["reason"])

    def test_the_parent_project_is_not_searched(self):
        nested = self.project / "child"
        nested.mkdir()
        self.write(self.project / ".devin" / "skills", "---\nname: grilling\n---\n")
        self.assertEqual(inspect_interview_skill(nested, "grilling", config_dir=self.home, host="devin")["status"],
                         "missing")

    def test_windows_and_copilot_locations_follow_their_variables(self):
        roots = dict((str(root), scope) for scope, root in devin_user_skill_roots(
            self.home, {"APPDATA": str(self.base / "AppData"), "COPILOT_HOME": str(self.base / "copilot")},
            windows=True))
        self.assertIn(str(self.base / "AppData" / "devin" / "skills"), roots)
        self.assertIn(str(self.base / "copilot" / "skills"), roots)
        self.assertNotIn(str(self.home / ".config" / "devin" / "skills"), roots)
        self.assertIn(str(self.home / ".config" / "devin" / "skills"),
                      {str(root) for _, root in devin_user_skill_roots(self.home, {}, windows=False)})

    def test_frontmatter_decides_whether_the_model_can_hold_the_interview(self):
        cases = {
            "triggers: [user]": "blocked",
            "triggers: [user, model]": "available",
            "triggers:\n  - user\n  - model": "available",
            "triggers:\n- model": "available",
            "triggers:\n  - user\n - model": "unverified",
            "triggers:\n - user\n  - model": "unverified",
            "description: \"no triggers\"": "available",
            "subagent: true": "blocked",
            "subagent: false": "available",
            "agent: reviewer": "blocked",
            "agent:": "available",
            "agent: []": "unverified",
            "agent:\n  - reviewer": "unverified",
            "subagent: maybe": "unverified",
            "subagent: False  # YAML boolean": "available",
            "subagent: TRUE": "blocked",
            # Devin documents triggers as a list and subagent as a boolean; nothing else is coerced.
            "triggers: model": "unverified",
            "triggers:": "unverified",
            "subagent: \"false\"": "unverified",
            "subagent: 0": "unverified",
            "subagent: yes": "unverified",
            "subagent:": "unverified",
            "triggers:\n  user: yes": "unverified",
            "description: Probe: unquoted colon": "invalid",
            # Claude's fields do not decide anything on Devin.
            "disable-model-invocation: true\ncontext: fork": "available",
        }
        for header, status in cases.items():
            with self.subTest(header=header):
                self.write(self.home / ".agents" / "skills", f"---\nname: grilling\n{header}\n---\nBody\n")
                self.assertEqual(self.inspect()["status"], status)

    def test_devins_documented_example_is_read(self):
        example = ("name: review\ndescription: Review code changes for issues\nargument-hint: \"[filename]\"\n"
                   "model: sonnet\nallowed-tools:\n  - read\n  - grep\n  - glob\n  - exec\n"
                   "permissions:\n  allow:\n    - Exec(npm run test)\n  deny:\n    - Write(/etc/**)\n"
                   "triggers:\n  - user\n  - model\n")
        fields = devin_frontmatter(example)
        self.assertEqual(fields["triggers"], ["user", "model"])
        self.assertEqual(fields["allowed-tools"], ["read", "grep", "glob", "exec"])
        self.assertIn("permissions", fields)
        self.write(self.home / ".agents" / "skills", "---\n" + example + "---\nBody\n")
        self.assertEqual(self.inspect()["status"], "available")

    @unittest.skipIf(os.name == "nt", "Creating symlinks needs extra rights on Windows")
    def test_symlinked_copies_are_one_definition(self):
        path = self.write(self.home / ".agents" / "skills", "---\nname: grilling\n---\n")
        (self.project / ".claude" / "skills").mkdir(parents=True)
        (self.project / ".claude" / "skills" / "grilling").symlink_to(path.parent, target_is_directory=True)
        self.assertEqual(self.inspect()["status"], "available")


if __name__ == "__main__":
    unittest.main()
