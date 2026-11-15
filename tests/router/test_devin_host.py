"""Devin as a host, based on hook records captured from Devin CLI 3000.12.57.

Devin sends no cwd (DEVIN_PROJECT_DIR names the project), gives subagents no
ask_user_question, and writes the answers into tool_response.output as a line
of text followed by JSON. These tests feed the sanitized native records through
the same router, records and seal that serve Claude Code and Codex.
"""
import copy
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from devflow_core import cli, router
from devflow_core.common import sessions
from devflow_core.common.errors import DevFlowError
from devflow_core.common.hosts import DEVIN_ANSWER_PREFIX, HOSTS, canonical_payload, question_context
from devflow_core.common.runs import RunStore, make_event
from devflow_core.common.storage import atomic_json, read_json
from devflow_core.plan.transcript import capture_interview, transcript_marker
from devflow_core.workflows import WORKFLOWS
from tests.router.test_runtime import PLUGIN, RuntimeFixture

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "devin_3000_12_57"
SESSION = "glitter-rubidium"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def answered(answers: dict) -> dict:
    """A PostToolUse result in Devin's measured form."""
    return {"success": True, "error": None,
            "output": DEVIN_ANSWER_PREFIX + json.dumps(answers, ensure_ascii=False, indent=2)}


def post_with(response=None, **changes):
    value = copy.deepcopy(fixture("question_answered_post.json"))
    if response is not None:
        value["tool_response"] = response
    value.update(changes)
    return {**value, "host": "devin"}


class AnswerTests(RuntimeFixture):
    """Devin's question result is read strictly; anything unreadable is no answer."""

    def normalized(self, payload):
        return canonical_payload(question_context(payload, []), "question")

    def test_answers_are_normalized_by_question_text(self):
        result = self.normalized(post_with())
        self.assertEqual((result["outcome"], result["agent_scope"]), ("answered", "main"))
        self.assertEqual(result["tool_response"], {"answers": {"Pick a fruit": ["Apple"]}})
        self.assertEqual(result["source"]["tool_response"], fixture("question_answered_post.json")["tool_response"])
        two = self.normalized({**fixture("question_two_post.json"), "host": "devin"})
        self.assertEqual(two["tool_response"], {"answers": {
            "What should greet(name) return?": ["Hello, {name}!"],
            "How should greet handle an empty or None name?": ["Hello, guest!"]}})

    def test_unreadable_skipped_and_failed_results_are_not_answers(self):
        cases = {
            "skipped": answered({"Pick a fruit": {"selected": ["Apple"], "skipped": True}}),
            "empty selection": answered({"Pick a fruit": {"selected": [], "skipped": False}}),
            "blank choice": answered({"Pick a fruit": {"selected": [" "], "skipped": False}}),
            "no skipped field": answered({"Pick a fruit": {"selected": ["Apple"]}}),
            "unrelated key": answered({"Another question": {"selected": ["Apple"], "skipped": False}}),
            "extra key": answered({"Pick a fruit": {"selected": ["Apple"], "skipped": False},
                                   "Extra": {"selected": ["x"], "skipped": False}}),
            "no answer line": {"success": True, "error": None, "output": '{"Pick a fruit": {"selected": ["Apple"]}}'},
            "broken JSON": {"success": True, "error": None, "output": DEVIN_ANSWER_PREFIX + "{not json"},
            "output not text": {"success": True, "error": None, "output": {"Pick a fruit": ["Apple"]}},
            "plain string": "User answered your questions:\n{}",
        }
        for name, response in cases.items():
            with self.subTest(name):
                result = self.normalized(post_with(response))
                self.assertEqual(result["outcome"], "unknown")
                self.assertEqual(result["tool_response"], response)
        for name, response in {"success false": {"success": False, "error": None, "output": ""},
                               "error": {"success": True, "error": "validation failed", "output": ""}}.items():
            with self.subTest(name):
                self.assertEqual(self.normalized(post_with(response))["outcome"], "failed")

    def test_duplicate_question_texts_are_not_answers(self):
        value = post_with()
        value["tool_input"]["questions"].append(copy.deepcopy(value["tool_input"]["questions"][0]))
        self.assertEqual(self.normalized(value)["outcome"], "unknown")

    def test_prompts_have_an_unknown_origin_and_devin_questions_are_the_main_conversation(self):
        prompt = canonical_payload({**fixture("prompt_skill.json"), "host": "devin"}, "prompt")
        self.assertEqual(prompt["origin"], "unknown")
        self.assertEqual(canonical_payload({**fixture("question_answered_pre.json"), "host": "devin"},
                                           "observation")["agent_scope"], "main")
        self.assertEqual(HOSTS["devin"].question_tool, "ask_user_question")

    def approval_state(self):
        return {"owner_host": "devin", "owner_session": SESSION, "escalation": {"boundary": 0}}

    def events(self, pre, post):
        return [make_event({**pre, "host": "devin"}, "observation"), make_event({**post, "host": "devin"}, "question")]

    def test_an_answer_matches_its_question_although_the_screen_adds_defaults(self):
        # Devin adds multi_select (and the answers) to tool_input between PreToolUse and PostToolUse.
        pre, post = fixture("question_answered_pre.json"), fixture("question_answered_post.json")
        self.assertNotIn("multi_select", pre["tool_input"]["questions"][0])
        self.assertIn("multi_select", post["tool_input"]["questions"][0])
        candidates = sessions.approval_candidates(self.approval_state(), self.events(pre, post))
        self.assertEqual(len(candidates), 1)
        self.assertEqual(json.loads(candidates[0]["answer_text"]), {"answers": {"Pick a fruit": ["Apple"]}})
        two = sessions.approval_candidates(self.approval_state(), self.events(
            fixture("question_two_pre.json"), fixture("question_two_post.json")))
        self.assertEqual(len(two), 1)

    def test_a_changed_question_another_session_or_host_is_not_a_candidate(self):
        pre, post = fixture("question_answered_pre.json"), fixture("question_answered_post.json")
        changed = copy.deepcopy(post)
        changed["tool_input"]["questions"][0]["question"] = "Pick a vegetable"
        multi = copy.deepcopy(post)
        multi["tool_input"]["questions"][0]["multi_select"] = True
        for name, after in {"question text": changed, "multi_select": multi}.items():
            with self.subTest(name):
                self.assertEqual(sessions.approval_candidates(self.approval_state(), self.events(pre, after)), [])
        other = {**self.approval_state(), "owner_session": "another-session"}
        self.assertEqual(sessions.approval_candidates(other, self.events(pre, post)), [])
        claude = {**self.approval_state(), "owner_host": "claude"}
        self.assertEqual(sessions.approval_candidates(claude, self.events(pre, post)), [])
        skipped = post_with(answered({"Pick a fruit": {"selected": ["Apple"], "skipped": True}}))
        self.assertEqual(sessions.approval_candidates(self.approval_state(), self.events(pre, skipped)), [])
        # A cancelled question has a PreToolUse record only: nothing to resolve with.
        only_pre = [make_event({**fixture("question_cancelled_pre.json"), "host": "devin"}, "observation")]
        self.assertEqual(sessions.approval_candidates(self.approval_state(), only_pre), [])


