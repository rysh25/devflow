"""One declared dialect per source, shared unchanged by reviewer transports."""
import json
from pathlib import Path
import tempfile
import unittest

from devflow_core.common.reviewers import CLIS, prepare_review
from devflow_core.workflows import WORKFLOWS

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DRAFT_07 = "http://json-schema.org/draft-07/schema#"


class ReviewSchemaTests(unittest.TestCase):
    def test_dialect_is_explicit_and_existing_constraints_are_unchanged(self):
        released = json.loads((FIXTURES / "devflow_0_7_3.json").read_text())["files"]
        for name, workflow in WORKFLOWS.items():
            with self.subTest(workflow=name):
                current = json.loads(workflow.resource("review.schema.json").read_text())
                previous = json.loads(released[f"scripts/devflow_core/{name}/review.schema.json"])
                self.assertEqual(current.pop("$schema"), DRAFT_07)
                previous.pop("$schema", None)
                self.assertEqual(current, previous)

    def test_every_transport_uses_the_canonical_schema(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name, workflow in WORKFLOWS.items():
                path = workflow.resource("review.schema.json")
                original = path.read_bytes()
                for kind in CLIS:
                    with self.subTest(workflow=name, reviewer=kind):
                        config = {"id": kind, "kind": kind, "command": [kind], "model": "test-model",
                                  "effort": "high", "timeout_seconds": 60}
                        work = root / name / kind
                        prepared = prepare_review(config, b"synthetic packet", work, path,
                                                  workflow.resource("reviewer.md"), cwd=root, probe={})
                        self.assertEqual(prepared.schema, original)
                        self.assertIn(original.decode("utf-8"), prepared.prompt.decode("utf-8"))
                        if kind == "claude":
                            argument = prepared.argv[prepared.argv.index("--json-schema") + 1]
                            self.assertEqual(json.loads(argument), json.loads(original))
                        elif kind == "codex":
                            argument = prepared.argv[prepared.argv.index("--output-schema") + 1]
                            self.assertEqual(argument, str(work.resolve() / "review.schema.json"))
                        else:
                            self.assertIn(str(work.resolve() / "review-input.md"), prepared.argv)
                        self.assertFalse(work.exists())
