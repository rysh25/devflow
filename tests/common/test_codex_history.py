"""Public API pagination and interview assembly from measured native records."""
import io
import copy
import json
import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from devflow_core.common.hosts.codex import read_conversation
from devflow_core.common.hosts import codex as codex_host
from devflow_core.common.runs import make_event
from devflow_core.common import sessions
from devflow_core.common.errors import CaptureError, DevFlowError
from devflow_core.plan import codex_interview, commands as plan_commands
from devflow_core.common.storage import atomic_json
from devflow_core.plan.engine import Engine
from devflow_core import router
from devflow_core.workflows import WORKFLOWS
from tests.router.test_runtime import RuntimeFixture, PLUGIN

FIXTURES = Path(__file__).resolve().parents[1]/'fixtures/codex_host_0_166_0'


class HistoryTests(RuntimeFixture):
    def native_history(self, filename, *, extra_items=(), with_coverage=False):
        source = json.loads((FIXTURES/filename).read_text())
        source['pages'][-1]['result']['data'].extend(extra_items)
        frames = [{'id':1,'result':{}},source['read'],*source['pages']]
        class Process:
            stdin = io.BytesIO()
            stdout = io.BytesIO(b''.join(json.dumps(frame).encode()+b'\n' for frame in frames))
            def wait(self, timeout=None):
                return 0
        with patch('devflow_core.common.hosts.codex.shutil.which',return_value='codex'), \
             patch('devflow_core.common.hosts.codex.subprocess.Popen',return_value=Process()), \
             patch('devflow_core.common.hosts.codex.stop_tree'):
            result = read_conversation(source['read']['result']['thread']['id'], self.project)
            return result if with_coverage else result.records

    def test_unknown_items_blocks_and_async_messages_are_counted(self):
        base = {'turnId':'probe-turn', 'startedAtMs':1000}
        native = self.native_history('human_history_api_pages.json', with_coverage=True, extra_items=[
            {**base, 'item':{'id':'future', 'type':'futureItem'}},
            {**base, 'item':{'id':'async', 'type':'agentMessage', 'delivery':'async', 'text':'Child report'}},
            {**base, 'item':{'id':'mixed', 'type':'userMessage', 'content':[
                {'type':'futureBlock'}, {'type':'text', 'text':'Visible text'}]}}
        ])
        self.assertEqual(native.unknown_types, {'item:futureItem':1, 'content:futureBlock':1})
        self.assertEqual(native.ignored_types['item:asyncAgentMessage'], 1)
        self.assertEqual(native.records[-1].text, 'Visible text')
        marker, events, _ = self.capture_case()
        with patch.object(codex_interview, 'read_conversation', return_value=native):
            out = codex_interview.capture_interview(marker, events, self.project)
        self.assertEqual(out['coverage']['unknown_types'], native.unknown_types)
        self.assertEqual(out['coverage']['ignored_types'], native.ignored_types)

    def test_unknown_agent_scope_is_counted_without_becoming_an_answer(self):
        marker, events, native = self.capture_case()
        for event in events[1:]:
            event['agent_scope'] = 'unknown'
        out = self.capture(marker, events, native)
        self.assertEqual(out['coverage']['unknown_types']['agent_scope:unknown'], 2)
        self.assertEqual(out['coverage']['human_response_count'], 0)

    def test_native_paginated_items_keep_visible_text_and_discard_reasoning(self):
        rows = self.native_history('history_api_pages.json')
        self.assertEqual(sum(row.role=='user' for row in rows), 6)
        self.assertEqual(sum(row.role=='assistant' for row in rows), 6)
        self.assertTrue(any(row.text.startswith('$devflow:status') for row in rows))
        self.assertTrue(all(row.item_id and row.turn_id for row in rows))

    def test_real_alpha_reaches_seal(self):
        native = self.native_history('human_history_api_pages.json')
        payloads = [json.loads((FIXTURES/name).read_text()) for name in
                    ('prompt_human_answer.json','question_answered_pre.json','question_answered_post.json')]
        for payload in payloads:
            payload['cwd']=str(self.project)
            router.handle_hook(payload, PLUGIN, environ={}, host='codex')
        session=payloads[0]['session_id']
        record=sessions.read_record(self.project,session,WORKFLOWS,'codex')
        run=record['run']['run_id']
        engine=Engine(self.project,session=session,host='codex')
        draft=engine._run(run)/'draft/decisions.json'
        atomic_json(draft,{'summary':'Probe settled','decisions':['Alpha'],'constraints':[],'open_questions':[]})
        with patch('devflow_core.plan.codex_interview.read_conversation',return_value=codex_host.Conversation(native, {}, {})):
            result=engine.seal(run,draft)
        self.assertEqual(result['status'],'planning')
        interview=engine.store.record_json(engine.store.read(run),'interview.json')
        self.assertEqual(interview['coverage']['human_response_count'],1)
        self.assertEqual(interview['coverage']['user_responses'],0)
        self.assertIn('Alpha',json.dumps(interview))

    def capture_case(self, pre_name='question_answered_pre.json', post_name='question_answered_post.json'):
        """Replay native shapes; identity rebinding below is an explicitly derived case."""
        native = self.native_history('human_history_api_pages.json')
        start = json.loads((FIXTURES/'prompt_human_answer.json').read_text())
        pre = json.loads((FIXTURES/pre_name).read_text())
        post = json.loads((FIXTURES/post_name).read_text()) if post_name else None
        if pre['session_id'] != start['session_id']:
            # The unanswered/child captures used a different native session.
            # Rebind identities for replay, without fabricating a successful reply.
            pre['session_id'] = start['session_id']
            if not pre.get('agent_id'):
                start['turn_id'] = pre['turn_id']
                native = [replace(row, turn_id=pre['turn_id']) for row in native if row.turn_id==native[0].turn_id]
            if post:
                post['session_id'] = start['session_id']
        events = [make_event({**start, 'host':'codex'}, 'prompt')['payload'],
                  make_event({**pre, 'host':'codex', 'agent_scope':'main'}, 'observation')['payload']]
        if post:
            events.append(make_event({**post, 'host':'codex', 'agent_scope':'main'}, 'question')['payload'])
        marker = {'session_id':start['session_id'], 'turn_id':start['turn_id']}
        return marker, events, native

    def capture(self, marker, events, native):
        with patch.object(codex_interview, 'read_conversation', return_value=codex_host.Conversation(native, {}, {})):
            return codex_interview.capture_interview(marker, events, self.project)

    def test_unanswered_native_result_is_preserved_without_counting_a_human(self):
        marker, events, native = self.capture_case('question_unanswered_pre.json', 'question_unanswered_post.json')
        out = self.capture(marker, events, native)
        results = [row for row in out['messages'] if row['source']['kind']=='questionnaire_unanswered']
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['content'], events[-1]['source']['tool_response'])
        self.assertEqual(results[0]['source']['outcome'], 'unknown')
        self.assertNotIn('is_error', results[0]['source'])
        self.assertEqual((out['coverage']['human_response_count'], out['coverage']['unanswered_questions']), (0, 1))

    def test_native_cancel_without_post_and_child_question_do_not_count(self):
        for name in ('question_cancel_pre.json', 'question_child_pre.json'):
            with self.subTest(fixture=name):
                marker, events, native = self.capture_case(name, None)
                out = self.capture(marker, events, native)
                self.assertEqual(out['coverage']['human_response_count'], 0)
                if name=='question_cancel_pre.json':
                    self.assertEqual(out['coverage']['unanswered_questions'], 1)
                    self.assertTrue(any('Cancellation probe' in row['content'] for row in out['messages']))
                else:
                    self.assertFalse(any('Child capability probe' in row['content'] for row in out['messages']))

    def test_seal_refuses_native_unanswered_cancel_and_child_cases(self):
        for pre, post in (('question_unanswered_pre.json', 'question_unanswered_post.json'),
                          ('question_cancel_pre.json', None), ('question_child_pre.json', None)):
            with self.subTest(fixture=pre):
                marker, events, native = self.capture_case(pre, post)
                for event in events:
                    raw = {**event['source'], 'cwd':str(self.project)}
                    router.handle_hook(raw, PLUGIN, environ={}, host='codex')
                record = sessions.read_record(self.project, marker['session_id'], WORKFLOWS, 'codex')
                run = record['run']['run_id']
                engine = Engine(self.project, session=marker['session_id'], host='codex')
                draft = engine._run(run)/'draft/decisions.json'
                atomic_json(draft, {'summary':'Negative replay', 'decisions':[], 'constraints':[], 'open_questions':[]})
                with patch.object(codex_interview, 'read_conversation', return_value=codex_host.Conversation(native, {}, {})), \
                        self.assertRaisesRegex(DevFlowError, 'At least one recorded human answer'):
                    engine.seal(run, draft)
                self.assertFalse((engine._run(run)/'interview.json').exists())
                engine.cancel(run)

    def test_multiple_question_ids_count_as_one_tool_call(self):
        marker, events, native = self.capture_case()
        pre = copy.deepcopy(events[1]['source'])
        post = copy.deepcopy(events[2]['source'])
        second = {**pre['tool_input']['questions'][0], 'id':'second_question'}
        for raw in (pre, post):
            raw['tool_input']['questions'].append(copy.deepcopy(second))
        post['tool_response'] = json.dumps({'answers': {'probe_option':{'answers':['Alpha']},
                                                      'second_question':{'answers':['Beta']}}})
        events[1:] = [make_event(pre, 'observation')['payload'], make_event(post, 'question')['payload']]
        out = self.capture(marker, events, native)
        self.assertEqual(out['coverage']['question_count_unit'], 'tool_call')
        self.assertEqual((out['coverage']['answered_questions'], out['coverage']['human_response_count']), (1, 1))
        answer = next(row for row in out['messages'] if row['source']['kind']=='questionnaire_answer')
        self.assertEqual(json.loads(answer['content'])['answers'], {'probe_option':['Alpha'], 'second_question':['Beta']})
        empty = {**post, 'tool_response':'{"answers":{}}'}
        events[2] = make_event(empty, 'question')['payload']
        out = self.capture(marker, events, native)
        self.assertEqual((out['coverage']['unanswered_questions'], out['coverage']['human_response_count']), (1, 0))
        pre['tool_input']['answers'] = {'probe_option':'Alpha', 'second_question':'Beta'}
        post['tool_input'] = copy.deepcopy(pre['tool_input'])
        events[1:] = [make_event(pre, 'observation')['payload'], make_event(post, 'question')['payload']]
        out = self.capture(marker, events, native)
        self.assertEqual((out['coverage']['prefilled_questions'], out['coverage']['human_response_count']), (1, 0))

    def test_session_plan_doctor_checks_api_without_changing_records(self):
        engine = Engine(self.project, host='codex')
        before = {str(p):p.read_bytes() for p in self.project.rglob('*') if p.is_file()}
        for ready in (True, False):
            probe = {'checked':True, 'ready':ready, 'all_pages_verified':False}
            with self.subTest(ready=ready), \
                    patch.object(plan_commands, 'diagnose', return_value={'ready':True,'config':{}}), \
                    patch.object(engine.workflow, 'doctor', return_value={'warnings':[]}), \
                    patch.object(plan_commands, 'python_errors', return_value=[]), \
                    patch.object(plan_commands, 'diagnose_conversation', return_value=probe, create=True) as check:
                report = plan_commands.doctor(engine, None, None, 'actual-session')
                check.assert_called_once_with(self.project, 'actual-session')
                self.assertEqual(report['ready'], ready)
                self.assertEqual(report['conversation_api'], probe)
        self.assertEqual(before, {str(p):p.read_bytes() for p in self.project.rglob('*') if p.is_file()})
        self.assertFalse((self.project/'.devflow').exists())

    def test_run_doctor_probes_only_the_matching_owner_session(self):
        start = json.loads((FIXTURES/'prompt_human_answer.json').read_text())
        start['cwd'] = str(self.project)
        router.handle_hook(start, PLUGIN, environ={}, host='codex')
        owner = start['session_id']
        record = sessions.read_record(self.project, owner, WORKFLOWS, 'codex')
        run = record['run']['run_id']
        engine = Engine(self.project, host='codex')
        before = {str(p):p.read_bytes() for p in self.project.rglob('*') if p.is_file()}
        for caller in ('another-session', None, owner):
            probe = {'checked':True, 'ready':True, 'all_pages_verified':False}
            with self.subTest(caller=caller), \
                    patch.object(plan_commands, 'diagnose_conversation', return_value=probe) as check:
                report = plan_commands.doctor(engine, None, run, caller)
                self.assertEqual(report['configuration_source'], 'frozen_run')
                if caller == owner:
                    check.assert_called_once_with(self.project, owner)
                    self.assertTrue(report['conversation_api']['checked'])
                else:
                    check.assert_not_called()
                    self.assertFalse(report['conversation_api']['checked'])
                    self.assertIsNone(report['conversation_api']['ready'])
                    self.assertIn('owner session', report['conversation_api']['reason'])
        self.assertEqual(before, {str(p):p.read_bytes() for p in self.project.rglob('*') if p.is_file()})

    def test_run_doctor_does_not_probe_a_different_hosts_session(self):
        run = self.plan_start()
        engine = Engine(self.project, host='codex')
        with patch.object(plan_commands, 'diagnose_conversation') as check:
            report = plan_commands.doctor(engine, None, run, self.session)
        check.assert_not_called()
        self.assertFalse(report['conversation_api']['checked'])
        self.assertIn('host', report['conversation_api']['reason'])

    def test_cli_run_doctor_reports_another_owner_without_api_or_record_changes(self):
        from contextlib import redirect_stdout
        from devflow_core import cli
        start = json.loads((FIXTURES/'prompt_human_answer.json').read_text())
        start['cwd'] = str(self.project)
        router.handle_hook(start, PLUGIN, environ={}, host='codex')
        record = sessions.read_record(self.project, start['session_id'], WORKFLOWS, 'codex')
        run = record['run']['run_id']
        before = {str(p):p.read_bytes() for p in self.project.rglob('*') if p.is_file()}
        output = io.StringIO()
        with redirect_stdout(output), patch.object(plan_commands, 'diagnose_conversation') as probe, \
                patch.object(codex_host, '_api') as api:
            code = cli.main(['--host','codex','--project',str(self.project),
                             '--run',run,'--session','another-session','plan','doctor'])
        probe.assert_not_called()
        api.assert_not_called()
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report['configuration_source'], 'frozen_run')
        self.assertFalse(report['conversation_api']['checked'])
        self.assertIn('owner session', report['conversation_api']['reason'])
        self.assertEqual(before, {str(p):p.read_bytes() for p in self.project.rglob('*') if p.is_file()})

    def test_prefilled_question_with_ui_fields_remains_nonhuman_evidence(self):
        marker, events, native = self.capture_case()
        pre, post = (copy.deepcopy(event['source']) for event in events[1:])
        pre['tool_input']['answers'] = {'probe_option':'Alpha'}
        post['tool_input']['answers'] = {'probe_option':['Alpha']}
        post['tool_input']['annotations'] = {'probe_option':{'source':'question UI'}}
        events[1:] = [make_event(pre, 'observation')['payload'], make_event(post, 'question')['payload']]
        out = self.capture(marker, events, native)
        self.assertEqual(out['coverage']['prefilled_questions'], 1)
        self.assertEqual(out['coverage']['human_response_count'], 0)
        self.assertEqual(out['coverage']['unanswered_questions'], 0)
        answer = next(row for row in out['messages'] if row['source']['kind']=='questionnaire_prefilled_answer')
        self.assertEqual(answer['content'], post['tool_response'])
        state = {'owner_host':'codex', 'owner_session':marker['session_id'], 'escalation':{'boundary':0}}
        envelopes = [make_event(pre, 'observation'), make_event(post, 'question')]
        self.assertEqual(sessions.approval_candidates(state, envelopes), [])

    def test_prefilled_question_still_requires_question_and_pre_identity(self):
        marker, original, native = self.capture_case()
        for changed_field in ('question', 'options', 'turn_id', 'repeated_pre'):
            events = copy.deepcopy(original)
            for event in events[1:]:
                event['tool_input']['answers'] = {'probe_option':'Alpha'}
            if changed_field == 'question':
                events[-1]['tool_input']['questions'][0]['question'] = 'Different question?'
            elif changed_field == 'options':
                events[-1]['tool_input']['questions'][0]['options'][0]['label'] = 'Different option'
            elif changed_field == 'turn_id':
                events[-1]['turn_id'] = 'another-turn'
            else:
                repeated = copy.deepcopy(events[1])
                repeated['tool_input']['answers'] = {'probe_option':'Beta'}
                events.insert(2, repeated)
            with self.subTest(field=changed_field), self.assertRaisesRegex(CaptureError, 'match'):
                self.capture(marker, events, native)

    def test_global_doctor_keeps_api_probe_offline_and_exposes_limits(self):
        from devflow_core import cli
        with patch.object(cli, 'preflight', return_value={}), \
                patch.object(cli, 'codex_version', return_value={'supported':True, 'version':'0.166.0'}), \
                patch.object(codex_host.subprocess, 'Popen') as launch:
            report = cli.doctor(self.project, 'codex')
        launch.assert_not_called()
        api = report['workflows']['plan']['conversation_api']
        self.assertFalse(api['checked'])
        self.assertEqual(api['timeout_seconds_per_call'], 30)
        self.assertFalse((self.project/'.devflow').exists())

    def test_post_question_must_match_pre_question_verbatim(self):
        marker, events, native = self.capture_case()
        for field in ('question', 'options'):
            changed = copy.deepcopy(events)
            question = changed[-1]['tool_input']['questions'][0]
            if field=='question':
                question[field] = 'A different question?'
            else:
                question[field][0]['label'] = 'A different option'
            with self.subTest(field=field), self.assertRaisesRegex(CaptureError, 'match'):
                self.capture(marker, changed, native)

    def test_native_item_order_wins_over_clock_order(self):
        marker, events, native = self.capture_case()
        native = [replace(row, started_at_ms=native[0].started_at_ms-index*1000) for index,row in enumerate(native)]
        out = self.capture(marker, events[:1], native)
        self.assertEqual([row['content'] for row in out['messages']], [row.text for row in native])

    def test_native_clock_reversal_marks_hooks_ambiguous_and_preserves_source_order(self):
        marker, events, original = self.capture_case()
        rows = [row for row in original if row.turn_id==marker['turn_id']]
        base = rows[0].started_at_ms
        for offsets, ambiguous in (((0, 100, 300), False), ((0, 300, 100), True)):
            native = [replace(row, started_at_ms=base+offset) for row,offset in zip(rows, offsets)]
            for event,offset in zip(events[1:], (200, 250)):
                event['captured_at'] = datetime.fromtimestamp((base+offset)/1000, timezone.utc).isoformat(timespec='milliseconds')
            with self.subTest(native_offsets=offsets):
                out = self.capture(marker, events, native)
                visible = [row for row in out['messages'] if row['source']['origin']=='codex_api']
                hooks = [row for row in out['messages'] if row['source']['origin']=='canonical_event']
                self.assertEqual([row['source']['item_id'] for row in visible], [row.item_id for row in native])
                self.assertEqual([row['content'] for row in visible], [row.text for row in native])
                self.assertEqual([row['source']['hook_event_index'] for row in hooks], [1, 2])
                self.assertEqual([row['source']['ordering_ambiguous'] for row in hooks], [ambiguous]*2)
                self.assertEqual(out['coverage']['human_response_count'], 1)

    def test_clock_reversal_in_another_turn_does_not_mark_this_turn(self):
        marker, events, original = self.capture_case()
        rows = [row for row in original if row.turn_id==marker['turn_id']]
        later = [row for row in original if row.turn_id!=marker['turn_id']]
        self.assertGreaterEqual(len(later), 2)
        base = rows[0].started_at_ms
        native = [replace(row, started_at_ms=base+offset) for row,offset in zip(rows, (0, 100, 300))]
        native += [replace(row, started_at_ms=base+1000-index*100) for index,row in enumerate(later)]
        for event,offset in zip(events[1:], (200, 250)):
            event['captured_at'] = datetime.fromtimestamp((base+offset)/1000, timezone.utc).isoformat(timespec='milliseconds')
        out = self.capture(marker, events, native)
        hooks = [row['source'] for row in out['messages'] if row['source']['origin']=='canonical_event']
        self.assertEqual([row['ordering_ambiguous'] for row in hooks], [False, False])

    def test_questions_stay_in_their_turn_before_a_later_native_input(self):
        marker, events, native = self.capture_case()
        out = self.capture(marker, events, native)
        question_index = next(i for i,row in enumerate(out['messages']) if row['source']['kind']=='questionnaire')
        later_input = next(i for i,row in enumerate(out['messages']) if row['source'].get('turn_id')=='probe-cancel-turn')
        self.assertLess(question_index, later_input)

    def test_same_second_merge_uses_milliseconds_and_reports_coarse_capture(self):
        marker, events, native = self.capture_case()
        native = [row for row in native if row.turn_id==marker['turn_id']]
        base = native[0].started_at_ms
        native = [replace(row, started_at_ms=base+offset) for row,offset in zip(native, (0, 100, 900))]
        for event,offset in zip(events[1:], (250, 350)):
            event['captured_at'] = datetime.fromtimestamp((base+offset)/1000, timezone.utc).isoformat(timespec='milliseconds')
        out = self.capture(marker, events, native)
        self.assertEqual([row['source']['kind'] for row in out['messages']],
                         ['prompt','assistant_text','questionnaire','questionnaire_answer','assistant_text'])
        self.assertFalse(any(row['source'].get('ordering_ambiguous') for row in out['messages']))
        events[1]['captured_at'] = datetime.fromtimestamp(base/1000, timezone.utc).isoformat(timespec='seconds')
        coarse = self.capture(marker, events, native)
        question = next(row for row in coarse['messages'] if row['source']['kind']=='questionnaire')
        self.assertTrue(question['source']['ordering_ambiguous'])

    def test_equal_ms_native_precedes_hooks_and_same_anchor_keeps_hook_order(self):
        marker, events, native = self.capture_case()
        native = [row for row in native if row.turn_id==marker['turn_id']]
        base = native[0].started_at_ms
        native = [replace(row, started_at_ms=base+offset) for row,offset in zip(native, (0, 100, 900))]
        for offsets in ((100, 100), (250, 350)):
            for event, offset in zip(events[1:], offsets):
                event['captured_at'] = datetime.fromtimestamp((base+offset)/1000, timezone.utc).isoformat(timespec='milliseconds')
            with self.subTest(offsets=offsets):
                out = self.capture(marker, events, native)
                self.assertEqual([row['source']['kind'] for row in out['messages']],
                                 ['prompt','assistant_text','questionnaire','questionnaire_answer','assistant_text'])
                hooks = [row['source'] for row in out['messages'] if row['source']['origin']=='canonical_event']
                self.assertEqual([row['hook_event_index'] for row in hooks], [1, 2])
                self.assertEqual([row['ordering_ambiguous'] for row in hooks], [offsets==(100, 100)]*2)

    def test_api_rejects_unmeasured_user_content_shapes(self):
        for content in ('Plain text', {'text':'Plain text'}, None, 42):
            with self.subTest(content=content), self.assertRaisesRegex(CaptureError, 'exact text or provenance'):
                self.native_history('human_history_api_pages.json', extra_items=[{
                    'turnId':'probe-turn', 'startedAtMs':1000,
                    'item':{'id':'unsupported-shape', 'type':'userMessage', 'content':content}}])

    def test_new_canonical_capture_keeps_millisecond_resolution(self):
        event = make_event({'host':'codex','session_id':'s','prompt':'x'}, 'prompt')
        self.assertRegex(event['payload']['captured_at'], r'\.\d{3}\+00:00$')

    def test_api_timeout_is_per_call_not_the_sum_of_all_pages(self):
        # Each RPC spends 20 clock units, but the complete read takes >30.
        with patch.object(codex_host.time, 'monotonic', side_effect=range(0, 2000, 10)):
            rows = self.native_history('human_history_api_pages.json')
        self.assertTrue(rows)

    def test_cleanup_errors_preserve_the_original_api_failure(self):
        class BadClose(io.BytesIO):
            def close(self):
                super().close()
                raise OSError('cleanup close failed')
        class Process:
            stdin = BadClose()
            stdout = BadClose(b'{"id":1,"result":{}}\n{"id":2,"result":{"thread":{"id":"wrong"}}}\n')
            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired('codex', timeout)
        with patch.object(codex_host.shutil, 'which', return_value='codex'), \
                patch.object(codex_host.subprocess, 'Popen', return_value=Process()), \
                patch.object(codex_host, 'stop_tree'), \
                self.assertRaisesRegex(CaptureError, 'identity'):
            read_conversation('expected', self.project)