class DevinRouterTests(RuntimeFixture):
    def devin(self, payload, **changes):
        value = {**payload, **changes}
        return router.handle_hook(value, PLUGIN, environ={"DEVIN_PROJECT_DIR": str(self.project)}, host="devin")

    def prompt_devin(self, text, session=SESSION):
        return self.devin(fixture("prompt_skill.json"), prompt=text, session_id=session)

    def context(self, result):
        return result["hookSpecificOutput"]["additionalContext"]

    def test_the_project_comes_from_devin_project_dir_without_cwd(self):
        self.assertNotIn("cwd", fixture("prompt_skill.json"))
        for name in ("status", "doctor"):
            with self.subTest(name):
                text = self.context(self.prompt_devin("/devflow:" + name))
                self.assertIn("DevFlow Devin entry context", text)
                entry = json.loads(text.split(": ", 1)[1])
                self.assertEqual(entry["host"], "devin")
                self.assertEqual(entry["project"], str(self.project))
                self.assertEqual(entry["global_arguments"][:2], ["--host", "devin"])
                self.assertFalse((self.project / ".devflow").exists())
        with self.assertRaisesRegex(DevFlowError, "DEVIN_PROJECT_DIR"):
            router.handle_hook(fixture("prompt_skill.json"), PLUGIN, environ={}, host="devin")
        with self.assertRaisesRegex(DevFlowError, "absolute"):
            router.handle_hook(fixture("prompt_skill.json"), PLUGIN, environ={"DEVIN_PROJECT_DIR": "relative"},
                               host="devin")
        # An ordinary prompt of a session without a record changes nothing.
        self.assertIsNone(self.prompt_devin("hello"))

    def test_plan_and_implement_start_resume_and_waiting_instructions(self):
        text = self.context(self.prompt_devin("/devflow:implement feature.md"))
        self.assertIn("DevFlow Devin entry context", text)
        self.assertIn("start is pending", text)
        started = router.runner_start(self.project, SESSION, "implement", "feature.md", host="devin")
        self.assertEqual(started["owner_host"], "devin")
        text = self.context(self.prompt_devin("/devflow:resume " + started["run_id"]))
        self.assertIn("--host devin", text)
        self.assertIn("get_output", text)
        WORKFLOWS["implement"].engine(self.project, session=SESSION, host="devin").cancel(
            started["run_id"], SESSION, "End fixture")
        self.prompt_devin("next input")
        text = self.context(self.prompt_devin("/devflow:plan Add a greeting"))
        record = sessions.read_record(self.project, SESSION, WORKFLOWS, "devin")
        state = RunStore(self.project, WORKFLOWS["plan"], host="devin").read(record["run"]["run_id"])
        self.assertEqual((state["owner_host"], state["status"]), ("devin", "interview"))
        self.assertEqual(state["transcript_marker"], transcript_marker(None))
        self.assertIn("get_output", text)

    def test_a_claude_session_with_the_same_id_or_a_claude_run_is_refused(self):
        run = self.plan_start(session=SESSION)
        before = self.record(SESSION)
        with self.assertRaisesRegex(DevFlowError, "host collision"):
            self.prompt_devin("/devflow:plan Another request")
        self.assertEqual(self.record(SESSION), before)
        text = self.context(self.prompt_devin("/devflow:resume " + run, session="another-devin-session"))
        self.assertIn("cross-host resume", text)

    def seal_interview(self, *hooks):
        self.prompt_devin("/devflow:plan Add a greeting")
        for payload in hooks:
            self.devin(payload)
        record = sessions.read_record(self.project, SESSION, WORKFLOWS, "devin")
        run_id = record["run"]["run_id"]
        engine = WORKFLOWS["plan"].engine(self.project, session=SESSION, host="devin")
        draft = self.run_dir("plan", run_id) / "draft"
        atomic_json(draft / "decisions.json", {"summary": "Greeting", "decisions": ["Hello"], "constraints": [],
                                               "open_questions": []})
        return engine, run_id, draft / "decisions.json"

    def test_the_interview_is_sealed_from_devin_hook_records_only(self):
        engine, run_id, decisions = self.seal_interview(
            fixture("question_two_pre.json"), fixture("question_two_post.json"))
        engine.seal(run_id, decisions)
        interview = read_json(self.run_dir("plan", run_id) / "interview.json")
        coverage = interview["coverage"]
        self.assertEqual((coverage["mode"], coverage["answered_questions"], coverage["human_response_count"]),
                         ("hook-records-only", 1, 1))
        self.assertIn("Devin's conversation record is not read", coverage["reason"])
        self.assertNotIn("Claude", coverage["reason"])
        answer = [m for m in interview["messages"] if m["source"].get("kind") == "questionnaire_answer"]
        self.assertEqual(json.loads(answer[0]["content"])["answers"]["What should greet(name) return?"],
                         ["Hello, {name}!"])

    def test_prompts_cancelled_and_skipped_questions_do_not_seal(self):
        skipped = post_with(answered({"Pick a fruit": {"selected": ["Apple"], "skipped": True}}))
        for name, hooks in {
                "prompts only": [dict(fixture("prompt_skill.json"), prompt="Hello, guest!")],
                "cancelled": [fixture("question_cancelled_pre.json")],
                "skipped": [fixture("question_answered_pre.json"), skipped]}.items():
            with self.subTest(name):
                self.setUp()
                engine, run_id, decisions = self.seal_interview(*hooks)
                with self.assertRaisesRegex(DevFlowError, "human answer"):
                    engine.seal(run_id, decisions)

    def test_capture_counts_only_question_tool_answers(self):
        events = [canonical_payload({**fixture("prompt_skill.json"), "host": "devin"}, "prompt"),
                  canonical_payload({**fixture("prompt_skill.json"), "prompt": "answer typed in chat",
                                     "host": "devin"}, "prompt")]
        interview = capture_interview(transcript_marker(None), events, host="devin")
        self.assertEqual(interview["coverage"]["human_response_count"], 0)


