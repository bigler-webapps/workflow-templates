"""Structural checks for reusable workflow job selection."""

import itertools
from pathlib import Path
import re
import unittest


WORKFLOW = (Path(__file__).resolve().parents[1] / "workflows/app-ci.yml").read_text(encoding="utf-8")
INPUTS = WORKFLOW.split("workflow_call:\n    inputs:\n", 1)[1].split("\n    secrets:\n", 1)[0]
VALIDATION = WORKFLOW.split("\n  validate-inputs:\n", 1)[1].split("\n  backend:\n", 1)[0]
FRONTEND = re.split(r"\n  \S", WORKFLOW.split("\n  frontend:\n", 1)[1], maxsplit=1)[0]
JOBS = ("backend", "frontend", "e2e", "security")


class JobSelectionTests(unittest.TestCase):
    def test_frontend_default_and_job_condition(self):
        block = re.split(r"\n      \S", INPUTS.split("      run-frontend:\n", 1)[1], maxsplit=1)[0]
        self.assertRegex(block, r"(?m)^        type: boolean$")
        self.assertRegex(block, r"(?m)^        default: true$")
        self.assertRegex(FRONTEND, r"(?m)^    if: \$\{\{ inputs\.run-frontend \}\}$")

    def test_empty_selection_fails_and_any_enabled_job_passes(self):
        step = VALIDATION.split("      - name: At least one job must be enabled\n", 1)[1].split("\n      - name:", 1)[0]
        condition = re.search(r"if: \$\{\{ (.*?) \}\}", step).group(1)
        self.assertEqual(condition, " && ".join(f"!inputs.run-{job}" for job in JOBS))
        self.assertIn("::error::At least one of run-backend, run-frontend, run-e2e, or run-security must be true.", step)
        self.assertRegex(step, r"(?m)^          exit 1$")
        for values in itertools.product((False, True), repeat=len(JOBS)):
            with self.subTest(values=values):
                # Evaluate the actual condition's conjunction over the selected inputs.
                terms = condition.split(" && ")
                inputs = dict(zip((f"inputs.run-{job}" for job in JOBS), values))
                fails = all(not inputs[term.removeprefix("!")] for term in terms)
                self.assertEqual(fails, not any(values))


if __name__ == "__main__":
    unittest.main()
