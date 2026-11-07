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
from devflow_core.common.hosts import canonical_payload, question_context
from devflow_core.common.runs import RunStore, make_event, open_escalation
from devflow_core.common.storage import atomic_json, read_json
from devflow_core.workflows import WORKFLOWS
from tests.router.test_runtime import RuntimeFixture, PLUGIN

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures' / 'codex_host_0_166_0'


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


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
                router.assert_available(self.project, 'another', router.stores(self.project, host="claude"))
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
                       '/devflow:implement --reviewer claude "opus" high -- feature.md'):
            context=self.prompt(prompt)['hookSpecificOutput']['additionalContext']
            self.assertIn('did not accept',context)
            self.assertFalse((self.project/'.devflow').exists())
