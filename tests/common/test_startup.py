import copy
from pathlib import Path
import tempfile
import unittest

from devflow_core.common.errors import DevFlowError
from devflow_core.common.operations import public_command
from devflow_core.common.startup import override_reviewers, parse_start
from devflow_core.workflows import WORKFLOWS


class StartupTests(unittest.TestCase):
    def config(self):
        with tempfile.TemporaryDirectory() as folder:
            return WORKFLOWS["plan"].load_config(Path(folder))

    def test_boundary_and_unicode_whitespace_preserve_plan_body(self):
        raw = '--reviewer claude opus high --reviewer codex gpt-6.1-sol xhigh --   REVIEW-42 を計画して  \n'
        parsed = parse_start(raw)
        self.assertEqual(parsed.body, 'REVIEW-42 を計画して  \n')
        self.assertEqual(parsed.reviewers, (("claude", "opus", "high"), ("codex", "gpt-6.1-sol", "xhigh")))
        self.assertEqual(parse_start('--\u3000依頼  ').body, '依頼  ')
        self.assertEqual(parse_start('  本文 -- は本文  ').body, '本文 -- は本文  ')

    def test_typos_help_abbreviation_missing_separator_are_errors(self):
        for raw in ('--reviwer claude opus high -- request', '--rev claude opus high -- request',
                    '--help -- request', '--reviewer claude opus high request', '--reviewer claude -- request'):
            with self.subTest(raw=raw), self.assertRaises(DevFlowError):
                parse_start(raw)

    def test_no_reviewer_keeps_effective_config(self):
        config = self.config()
        self.assertIs(override_reviewers(config, parse_start('request')), config)
        self.assertIs(override_reviewers(config, parse_start('-- request')), config)

    def test_full_replacement_and_final_effective_candidates(self):
        config = self.config()
        config["reviewers"][0]["command"] = ['custom-codex', '--profile', 'trusted']
        config["reviewers"][0]["timeout_seconds"] = 99
        parsed = parse_start('--reviewer claude opus high --reviewer codex new-model high --reviewer claude sonnet low -- request')
        result = override_reviewers(config, parsed)
        self.assertEqual([r['id'] for r in result['reviewers']], ['claude', 'codex', 'claude-2'])
        self.assertEqual(result['reviewers'][1]['command'], config['reviewers'][0]['command'])
        self.assertEqual(result['reviewers'][1]['timeout_seconds'], 99)
        self.assertEqual(result['reviewers'][0]['command'], ['claude'])
        self.assertEqual(result['reviewers'][0]['timeout_seconds'], 1800)
        self.assertEqual(len(config['reviewers']), 2)

    def test_same_kind_candidates_must_have_equal_command_and_timeout(self):
        config = self.config()
        config['reviewers'].append({**copy.deepcopy(config['reviewers'][0]), 'id': 'second'})
        parsed = parse_start('--reviewer codex m high --reviewer claude opus high -- request')
        self.assertEqual(override_reviewers(config, parsed)['reviewers'][0]['command'], ['codex'])
        config['reviewers'][-1]['timeout_seconds'] += 1
        with self.assertRaisesRegex(DevFlowError, 'ambiguous'):
            override_reviewers(config, parsed)

    def test_independence_and_supported_kinds_still_apply(self):
        for options in ('--reviewer claude opus high', '--reviewer claude opus high --reviewer claude sonnet low',
                        '--reviewer unknown m high --reviewer codex m high'):
            with self.subTest(options=options), self.assertRaises(DevFlowError):
                override_reviewers(self.config(), parse_start(options + ' -- request'))

    def test_host_operation_detection_is_explicit_and_retains_tail(self):
        for host, prefix in (('claude', '/'), ('codex', '$')):
            for operation in ('plan', 'implement', 'status', 'resume', 'doctor'):
                self.assertEqual(public_command(prefix+'devflow:'+operation+'\u3000arg  ', host), (operation, 'arg  '))
        self.assertEqual(public_command('説明: $devflow:status', 'codex'), (None, ''))
