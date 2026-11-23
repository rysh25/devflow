"""Host identity and read-only operations, based on actual native hook fixtures."""
import copy
import io
import json
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from devflow_core import cli, router
from devflow_core.common import sessions
from devflow_core.common.errors import DevFlowError
from devflow_core.common.hosts import HOSTS, canonical_payload, question_context
from devflow_core.common.runs import RunStore, make_event, open_escalation
from devflow_core.common.storage import atomic_json, read_json
from devflow_core.workflows import WORKFLOWS
from tests.router.test_runtime import RuntimeFixture, PLUGIN

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures' / 'codex_host_0_166_0'


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


class HostSettingsTests(RuntimeFixture):
    def submit(self, host, project, session, operation, argument, cwd=None):
        profile = HOSTS[host]
        payload = self.payload(session=session, prompt=f"{profile.command_prefix}{operation} {argument}",
                               cwd=str(cwd or project), turn_id="fixture-turn")
        environ = {profile.project_variable: str(project)} if profile.project_variable else {}
        if host == "devin":
            del payload["cwd"]
        return router.handle_hook(payload, PLUGIN, environ=environ, host=host)

    def cli_result(self, host, project, *args, code=0):
        output = io.StringIO()
        with redirect_stdout(output), patch("devflow_core.cli.host_version",
                                           return_value={"version": None, "supported": True}):
            result = cli.main(["--host", host, "--project", str(project), *args])
        self.assertEqual(result, code, output.getvalue())
        return json.loads(output.getvalue())

    def test_native_starts_and_each_doctor_use_the_host_and_freeze_configuration(self):
        for index, host in enumerate(HOSTS):
            with self.subTest(host=host):
                project = self.base / host
                project.mkdir()
                atomic_json(project / "devflow.config.json", {})
                shared = project / f"devflow.config.{host}.json"
                local = project / f"devflow.config.{host}.local.json"
                atomic_json(shared, {"max_reviews": 12, "implement": {"snapshot": {"mode": "files"}}})
                atomic_json(local, {"max_reviews": 21 + index, "plan": {"publish": {"directory": f"plans-{host}"}},
                                    "implement": {"max_reviews": 31 + index, "max_check_attempts": 5 + index}})
                child = project / "nested"
                child.mkdir()
                (child / shared.name).write_text("ignored", encoding="utf-8")
                (project / "feature.md").write_text("# Feature\n", encoding="utf-8")
                global_report = self.cli_result(host, child, "doctor")
                self.assertEqual(global_report["workflows"]["plan"]["config"]["max_reviews"], 21 + index)
                self.assertEqual(global_report["workflows"]["implement"]["config"]["max_reviews"], 31 + index)
                for flow, expected in (("plan", 21 + index), ("implement", 31 + index)):
                    self.assertEqual(self.cli_result(host, child, flow, "doctor")["config"]["max_reviews"], expected)

                plan_session, impl_session = f"{host}-plan", f"{host}-implement"
                self.submit(host, project, plan_session, "plan", "--max-reviews 51 -- request", cwd=child)
                plan_run = sessions.read_record(project, plan_session, WORKFLOWS, host)["run"]["run_id"]
                plan_config = read_json(project / ".devflow" / "runs" / "plan" / plan_run / "config.json")
                self.assertEqual(plan_config["publish"]["directory"], f"plans-{host}")
                self.submit(host, project, impl_session, "implement", "--max-reviews 52 -- feature.md", cwd=child)
                pending = self.cli_result(host, project, "--session", impl_session, "implement", "doctor")
                self.assertEqual((pending["configuration_source"], pending["config"]["max_reviews"]),
                                 ("pending_start", 52))
                started = router.runner_start(project, impl_session, "implement", "feature.md", host=host)
                self.assertEqual((started["owner_host"], started["config"]["max_reviews"]), (host, 52))
                self.assertEqual(started["config"]["max_check_attempts"], 5 + index)

                shared.write_text("broken", encoding="utf-8")
                local.unlink()
                (project / "devflow.config.json").write_text("broken", encoding="utf-8")
                for flow, session, run, expected in (("plan", plan_session, plan_run, 51),
                                                      ("implement", impl_session, started["run_id"], 52)):
                    self.submit(host, project, session, "resume", run, cwd=child)
                    report = self.cli_result(host, project, "--run", run, flow, "doctor")
                    self.assertEqual((report["configuration_source"], report["config"]["max_reviews"]),
                                     ("frozen_run", expected))
                self.assertIn(shared.name, self.cli_result(host, project, "doctor", code=2)["message"])

    def test_selected_document_is_read_once_per_start_or_global_doctor(self):
        path = self.project / "devflow.config.codex.json"
        state = {"max_reviews": 17, "implement": {"snapshot": {"mode": "files"}}}

        def read_and_replace(path_read):
            value = read_json(path_read)
            if path_read == path:
                atomic_json(path, {"unknown": 1})
            return value

        plan_session = "codex-file-once"
        for command in ("plan", "doctor", "implement"):
            atomic_json(path, state)
            if command == "implement":
                self.submit("codex", self.project, self.session, "implement", "feature.md")
            with patch("devflow_core.common.settings.read_json", side_effect=read_and_replace) as reading:
                if command == "plan":
                    self.submit("codex", self.project, plan_session, "plan", "request")
                    plan_run = sessions.read_record(self.project, plan_session, WORKFLOWS, "codex")["run"]["run_id"]
                    config = read_json(self.run_dir("plan", plan_run) / "config.json")
                    self.assertEqual(config["max_reviews"], 17)
                elif command == "doctor":
                    report = self.cli_result("codex", self.project, "doctor")
                    self.assertTrue(all(item["config"]["max_reviews"] == 17
                                        for item in report["workflows"].values()))
                else:
                    started = router.runner_start(self.project, self.session, "implement", "feature.md", host="codex")
                    self.assertEqual(started["config"]["max_reviews"], 17)
                self.assertEqual(sum(call.args == (path,) for call in reading.call_args_list), 1, command)

    def test_implement_start_revalidates_the_file_selected_after_the_hook(self):
        path = self.project / "devflow.config.codex.json"
        self.submit("codex", self.project, self.session, "implement", "feature.md")
        atomic_json(path, {"unknown": 1})
        with self.assertRaisesRegex(DevFlowError, f"Unknown {path.name} keys"):
            router.runner_start(self.project, self.session, "implement", "feature.md", host="codex")
        root = self.project / ".devflow" / "runs" / "implement"
        self.assertFalse(any(root.glob("*/state.json")))

    def test_selected_dedicated_snapshot_syntax_is_checked_at_doctor_and_start(self):
        shared = self.project / "devflow.config.codex.json"
        local = self.project / "devflow.config.codex.local.json"
        cases = (
            ("include", ["../outside/**"], ["**"], "Unsafe snapshot glob"),
            ("context_paths", ["../outside.py"], [], "Non-portable file path"),
        )
        for field, bad, override, error in cases:
            with self.subTest(field=field):
                atomic_json(shared, {"implement": {"snapshot": {field: bad}}})
                atomic_json(local, {"implement": {"snapshot": {field: override}}})
                self.submit("codex", self.project, self.session, "implement", "feature.md")
                doctor = self.cli_result("codex", self.project, "--session", self.session, "implement", "doctor", code=2)
                self.assertIn(f"{shared.name}: {error}", doctor["message"])
                with self.assertRaisesRegex(DevFlowError, f"{shared.name}: {error}"):
                    router.runner_start(self.project, self.session, "implement", "feature.md", host="codex")
                root = self.project / ".devflow" / "runs" / "implement"
                self.assertFalse(any(root.glob("*/state.json")))


