"""Run the actual 0.7.3 reader against newer data without changing any bytes.

The selected 0.8.0 release uses formats 10/3, so format-9 continuation is not
supported. If a split release preserving 9/2 is selected, it must separately
exercise actual 0.7.3 runs through resume, recovery, review and approval.
The simulated split-format downgrade below is not that continuation test.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

from devflow_core.common.storage import atomic_json, read_json
from tests.router.test_runtime import RuntimeFixture


class DowngradeTests(RuntimeFixture):
    def old_runner(self):
        fixture=Path(__file__).resolve().parents[1]/'fixtures/devflow_0_7_3.json'
        source=json.loads(fixture.read_text())
        root=self.base/'released-0.7.3'
        for relative,text in source['files'].items():
            path=root/relative
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(text,encoding='utf-8')
        return root/'scripts/devflow.py'

    def bytes(self):
        return {str(p):p.read_bytes() for p in (self.project/'.devflow').rglob('*') if p.is_file()}

    def run_old(self,*args):
        before=self.bytes()
        result=subprocess.run([sys.executable,'-B',str(self.old_runner()),'--project',str(self.project),*args],
                              cwd=self.project,capture_output=True,text=True,timeout=15,
                              env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'),shell=False)
        self.assertEqual(before,self.bytes())
        return result.returncode,json.loads(result.stdout)

    def test_formats_10_and_3_are_clear_errors_and_never_rewritten(self):
        run=self.plan_start()
        code,result=self.run_old('--run',run,'status')
        self.assertEqual(code,2)
        self.assertIn('schema_version',result['message'])
        code,result=self.run_old('--session',self.session,'status')
        self.assertEqual(code,0)
        self.assertIn('Session record',result['session_record_error'])

    def test_split_format_9_with_claude_kind_is_not_downgrade_compatible(self):
        self.prompt('/devflow:plan --reviewer claude opus high --reviewer codex m high -- Request')
        record=self.record()
        run=record['run']['run_id']
        state=read_json(self.run_dir('plan',run)/'state.json')
        state['schema_version']=9
        del state['owner_host']
        atomic_json(self.run_dir('plan',run)/'state.json',state)
        code,result=self.run_old('--run',run,'plan','doctor')
        self.assertEqual(code,2)
        self.assertIn('reviewer kind',result['message'])