class HookExitTests(RuntimeFixture):
    """Devin blocks the prompt when a hook exits with 2; a DevFlow hook never does."""

    def run_cli(self, argv, stdin="{}"):
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdin", io.StringIO(stdin)), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_hook_failures_and_malformed_hook_invocations_exit_1(self):
        for argv in (["--host", "devin", "hook"], ["--host", "unknown-host", "hook"],
                     ["--host", "devin", "--bogus", "hook"], ["--host", "devin", "hook", "extra"],
                     ["--project", str(self.project), "hook"], ["--hos", "unknown-host", "hook"],
                     ["--ho", "devin", "--sess", "x", "hook"], ["--host", "devin", "--proj", "/x", "hook"],
                     ["--proj", "plan", "--host", "unknown-host", "hook"], ["--hos", "status", "hook"],
                     ["--h", "doctor", "hook"]):
            with self.subTest(argv=argv), patch.dict("os.environ", {"DEVIN_PROJECT_DIR": str(self.project)}):
                code, out, err = self.run_cli(argv, stdin="not json")
                self.assertEqual((code, out), (1, ""))
                self.assertIn("DevFlow hook failed", err)
        code, out, _ = self.run_cli(["--host", "devin", "plan", "hook"])
        self.assertEqual(code, 2)  # Not a hook invocation: an ordinary usage error.
        self.assertIn('"error"', out)