class HostTests(RuntimeFixture):
    def codex(self, name, **changes):
        payload = fixture(name)
        payload.update(cwd=str(self.project), **changes)
        return router.handle_hook(payload, PLUGIN, environ={}, host='codex')

    def all_bytes(self):
        return {str(p): p.read_bytes() for p in (self.project/'.devflow').rglob('*') if p.is_file()}

    def test_five_native_operations_have_actual_context(self):
        for name in ('status', 'doctor'):
            result = self.codex('prompt_'+name+'.json')
            self.assertIn('"host": "codex"', result['hookSpecificOutput']['additionalContext'])
            self.assertFalse((self.project/'.devflow').exists())
        result = self.codex('prompt_implement.json', prompt='$devflow:implement feature.md')
        text = result['hookSpecificOutput']['additionalContext']
        self.assertIn('"session_id"', text)
        self.assertIn('"runner"', text)
        session = fixture('prompt_implement.json')['session_id']
        started = router.runner_start(self.project, session, 'implement', 'feature.md', host='codex')
        self.assertEqual(started['owner_host'], 'codex')
        result = self.codex('prompt_resume.json', prompt='$devflow:resume '+started['run_id'])
        self.assertIn('"host": "codex"', result['hookSpecificOutput']['additionalContext'])
        # Finish the independent Implement case before testing a new Plan start.
        WORKFLOWS['implement'].engine(self.project, session=session, host='codex').cancel(started['run_id'], session, 'End fixture')
        result = self.codex('prompt_plan.json')
        context = result['hookSpecificOutput']['additionalContext']
        self.assertIn('"host": "codex"', context)
        plan_session = fixture('prompt_plan.json')['session_id']
        record = sessions.read_record(self.project, plan_session, WORKFLOWS, 'codex')
        state = RunStore(self.project, WORKFLOWS['plan'], host='codex').read(record['run']['run_id'])
        self.assertEqual((state['owner_host'], state['status']), ('codex', 'interview'))

    def test_approval_requires_the_exact_question_input_and_codex_turn(self):
        native_pre = {**fixture('question_answered_pre.json'), 'host':'codex', 'agent_scope':'main'}
        native_post = {**fixture('question_answered_post.json'), 'host':'codex', 'agent_scope':'main'}
        state = {'owner_host':'codex', 'owner_session':native_pre['session_id'], 'escalation':{'boundary':0}}
        for field in ('question', 'options', 'id', 'turn_id'):
            post = copy.deepcopy(native_post)
            if field == 'turn_id':
                post['turn_id'] = 'different-turn'
            elif field == 'question':
                post['tool_input']['questions'][0]['question'] = 'A different question?'
            elif field == 'id':
                post['tool_input']['questions'][0]['id'] = 'different_question'
                post['tool_response'] = json.dumps({'answers':{'different_question':{'answers':['Alpha']}}})
            else:
                post['tool_input']['questions'][0]['options'][0]['label'] = 'A different option'
            with self.subTest(field=field):
                events = [make_event(native_pre, 'observation'), make_event(post, 'question')]
                self.assertEqual(sessions.approval_candidates(state, events), [])

    def test_claude_child_observation_cannot_verify_a_main_question(self):
        question = {'questions':[{'question':'Continue?'}]}
        pre = self.payload('PreToolUse', tool_name='AskUserQuestion', tool_use_id='same-id',
                           tool_input=question, agent_id='child')
        post = self.payload('PostToolUse', tool_name='AskUserQuestion', tool_use_id='same-id',
                            tool_input=question, tool_response='yes')
        state = {'owner_host':'claude', 'owner_session':self.session, 'escalation':{'boundary':0}}
        events = [make_event(pre, 'observation'), make_event(post, 'question')]
        self.assertEqual(sessions.approval_candidates(state, events), [])

    def test_cross_host_resume_and_same_id_collision_change_no_records(self):
        run = self.plan_start()
        before = self.all_bytes()
        result = self.codex('prompt_resume.json', session_id='different-session', prompt='$devflow:resume '+run)
        self.assertIn('cross-host resume', result['hookSpecificOutput']['additionalContext'])
        # A rejected resume may create the standard dispatch lock; no run/session/event changes.
        after = self.all_bytes()
        self.assertEqual(before, after)
        with self.assertRaisesRegex(DevFlowError, 'host collision'):
            self.codex('prompt_plan.json', session_id=self.session)
        with self.assertRaisesRegex(DevFlowError, 'collision'):
            sessions.write_record(self.project, sessions.empty_record(self.session, 'codex'), WORKFLOWS)
        self.assertEqual(before, self.all_bytes())

    def test_old_sessions_including_pending_do_not_block_and_release_only_on_input(self):
        for pending in (False, True):
            with self.subTest(pending=pending):
                path = sessions.session_path(self.project, self.session)
                value = {'schema_version':2,'session_id':self.session,'run':None,
                         'pending_start':{'unreadable_old_payload':True} if pending else None}
                atomic_json(path, value)
                info = sessions.inspect_record(path, WORKFLOWS)
                self.assertEqual((info.format, info.host), ('unsupported', 'claude'))
                before = path.read_bytes()
                self.prompt('/devflow:status')
                self.prompt('/devflow:doctor')
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(router.status(self.project, self.session, None, host="claude")['status'], 'unsupported')
                self.assertEqual(path.read_bytes(), before)
                started = self.plan_start(session=f'another-{pending}')  # An old record never blocks a start.
                self.engine('plan').cancel(started)
                with self.assertRaisesRegex(DevFlowError, 'collision'):
                    sessions.write_record(self.project, sessions.empty_record(self.session, 'codex'), WORKFLOWS)
                self.prompt('next ordinary input')
                self.assertFalse(path.exists())

    def test_status_and_doctor_preserve_pending_and_active_records(self):
        self.prompt('/devflow:implement feature.md')
        before = self.all_bytes()
        self.prompt('/devflow:status')
        self.prompt('/devflow:doctor')
        self.assertEqual(before, self.all_bytes())
        router.runner_start(self.project, self.session, 'implement', 'feature.md', host="claude")
        before = self.all_bytes()
        self.prompt('/devflow:status')
        self.prompt('/devflow:doctor')
        self.assertEqual(before, self.all_bytes())

    def test_native_answer_normalization_and_approval_require_pair_and_root_scope(self):
        # The router establishes main scope by matching the native root turn.
        pre = canonical_payload({**fixture('question_answered_pre.json'), 'host':'codex', 'agent_scope':'main'}, 'observation')
        post = canonical_payload({**fixture('question_answered_post.json'), 'host':'codex', 'agent_scope':'main'}, 'question')
        self.assertEqual(post['tool_response'], {'answers':{'probe_option':['Alpha']}})
        self.assertEqual(post['source']['tool_name'], 'request_user_input')
        self.assertEqual(post['tool_name'], 'AskUserQuestion')
        state = {'owner_host':'codex','owner_session':post['session_id'],'event_ids':[], 'escalation':{'boundary':0}}
        events = [make_event(pre,'observation'),make_event(post,'question')]
        self.assertEqual(len(sessions.approval_candidates(state, events)), 1)
        self.assertEqual(sessions.approval_candidates(state, events[1:]), [])
        for scope in ('subagent','unknown'):
            bad = copy.deepcopy(events);bad[1]['payload']['agent_scope']=scope
            self.assertEqual(sessions.approval_candidates(state,bad), [])
        empty = canonical_payload({**fixture('question_unanswered_post.json'), 'host':'codex'}, 'question')
        self.assertNotIn('is_error', empty)
        self.assertEqual(empty['outcome'], 'unknown')

    def test_prefilled_status_uses_the_same_verified_scope_as_approval(self):
        raw = {**fixture('question_answered_pre.json'), 'host':'codex'}
        raw['tool_input']['answers'] = {'probe_option':'Alpha'}
        for scope in ('main', 'subagent', 'unknown'):
            with self.subTest(scope=scope):
                event = make_event({**raw, 'agent_scope':scope}, 'observation')
                self.assertEqual(sessions.prefilled_question_events([event]),
                                 [event['event_id']] if scope=='main' else [])

    def test_unknown_answer_does_not_invent_or_discard_native_error_status(self):
        raw = {**fixture('question_unanswered_post.json'), 'host':'codex'}
        for flag in (False, True):
            result = canonical_payload({**raw, 'is_error':flag}, 'question')
            self.assertEqual(result['is_error'], flag)
            self.assertEqual(result['outcome'], 'unknown')

    def test_canonical_scope_uses_each_hosts_native_evidence(self):
        raw = fixture('question_answered_pre.json')
        with self.assertRaisesRegex(DevFlowError, 'explicit supported --host'):
            canonical_payload(raw, 'observation')
        self.assertEqual(canonical_payload({**raw, 'host':'claude'}, 'observation')['agent_scope'], 'main')
        self.assertEqual(canonical_payload({**raw, 'host':'codex'}, 'observation')['agent_scope'], 'unknown')
        for host in ('claude', 'codex'):
            child = canonical_payload({**raw, 'host':host, 'agent_id':'child', 'agent_scope':'main'}, 'observation')
            self.assertEqual(child['agent_scope'], 'subagent')
        prompt = canonical_payload({**fixture('prompt_plan.json'), 'host':'codex'}, 'prompt')
        self.assertEqual(prompt['origin'], 'unknown')

    def test_host_adapter_correlates_only_the_same_root_host_session_and_turn(self):
        payload = {**fixture('question_answered_pre.json'), 'host':'codex'}
        prompt = {'host':'codex', 'session_id':payload['session_id'], 'turn_id':payload['turn_id'], 'prompt':'Start'}
        for changes, scope in (({}, 'main'), ({'host':'claude'}, 'unknown'),
                               ({'session_id':'other'}, 'unknown'), ({'turn_id':'other'}, 'unknown'),
                               ({'agent_id':'child'}, 'unknown')):
            event = make_event({**prompt, **changes}, 'prompt')
            with self.subTest(changes=changes):
                result = question_context(payload, [event])
                self.assertEqual(result['agent_scope'], scope)
        self.assertEqual(question_context({**payload, 'agent_id':'child'}, [])['agent_scope'], 'subagent')
        self.assertEqual(question_context({**payload, 'turn_id':None}, [])['agent_scope'], 'unknown')

    def test_startup_override_freezes_raw_body_and_settings(self):
        raw = '/devflow:plan --reviewer claude opus high --reviewer codex gpt-6.1-sol xhigh --\u3000依頼  \n'
        self.prompt(raw)
        run = self.record()['run']['run_id']
        folder = self.run_dir('plan', run)
        request = read_json(folder/'request.json')
        self.assertEqual(request['request'], '依頼  \n')
        config = read_json(folder/'config.json')
        self.assertEqual([r['kind'] for r in config['reviewers']], ['claude','codex'])
        atomic_json(self.project/'devflow.config.local.json', {'plan':{'max_reviews':2}})
        self.assertEqual(read_json(folder/'config.json'), config)

    def test_max_reviews_option_freezes_the_limit_and_one_reviewer(self):
        self.prompt('/devflow:plan --max-reviews 2 --reviewer codex m high -- 依頼')
        run = self.record()['run']['run_id']
        folder = self.run_dir('plan', run)
        config = read_json(folder/'config.json')
        self.assertEqual((config['max_reviews'], [r['id'] for r in config['reviewers']]), (2, ['codex']))
        self.assertEqual(read_json(folder/'state.json')['review_limit'], 2)
        self.assertEqual(read_json(folder/'request.json')['request'], '依頼')
        # Later settings changes do not touch the frozen limit.
        atomic_json(self.project/'devflow.config.local.json', {'plan':{'max_reviews':5}})
        self.assertEqual(read_json(folder/'config.json'), config)

    def test_implement_max_reviews_option_is_frozen_at_start(self):
        self.prompt('/devflow:implement --max-reviews 3 --reviewer claude opus high -- feature.md')
        result=router.runner_start(self.project,self.session,'implement','feature.md', host="claude")
        self.assertEqual((result['config']['max_reviews'], [r['id'] for r in result['config']['reviewers']]),
                         (3, ['claude']))
        self.assertEqual(result['review_limit'], 3)

    def test_missing_host_fails_before_any_hook_or_mutation(self):
        for args in (['hook'], ['status'], ['doctor'], ['plan','status'], ['implement','doctor'],
                     ['--session','s','status'], ['plan','cancel']):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err), patch('sys.stdin', io.StringIO('{}')):
                code = cli.main(['--project', str(self.project), *args])
            self.assertNotEqual(code, 0)
            self.assertIn('--host', out.getvalue()+err.getvalue())
            self.assertFalse((self.project/'.devflow').exists())

    def test_help_and_version_work_without_host(self):
        for args in (['--help'], ['--version'], ['plan','--help'], ['implement','--help']):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err), self.assertRaises(SystemExit) as raised:
                cli.main(args)
            self.assertEqual(raised.exception.code, 0)
            self.assertTrue(out.getvalue())
            self.assertEqual(err.getvalue(), '')
        self.assertFalse((self.project/'.devflow').exists())

    def test_implement_settings_failure_keeps_pending_for_retry(self):
        self.prompt('/devflow:implement --reviewer claude opus high --reviewer codex m high -- feature.md')
        original=self.record()
        atomic_json(self.project/'devflow.config.local.json', {'implement':{'max_reviews':True}})
        with self.assertRaises(DevFlowError):
            router.runner_start(self.project,self.session,'implement','feature.md', host="claude")
        self.assertEqual(self.record(),original)
        self.prompt('/devflow:status')
        self.prompt('/devflow:doctor')
        self.assertEqual(self.record(),original)
        atomic_json(self.project/'devflow.config.local.json', {})
        result=router.runner_start(self.project,self.session,'implement','feature.md', host="claude")
        self.assertEqual([r['kind'] for r in result['config']['reviewers']],['claude','codex'])

    def test_syntax_error_never_leaves_pending_or_run(self):
        for prompt in ('/devflow:plan --reviwer claude opus high -- request',
                       '/devflow:implement --rev claude opus high -- feature.md',
                       '/devflow:implement --reviewer claude "opus" high -- feature.md',
                       '/devflow:plan --max-reviews 0 -- request',
                       '/devflow:implement --max-reviews 100 -- feature.md',
                       '/devflow:implement --max-reviews 2 --max-reviews 3 -- feature.md'):
            context=self.prompt(prompt)['hookSpecificOutput']['additionalContext']
            self.assertIn('did not accept',context)
            self.assertFalse((self.project/'.devflow').exists())
