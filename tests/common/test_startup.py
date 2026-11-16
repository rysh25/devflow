import copy
from pathlib import Path
import tempfile
import unittest

from devflow_core.common.errors import DevFlowError
from devflow_core.common.operations import public_command
from devflow_core.common.startup import apply_startup, parse_start
from devflow_core.workflows import WORKFLOWS


class StartupTests(unittest.TestCase):
    def config(self):
        with tempfile.TemporaryDirectory() as folder:
            return WORKFLOWS["plan"].load_config(Path(folder).resolve())

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
        self.assertIs(apply_startup(config, parse_start('request')), config)
        self.assertIs(apply_startup(config, parse_start('-- request')), config)

    def test_full_replacement_and_final_effective_candidates(self):
        config = self.config()
        config["reviewers"][0]["command"] = ['custom-codex', '--profile', 'trusted']
        config["reviewers"][0]["timeout_seconds"] = 99
        parsed = parse_start('--reviewer claude opus high --reviewer codex new-model high --reviewer claude sonnet low -- request')
        result = apply_startup(config, parsed)
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
        self.assertEqual(apply_startup(config, parsed)['reviewers'][0]['command'], ['codex'])
        config['reviewers'][-1]['timeout_seconds'] += 1
        with self.assertRaisesRegex(DevFlowError, 'ambiguous'):
            apply_startup(config, parsed)

    def test_supported_kinds_still_apply(self):
        with self.assertRaisesRegex(DevFlowError, 'Unsupported reviewer kind'):
            apply_startup(self.config(), parse_start('--reviewer unknown m high --reviewer codex m high -- request'))
        # One reviewer, or reviewers of a single kind, are accepted.
        one = apply_startup(self.config(), parse_start('--reviewer claude opus high -- request'))
        self.assertEqual([r['id'] for r in one['reviewers']], ['claude'])
        same = apply_startup(self.config(), parse_start('--reviewer claude opus high --reviewer claude sonnet low -- request'))
        self.assertEqual([r['id'] for r in same['reviewers']], ['claude', 'claude-2'])

    def test_max_reviews_option_is_parsed_in_any_order(self):
        for raw in ('--max-reviews 4 -- 依頼  ', '--max-reviews 4 --reviewer codex m high -- 依頼  ',
                    '--reviewer codex m high --max-reviews 4 -- 依頼  '):
            with self.subTest(raw=raw):
                parsed = parse_start(raw)
                self.assertEqual((parsed.max_reviews, parsed.body), (4, '依頼  '))
        self.assertIsNone(parse_start('--reviewer codex m high -- request').max_reviews)
        for value in ('1', '9', '10', '99'):
            self.assertEqual(parse_start(f'--max-reviews {value} -- request').max_reviews, int(value))

    def test_invalid_max_reviews_are_errors(self):
        for raw in ('--max-reviews 0 -- r', '--max-reviews 100 -- r', '--max-reviews 04 -- r', '--max-reviews +4 -- r',
                    '--max-reviews 4.0 -- r', '--max-reviews \uff14 -- r', '--max-reviews -- r', '--max-reviews x -- r',
                    '--max-reviews 2 --max-reviews 3 -- r', '--max-review 4 -- r', '--max 4 -- r'):
            with self.subTest(raw=raw), self.assertRaises(DevFlowError):
                parse_start(raw)

    def test_max_reviews_option_sets_the_limit_and_is_checked_against_the_reviewers(self):
        config = self.config()
        original = copy.deepcopy(config)
        result = apply_startup(config, parse_start('--max-reviews 1 --reviewer codex m high -- request'))
        self.assertEqual((result['max_reviews'], [r['id'] for r in result['reviewers']]), (1, ['codex']))
        self.assertEqual(apply_startup(config, parse_start('--max-reviews 9 -- request'))['max_reviews'], 9)
        self.assertEqual(config, original)
        # The default two reviewers need at least two reviews; three reviewers need three.
        with self.assertRaisesRegex(DevFlowError, 'one reviewer up to max_reviews'):
            apply_startup(config, parse_start('--max-reviews 1 -- request'))
        with self.assertRaisesRegex(DevFlowError, 'one reviewer up to max_reviews'):
            apply_startup(config, parse_start('--max-reviews 2 --reviewer codex a high --reviewer claude b high '
                                              '--reviewer devin c high -- request'))

    def test_host_operation_detection_is_explicit_and_retains_tail(self):
        for host, prefix in (('claude', '/'), ('codex', '$')):
            for operation in ('plan', 'implement', 'status', 'resume', 'doctor'):
                self.assertEqual(public_command(prefix+'devflow:'+operation+'\u3000arg  ', host), (operation, 'arg  '))
        self.assertEqual(public_command('説明: $devflow:status', 'codex'), (None, ''))