class DevinDoctorTests(RuntimeFixture):
    def invoke_devin(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--host", "devin", "--project", str(self.project), *args])
        return code, json.loads(out.getvalue())

    def test_doctor_uses_devins_version_and_never_codex(self):
        version = {"minimum_version": "3000.12.57", "version": "3000.13.0", "supported": True, "path": "/x/devin"}
        skill = {"configured_name": "grilling", "status": "available", "static_ready": True,
                 "runtime_verified": False, "reason": "Fixture"}
        with patch.object(cli, "devin_version", return_value=version), \
             patch.object(cli, "codex_version", side_effect=AssertionError("Codex must not be probed")), \
             patch.object(cli, "claude_version", side_effect=AssertionError("Claude must not be probed")), \
             patch.object(cli, "preflight", return_value={"offline": True}), \
             patch("devflow_core.plan.interview.inspect_interview_skill", return_value=skill), \
             patch.object(cli.shutil, "which", side_effect=lambda name: "/synthetic/" + name):
            code, report = self.invoke_devin("doctor")
        self.assertEqual(code, 0)
        self.assertEqual(report["host_diagnostics"], version)
        self.assertEqual(report["executables"]["devin"], "/synthetic/devin")
        self.assertIn("Devin CLI 3000.13.0 is newer than 3000.12.57", " ".join(report["warnings"]))
        self.assertIn("Real Devin hook and interview skill execution", report["not_verified"])
        self.assertNotIn("conversation_api", report["workflows"]["plan"])
        self.assertTrue(report["ready"], report)

    def test_doctor_readiness_still_depends_on_the_configured_reviewers(self):
        atomic_json(self.project / "devflow.config.json", {"schema_version": 1, "implement": {"snapshot": {"mode": "files"}},
                    "reviewers": [{"id": "fake-a", "kind": "devin", "command": ["fake-devin"], "model": "swe-2",
                                   "effort": "high", "timeout_seconds": 60},
                                  {"id": "fake-b", "kind": "opencode", "command": ["fake-opencode"],
                                   "model": "m", "effort": "high", "timeout_seconds": 60}]})
        version = {"minimum_version": "3000.12.57", "version": "3000.12.57", "supported": True}
        skill = {"configured_name": "grilling", "status": "available", "static_ready": True, "reason": "Fixture"}
        for ready in (True, False):
            probe = (patch.object(cli, "preflight", return_value={"offline": True}) if ready else
                     patch.object(cli, "preflight", side_effect=DevFlowError("Reviewer executable not found")))
            with self.subTest(ready=ready), probe, patch.object(cli, "devin_version", return_value=version), \
                 patch("devflow_core.plan.interview.inspect_interview_skill", return_value=skill), \
                 patch.object(cli.shutil, "which", side_effect=lambda name: "/synthetic/" + name):
                code, report = self.invoke_devin("doctor")
            self.assertEqual(code, 0)
            self.assertEqual(report["ready"], ready)
            self.assertEqual([r["id"] for r in report["workflows"]["implement"]["reviewers"]], ["fake-a", "fake-b"])
