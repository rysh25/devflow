"""The workflow contract: one command table from which the CLI parser and dispatch derive."""
import argparse
import inspect
import unittest

from devflow_core import cli
from devflow_core.common.errors import DevFlowError
from devflow_core.common.workflow import OPTION_KINDS, Option
from devflow_core.workflows import WORKFLOWS


def subcommands(parser: argparse.ArgumentParser) -> dict:
    action = next(item for item in parser._actions if isinstance(item, argparse._SubParsersAction))
    return action.choices


class CommandTableTests(unittest.TestCase):
    def test_an_unknown_option_kind_fails_when_the_table_is_defined(self):
        with self.assertRaisesRegex(ValueError, "Unknown option kind"):
            Option("--file", "drafft")
        for kind in OPTION_KINDS:
            with self.subTest(kind=kind):
                Option("--x", kind)

    def test_the_cli_offers_exactly_the_actions_of_the_table(self):
        workflows = subcommands(cli.parser())
        for workflow in WORKFLOWS.values():
            with self.subTest(workflow=workflow.name):
                names = {command.name for command in workflow.commands}
                if workflow.start_option:
                    names.add("start")
                self.assertEqual(set(subcommands(workflows[workflow.name])), names)
                recover = next(c for c in workflow.commands if c.name == "recover")
                self.assertIn("confirm", [option.kind for option in recover.options])

    def test_a_decision_option_takes_every_answer_including_accept(self):
        implement = subcommands(subcommands(cli.parser())["implement"])["resolve"]
        decision = next(action for action in implement._actions if action.dest == "decision")
        self.assertEqual(set(decision.choices), {"continue", "cancel", "stop", "accept"})


class OwnerTests(unittest.TestCase):
    def test_every_change_to_a_run_needs_the_owning_session_except_a_humans_recover(self):
        for name, workflow in WORKFLOWS.items():
            for command in workflow.commands:
                with self.subTest(flow=name, action=command.name):
                    self.assertEqual(command.needs_owner, command.name not in ("status", "doctor", "recover"))
                    if command.name in ("status", "doctor", "recover"):
                        continue
                    # Without --session, a change never runs (the owner is checked before the action).
                    args = argparse.Namespace(action=command.name)
                    with self.assertRaisesRegex(DevFlowError, "pass --session"):
                        workflow.run_command(args, None, "20260101T000000Z-000000000000", None)


class StartContractTests(unittest.TestCase):
    def test_every_workflow_builds_a_start_from_the_same_arguments(self):
        for workflow in WORKFLOWS.values():
            with self.subTest(workflow=workflow.name):
                parameters = list(inspect.signature(workflow.build_start).parameters)
                self.assertEqual(parameters, ["project", "pending", "options", "scratch"])

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


if __name__ == "__main__":
    unittest.main()
