"""Structural regression tests for app-ci's opt-in security gates (WFT-CI-23).

Run with: python .github/scripts/test_app_ci_security_gates.py

These are structural checks against the live workflow text, plus one live
bandit run proving the gate can actually fail (not just parse). The
unchanged-caller proof itself (a caller setting none of these inputs sees an
identical `security` job) is verified live via a `ci-test/<ID>` dispatch per
the WO's own test procedure -- a reusable workflow's runtime behaviour cannot
be reproduced locally.
"""

from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = (ROOT / ".github/workflows/app-ci.yml").read_text(encoding="utf-8")

# Job-scoped, same idiom as test_pnpm_action_setup_dest.py: `security` sits
# between `backend` and `frontend`, so slicing between those two headers
# captures exactly its body and nothing from a neighbouring job.
SECURITY_JOB = CI_WORKFLOW.split("\n  security:\n", 1)[1].split("\n  frontend:\n", 1)[0]

# The exact report-only pip-audit invocation that ran before this WO -- any
# drift here means the "unchanged caller" default no longer matches history.
PRE_WO_PIP_AUDIT_LINE = (
    'pip-audit -r backend/requirements.txt || echo "::warning::pip-audit findings (non-blocking v1)"'
)


class WftCi23StructuralTests(unittest.TestCase):
    def test_pip_audit_non_blocking_branch_is_byte_identical_to_pre_wo_script(self):
        """pip-audit-blocking defaults false; that branch must be the exact
        script that ran before this WO, not a rewrite that happens to behave
        the same today."""
        self.assertIn(PRE_WO_PIP_AUDIT_LINE, SECURITY_JOB)

    def test_pip_audit_blocking_branch_does_not_swallow_the_exit_code(self):
        """The true branch must let pip-audit's own exit code reach the step
        -- no `||` fallback inside the script, and `continue-on-error` must be
        gated off for this case (reviewer R1 / sec_reviewer S1: it must NOT
        stay unconditionally true, or a real finding still shows green)."""
        step = re.search(
            r"^      - name: pip-audit \(report-only\)\n(?P<body>(?:^ {8}.*\n|\n)+?)(?=^      - name:)",
            SECURITY_JOB,
            re.MULTILINE,
        )
        self.assertIsNotNone(step)
        assert step is not None
        body = step.group("body")
        continue_on_error = re.search(r"^        continue-on-error:\s*(?P<value>\S.*)$", body, re.MULTILINE)
        self.assertIsNotNone(continue_on_error, "pip-audit step must still carry a continue-on-error directive")
        assert continue_on_error is not None
        self.assertEqual(
            continue_on_error.group("value"),
            "${{ !inputs.pip-audit-blocking }}",
            "must be gated on pip-audit-blocking, not left unconditionally true "
            "(R1/S1: an unconditional true would mask a real blocking finding too)",
        )
        blocking_branch = re.search(
            r'pip-audit-blocking\s*}}"\s*=\s*"true"\s*\]; then\n(?P<line>.*)\n\s*else\n',
            body,
        )
        self.assertIsNotNone(blocking_branch)
        assert blocking_branch is not None
        self.assertNotIn("||", blocking_branch.group("line"))
        self.assertIn("pip-audit -r backend/requirements.txt", blocking_branch.group("line"))

    def test_assertion_fails_if_blocking_branch_reintroduces_the_swallow(self):
        """Mutation check: reinstate the `||` on the live blocking branch and
        confirm the check above would have caught it."""
        mutated = SECURITY_JOB.replace(
            "              pip-audit -r backend/requirements.txt\n            else",
            '              pip-audit -r backend/requirements.txt || echo "::warning::pip-audit findings (non-blocking v1)"\n            else',
            1,
        )
        self.assertNotEqual(mutated, SECURITY_JOB, "fixture setup: replacement did not match live text")
        blocking_branch = re.search(
            r'pip-audit-blocking\s*}}"\s*=\s*"true"\s*\]; then\n(?P<line>.*)\n\s*else\n',
            mutated,
        )
        self.assertIsNotNone(blocking_branch)
        assert blocking_branch is not None
        self.assertIn("||", blocking_branch.group("line"))

    def test_assertion_fails_if_continue_on_error_is_left_unconditional(self):
        """Mutation check (R1/S1): revert continue-on-error to the pre-fix
        unconditional `true` and confirm the check above would reject it."""
        mutated = SECURITY_JOB.replace(
            "        continue-on-error: ${{ !inputs.pip-audit-blocking }}",
            "        continue-on-error: true",
            1,
        )
        self.assertNotEqual(mutated, SECURITY_JOB, "fixture setup: replacement did not match live text")
        step = re.search(
            r"^      - name: pip-audit \(report-only\)\n(?P<body>(?:^ {8}.*\n|\n)+?)(?=^      - name:)",
            mutated,
            re.MULTILINE,
        )
        self.assertIsNotNone(step)
        assert step is not None
        continue_on_error = re.search(
            r"^        continue-on-error:\s*(?P<value>\S.*)$", step.group("body"), re.MULTILINE
        )
        self.assertIsNotNone(continue_on_error)
        assert continue_on_error is not None
        self.assertNotEqual(continue_on_error.group("value"), "${{ !inputs.pip-audit-blocking }}")

    def test_string_input_defaults_match_spec(self):
        """Reviewer R3: a silent default drift on the string inputs wouldn't
        be caught by the boolean-only check below."""
        inputs_block = CI_WORKFLOW.split("workflow_call:\n    inputs:\n", 1)[1]
        for name, expected_default in (
            ("bandit-path", "'backend'"),
            ("bandit-config", "''"),
            ("ruff-path", "'backend'"),
            ("security-python-version", "'3.12'"),
        ):
            match = re.search(
                rf"      {re.escape(name)}:\n(?:.*\n)*?        default: (?P<default>\S+)\n",
                inputs_block,
            )
            self.assertIsNotNone(match, f"{name} not found in workflow_call.inputs")
            assert match is not None
            self.assertEqual(match.group("default"), expected_default, f"{name} default drifted")

    def test_new_boolean_inputs_are_declared_as_booleans(self):
        """Reviewer R3: catch a boolean input silently declared as a string,
        which would make GitHub Actions treat it as truthy on any non-empty
        caller value instead of the intended true/false toggle."""
        inputs_block = CI_WORKFLOW.split("workflow_call:\n    inputs:\n", 1)[1]
        for name in ("pip-audit-blocking", "run-bandit", "run-ruff"):
            match = re.search(
                rf"      {name}:\n(?:.*\n)*?        type: (?P<type>\S+)\n",
                inputs_block,
            )
            self.assertIsNotNone(match, f"{name} not found in workflow_call.inputs")
            assert match is not None
            self.assertEqual(match.group("type"), "boolean", f"{name} must be type: boolean")

    def test_bandit_and_ruff_steps_are_gated_off_by_default(self):
        for step_name, input_name in (
            ("Static analysis (bandit)", "run-bandit"),
            ("Ruff", "run-ruff"),
        ):
            step = re.search(
                rf"^      - name: {re.escape(step_name)}\n        if: \$\{{\{{ inputs\.{input_name} \}}\}}\n",
                SECURITY_JOB,
                re.MULTILINE,
            )
            self.assertIsNotNone(step, f"{step_name} must be gated on inputs.{input_name}")

    def test_setup_python_runs_before_pip_audit_and_is_not_gated(self):
        """WM-TAKE-8 reversed WFT-CI-23's gating, deliberately.

        Gated on run-bandit/run-ruff and placed BELOW pip-audit, the setup step
        left pip-audit resolving a bare `pip` against the runner's ambient
        Python. The self-hosted netcup runners have none: the step exited 127,
        and every caller on the default `pip-audit-blocking: false` swallowed it
        through continue-on-error, so the dependency audit reported green while
        doing nothing. Both halves are the contract -- unconditional AND ahead
        of pip-audit -- because either alone still leaves the audit depending on
        something the runner may not have.
        """
        setup_name = "- name: Set up Python for the security tooling"
        setup = SECURITY_JOB.index(setup_name)
        audit = SECURITY_JOB.index("- name: pip-audit (report-only)")
        self.assertLess(setup, audit, "setup-python must precede pip-audit")

        block = SECURITY_JOB[setup:audit]
        self.assertIn("uses: actions/setup-python@", block)
        gates = [ln for ln in block.splitlines() if ln.strip().startswith("if:")]
        self.assertEqual(gates, [], "setup-python must carry no `if:` gate")

    def test_pip_audit_never_invokes_a_bare_pip(self):
        """The 127 itself: `pip install pip-audit` with no pip on PATH.

        Checked over the whole security job, not just the one step, so a bare
        `pip` reintroduced anywhere in it fails here too.
        """
        self.assertEqual(
            self._bare_pip_lines(SECURITY_JOB),
            [],
            "use `python -m pip install`, never a bare `pip` -- it is not on "
            "PATH on the self-hosted runners",
        )
        self.assertIn("python -m pip install pip-audit", SECURITY_JOB)

    def test_assertion_fails_if_a_bare_pip_is_reintroduced(self):
        """Mutation test: prove the guard above actually fires."""
        mutated = SECURITY_JOB.replace(
            "python -m pip install pip-audit", "pip install pip-audit", 1
        )
        self.assertNotEqual(
            mutated, SECURITY_JOB, "fixture setup: replacement did not match live text"
        )
        self.assertNotEqual(
            self._bare_pip_lines(mutated), [], "guard would not have caught the 127"
        )

    @staticmethod
    def _bare_pip_lines(job_text):
        return [
            ln.strip()
            for ln in job_text.splitlines()
            if ln.strip().startswith("pip install")
        ]

    def test_bandit_and_ruff_versions_are_pinned(self):
        self.assertIn("bandit==1.9.4", SECURITY_JOB)
        self.assertIn("ruff==0.15.20", SECURITY_JOB)

    def test_new_inputs_default_to_current_behaviour(self):
        inputs_block = CI_WORKFLOW.split("workflow_call:\n    inputs:\n", 1)[1]
        for name in (
            "pip-audit-blocking",
            "run-bandit",
            "run-ruff",
        ):
            match = re.search(
                rf"      {name}:\n(?:.*\n)*?        default: (?P<default>\S+)\n",
                inputs_block,
            )
            self.assertIsNotNone(match, f"{name} not found in workflow_call.inputs")
            assert match is not None
            self.assertEqual(match.group("default"), "false", f"{name} must default to false")

    def test_no_expression_syntax_in_workflow_call_input_descriptions(self):
        """Postmortem guard for d8fc8e6: WFT-CI-23 itself wrote a literal,
        empty `${{ }}` into two `description:` fields on `bandit-path` and
        `ruff-path` as prose explaining GitHub-expression interpolation.
        GitHub Actions parses `${{ }}` wherever it appears in the file,
        description text included, and an empty expression is a parse error
        -- this made app-ci.yml unresolvable for all nineteen `@main` callers
        for ~2h, caught during WM-TAKE-8 and fixed same-day. Scoped to the
        whole `workflow_call.inputs` block (not just the two fixed fields) so
        it also guards every future input description, not just this one."""
        inputs_block = CI_WORKFLOW.split("workflow_call:\n    inputs:\n", 1)[1].split(
            "\n    secrets:\n", 1
        )[0]
        self.assertNotIn(
            "${{",
            inputs_block,
            "a workflow_call input description contains a literal '${{' -- "
            "GitHub Actions evaluates expressions in description text too, and "
            "an empty/malformed one breaks workflow resolution for every caller",
        )

    def test_assertion_fails_if_a_description_reintroduces_expression_syntax(self):
        """Mutation check: reinstate the exact pre-fix text (d8fc8e6's diff)
        and confirm the guard above would have caught it."""
        mutated = CI_WORKFLOW.replace(
            "Reviewer R2: interpolated as a GitHub expression directly into a\n"
            "          shell command, same pre-existing pattern as inputs.frontend-path\n"
            "          elsewhere in this file",
            "Reviewer R2: interpolated via ${{ }} directly into a shell command,\n"
            "          same pre-existing pattern as inputs.frontend-path elsewhere in this\n"
            "          file",
            1,
        )
        self.assertNotEqual(mutated, CI_WORKFLOW, "fixture setup: replacement did not match live text")
        mutated_inputs_block = mutated.split("workflow_call:\n    inputs:\n", 1)[1].split(
            "\n    secrets:\n", 1
        )[0]
        self.assertIn("${{", mutated_inputs_block)

    def test_pnpm_action_setup_dest_fix_is_untouched(self):
        """This WO edits the same job neighbourhood as CI-21 (both live in
        app-ci.yml). Guard that CI-21's fix wasn't accidentally reverted
        while editing around it."""
        frontend_job = CI_WORKFLOW.split("\n  frontend:\n", 1)[1]
        self.assertIn("runner.temp }}/setup-pnpm", frontend_job)


class WftCi23BanditLiveMutationTest(unittest.TestCase):
    """Bandit is installed locally (1.9.4, matching the pin) -- prove the
    gate is a real gate, not decoration, by running it against a
    deliberately unsafe fixture and a clean one."""

    def _run_bandit(self, source):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "sample.py"
            target.write_text(source, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-m", "bandit", "-q", "-r", str(target)],
                capture_output=True,
                text=True,
            )
            return result.returncode

    def test_bandit_fails_on_a_known_unsafe_pattern(self):
        unsafe = "import subprocess\nsubprocess.call('ls ' + input(), shell=True)\n"
        self.assertNotEqual(self._run_bandit(unsafe), 0)

    def test_bandit_passes_on_a_clean_file(self):
        clean = "def add(a, b):\n    return a + b\n"
        self.assertEqual(self._run_bandit(clean), 0)


if __name__ == "__main__":
    unittest.main()
