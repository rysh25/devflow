"""The workflow contract: one command table from which the guard and the CLI derive."""
import argparse
import inspect
import unittest

from devflow_core import cli
from devflow_core.common import guard
from devflow_core.common.errors import GuardError
from devflow_core.common.workflow import Command, Option
from devflow_core.workflows import WORKFLOWS


class CommandTableTests(unittest.TestCase):
    def test_an_unknown_option_kind_fails_when_the_table_is_defined(self):
        with self.assertRaisesRegex(ValueError, "Unknown option kind"):
            Option("--file", "drafft")

    def test_a_claude_action_cannot_take_an_option_the_guard_cannot_check(self):
        with self.assertRaisesRegex(ValueError, "cannot check"):
            Command("count", "x", lambda *_: None, (Option("--n", "int"),))
        Command("count", "x", lambda *_: None, (Option("--n", "int"),), claude=False)

    def test_the_guard_refuses_an_unknown_kind(self):
        with self.assertRaisesRegex(GuardError, "cannot be checked"):
            guard.check_option("drafft", "x", None, None)

    def test_guard_actions_and_human_actions_come_from_the_table(self):
        for workflow in WORKFLOWS.values():
            with self.subTest(workflow=workflow.name):
                names = {command.name for command in workflow.commands}
                self.assertEqual(set(workflow.actions) | set(workflow.human_actions), names)
                self.assertIn("recover", workflow.human_actions)
                self.assertNotIn("recover", workflow.actions)
                recover = next(c for c in workflow.commands if c.name == "recover")
                self.assertIn("confirm", [option.kind for option in recover.options])


class StartContractTests(unittest.TestCase):
    def test_every_workflow_builds_a_start_from_the_same_arguments(self):
        for workflow in WORKFLOWS.values():
            with self.subTest(workflow=workflow.name):
                parameters = list(inspect.signature(workflow.build_start).parameters)
                self.assertEqual(parameters, ["project", "pending", "options"])

    def test_start_options_come_only_from_the_declared_extra_options(self):
        parser = cli.parser()
        implement = WORKFLOWS["implement"]
        args = parser.parse_args(["implement", "start", "--plan", "plan.md", "--workspace", "../worktree"])
        self.assertEqual(implement.start_options(args), {"workspace": "../worktree"})
        args = parser.parse_args(["implement", "start", "--plan", "plan.md"])
        self.assertEqual(implement.start_options(args), {"workspace": None})
        # Plan declares no extra start option: nothing is read from args (not even workspace).
        self.assertEqual(WORKFLOWS["plan"].start_options(argparse.Namespace()), {})
        with self.assertRaises(cli.UsageError):
            parser.parse_args(["implement", "start", "--plan", "plan.md", "--unknown", "x"])

    def test_restricting_is_a_declared_property_not_a_name(self):
        self.assertTrue(WORKFLOWS["plan"].restricts_tools)
        self.assertFalse(WORKFLOWS["implement"].restricts_tools)


if __name__ == "__main__":
    unittest.main()
