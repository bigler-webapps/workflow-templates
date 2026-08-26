"""Structural regression test for app-ci's pnpm/action-setup destination isolation (CI-21).

Run with: python .github/scripts/test_pnpm_action_setup_dest.py
"""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = (ROOT / ".github/workflows/app-ci.yml").read_text(encoding="utf-8")

# Job-scoped: `frontend` is the last job in the file, so slicing at its header
# captures exactly its body. Anchoring the search here (not the whole file)
# means a `dest:` on an unrelated step elsewhere in the file can never
# satisfy this check -- the "detached from the pnpm/action-setup step" case
# CI-21's own required-tests section calls out, and the same job-scoping
# idiom `test_security_job_still_does_not_use_the_action` already uses below.
FRONTEND_JOB = CI_WORKFLOW.split("\n  frontend:\n", 1)[1]

STEP_PATTERN = re.compile(
    r"^      - uses: pnpm/action-setup@[0-9a-f]{40}  # v4\.4\.0\n"
    r"        with:\n"
    r"(?P<body>(?:^ {10}\S.*\n)+)",
    re.MULTILINE,
)


def _find_dest(job_text):
    """Return the `dest:` value inside the frontend job's pnpm/action-setup
    step, or None. Callers must pass job-scoped text (see FRONTEND_JOB)."""
    match = STEP_PATTERN.search(job_text)
    if match is None:
        return None
    dest = re.search(r"^ {10}dest:\s*(\S.*)$", match.group("body"), re.MULTILINE)
    return dest.group(1) if dest else None


def _strip_dest_from_real_step(job_text):
    """Return (job_text with the real step's `dest:` line removed, the removed
    line). Mutates the ACTUAL live step text rather than a hand-typed copy, so
    the mutation can't silently drift from what the fixed file really looks
    like."""
    match = STEP_PATTERN.search(job_text)
    assert match is not None, "pnpm/action-setup step not found in frontend job"
    dest_line = re.search(r"^ {10}dest:.*\n", match.group("body"), re.MULTILINE)
    assert dest_line is not None, "fixture setup: no dest line on the live step to strip"
    body_start = match.start("body")
    start = body_start + dest_line.start()
    end = body_start + dest_line.end()
    return job_text[:start] + job_text[end:], dest_line.group(0)


class CI21StructuralTests(unittest.TestCase):
    def test_pnpm_action_setup_has_a_per_job_dest(self):
        """The live workflow's frontend job carries the fix."""
        dest = _find_dest(FRONTEND_JOB)
        self.assertIsNotNone(
            dest,
            "frontend job's pnpm/action-setup step is missing a `dest:` input -- "
            "without one it falls back to the action's shared default (~/setup-pnpm), "
            "which races across concurrent job slots on the self-hosted runner (CI-21)",
        )
        assert dest is not None
        self.assertIn("runner.temp", dest)

    def test_assertion_fails_on_the_pre_fix_step(self):
        """Mutation check (CI-21's 'unfixed app-ci.yml' case): strip the dest
        line out of the REAL live step and confirm the same lookup the test
        above uses rejects it."""
        pre_fix_job, _ = _strip_dest_from_real_step(FRONTEND_JOB)
        self.assertIsNone(_find_dest(pre_fix_job))

    def test_assertion_fails_on_a_dest_detached_from_the_step(self):
        """Mutation check (CI-21's other required case: a `dest:` present in
        the file but detached from the pnpm/action-setup step). Built from the
        pre-fix job text (dest stripped from the real step) plus a decoy step
        that duplicates the step signature and carries the fix -- proves the
        job-scoped lookup finds the real (still-unfixed) step, not the decoy."""
        pre_fix_job, _ = _strip_dest_from_real_step(FRONTEND_JOB)
        decoy = (
            "\n      - uses: pnpm/action-setup@1111111111111111111111111111111111111111  # v4.4.0\n"
            "        with:\n"
            "          version: ${{ inputs.pnpm-version }}\n"
            "          dest: ${{ runner.temp }}/setup-pnpm\n"
        )
        self.assertIsNone(_find_dest(pre_fix_job + decoy))

    def test_assertion_fails_on_a_dest_not_scoped_to_runner_temp(self):
        """Mutation check: a `dest` that keeps the shared default must not pass."""
        pre_fix_job, _ = _strip_dest_from_real_step(FRONTEND_JOB)
        wrong_dest_job = pre_fix_job.replace(
            "          version: ${{ inputs.pnpm-version }}\n",
            "          version: ${{ inputs.pnpm-version }}\n          dest: ~/setup-pnpm\n",
            1,
        )
        dest = _find_dest(wrong_dest_job)
        self.assertIsNotNone(dest)
        assert dest is not None
        self.assertNotIn("runner.temp", dest)

    def test_security_job_still_does_not_use_the_action(self):
        """CI-21's Envelope correction: security provisions pnpm via corepack, not this
        action -- guard against the fix later being (wrongly) duplicated onto a step
        that was never part of the race."""
        security_job = CI_WORKFLOW.split("\n  security:\n", 1)[1].split("\n  frontend:\n", 1)[0]
        self.assertNotIn("pnpm/action-setup", security_job)
        self.assertIn("corepack enable", security_job)


if __name__ == "__main__":
    unittest.main()
