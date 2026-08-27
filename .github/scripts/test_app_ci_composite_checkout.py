"""Structural regression tests for app-ci's backend job (WFT-CI-25).

Run with: python .github/scripts/test_app_ci_composite_checkout.py

CI-13's original subject -- a cross-repo checkout of workflow-templates so
the `backend` job could resolve `publish-backend-image` as a local composite
action -- no longer exists. WFT-CI-25 removed the publish step itself
(CI-11 had already moved the real publish to `main.yml`; this half was dead
weight, not a second implementation). This file now guards the opposite
invariant: that removal stays removed, and that the frontend job's own
unrelated `.wt-checkout` (for the i18n duplicate-key script) is untouched.
"""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = (ROOT / ".github/workflows/app-ci.yml").read_text(encoding="utf-8")

BACKEND_JOB = CI_WORKFLOW.split("\n  backend:\n", 1)[1].split("\n  security:\n", 1)[0]
FRONTEND_JOB = CI_WORKFLOW.split("\n  frontend:\n", 1)[1]


class WftCi25StructuralTests(unittest.TestCase):
    def test_backend_job_does_not_publish(self):
        """The publish step, its id, and the cross-repo checkout that only
        existed to resolve it are all gone from `backend`."""
        self.assertNotIn("Publish backend image to GHCR", BACKEND_JOB)
        self.assertNotIn("publish_backend_image", BACKEND_JOB)
        self.assertNotIn("Checkout workflow-templates (for composite actions)", BACKEND_JOB)
        self.assertNotIn(".wt-checkout", BACKEND_JOB)
        self.assertNotIn("publish-backend-image", BACKEND_JOB)

    def test_assertion_fails_if_the_publish_step_is_reintroduced(self):
        """Mutation check: paste the real pre-WFT-CI-25 publish step back in
        (from git history) and confirm the guard above rejects it."""
        reintroduced = BACKEND_JOB + (
            "\n      - name: Publish backend image to GHCR\n"
            "        id: publish_backend_image\n"
            "        uses: ./.wt-checkout/.github/actions/publish-backend-image\n"
        )
        self.assertNotEqual(reintroduced, BACKEND_JOB, "fixture setup: append did not change the job text")
        self.assertIn("Publish backend image to GHCR", reintroduced)

    def test_remove_ci_image_step_no_longer_references_a_published_image(self):
        """The rework (not a deletion) of `Remove CI image`: it must still
        remove $CI_IMAGE (the runner-disk leak this step exists to prevent,
        WM-TAKE-9) but must no longer reference a published-image output that
        no longer exists."""
        step = re.search(
            r"^      - name: Remove CI image\n(?P<body>(?:^ {8}.*\n|\n)+)",
            BACKEND_JOB,
            re.MULTILINE,
        )
        self.assertIsNotNone(step)
        assert step is not None
        body = step.group("body")
        self.assertIn("docker image rm -f \"$CI_IMAGE\"", body)
        self.assertNotIn("PUBLISHED_IMAGE", body)
        self.assertNotIn("publish_backend_image", body)

    def test_resolve_sha_step_is_gone_with_its_only_consumer(self):
        """`resolve_sha` existed solely to feed the publish step's
        `image_tag:` input -- with that gone, an orphaned step would be dead
        code left behind by an incomplete removal."""
        self.assertNotIn("Resolve checked-out SHA", BACKEND_JOB)
        self.assertNotIn("resolve_sha", BACKEND_JOB)

    def test_frontend_jobs_own_wt_checkout_is_untouched(self):
        """A different job, a different reason (the i18n duplicate-key
        script) -- WFT-CI-25 must not have collateral-removed this one."""
        self.assertIn("Checkout workflow-templates (for i18n check)", FRONTEND_JOB)
        self.assertIn(".wt-checkout", FRONTEND_JOB)
        self.assertIn("check_i18n_duplicate_keys.py", FRONTEND_JOB)


if __name__ == "__main__":
    unittest.main()
