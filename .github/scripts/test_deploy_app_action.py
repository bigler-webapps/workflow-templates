"""Structural regression tests for the immutable-image deploy path.

Run with: python .github/scripts/test_deploy_app_action.py
"""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = (ROOT / ".github/workflows/app-ci.yml").read_text(encoding="utf-8")
DEPLOY_ACTION = (ROOT / ".github/actions/deploy-app/action.yml").read_text(encoding="utf-8")


class CI9StructuralTests(unittest.TestCase):
    def test_ci_uses_github_token_and_preserves_the_empty_tag_guard(self):
        """The publish workflow relies on the run token, never a stored PAT."""
        self.assertIn("GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}", CI_WORKFLOW)
        self.assertIn("printf '%s' \"$GITHUB_TOKEN\" | docker login ghcr.io", CI_WORKFLOW)
        self.assertIn('[ -n "$IMAGE_TAG" ] || { echo "❌ IMAGE_TAG (github.sha) is empty; refusing to publish.";', CI_WORKFLOW)
        self.assertNotIn("GHCR_TOKEN", CI_WORKFLOW)

    def test_published_images_have_an_oci_source_label(self):
        """Both the normal build and CI-6 full-image rebuild are linked to the repo."""
        source_label = "--label org.opencontainers.image.source=https://github.com/${{ github.repository }}"
        self.assertEqual(CI_WORKFLOW.count(source_label), 2)

    def test_deploy_action_keeps_registry_and_build_secrets_out_of_env(self):
        """The generated server .env excludes the run token and pull-only build secrets."""
        self.assertNotIn("GHCR_TOKEN", DEPLOY_ACTION)
        self.assertIn('test("^(SSH_|B2_|RESTIC_|GITHUB_|TS_)")', DEPLOY_ACTION)
        self.assertIn("del(.HRAM_ENGINE_READ_TOKEN, .VITE_APP_MUI_LICENSE_KEY)", DEPLOY_ACTION)

    def test_deploy_action_never_reads_secrets_context_directly(self):
        """A composite action has no `secrets` context -- every credential must
        arrive as a declared input, forwarded by the caller's `with:` block
        (CI-10 regression: an earlier draft referenced `secrets.GITHUB_TOKEN`
        directly inside this composite action, which is invalid GitHub Actions
        syntax and fails the whole action invocation, not just the login step)."""
        self.assertNotIn("${{ secrets.", DEPLOY_ACTION)
        self.assertIn("github_token:", DEPLOY_ACTION)
        self.assertIn("GITHUB_TOKEN: ${{ inputs.github_token }}", DEPLOY_ACTION)

    def test_ci_pushes_the_github_sha_tag_after_pytest(self):
        """app-ci.yml publishes the immutable SHA tag, only after tests pass."""
        self.assertIn("IMAGE_TAG: ${{ github.sha }}", CI_WORKFLOW)
        self.assertIn("IMAGE_TAG: ${{ github.sha }}", DEPLOY_ACTION)
        self.assertIn("printf '\\nIMAGE_TAG=%s\\n' \"$IMAGE_TAG\" >> .env", DEPLOY_ACTION)
        self.assertIn('docker push "$PUBLISHED_IMAGE:$IMAGE_TAG"', CI_WORKFLOW)
        self.assertLess(
            CI_WORKFLOW.index("- name: Run pytest"),
            CI_WORKFLOW.index("- name: Publish backend image to GHCR"),
        )
        self.assertNotIn('docker push "$PUBLISHED_IMAGE:latest"', CI_WORKFLOW)

    def test_default_deploy_pulls_and_fallback_keeps_build(self):
        """deploy-app/action.yml separates pull-based default from --build fallback."""
        self.assertIn("use_build_fallback:", DEPLOY_ACTION)
        self.assertIn("default: 'false'", DEPLOY_ACTION)
        fallback, default = re.search(
            r'if \[ "\\\$\{USE_BUILD_FALLBACK\}" = "true" \]; then(?P<fallback>.*?)'
            r'        else(?P<default>.*?)\n        fi',
            DEPLOY_ACTION,
            re.DOTALL,
        ).group("fallback", "default")
        self.assertIn('compose \\$COMPOSE_FILES --env-file .env up -d --build --force-recreate --remove-orphans', fallback)
        self.assertNotIn('--build', default)
        self.assertIn('compose \\$COMPOSE_FILES --env-file .env pull', default)
        self.assertIn('compose \\$COMPOSE_FILES --env-file .env up -d --force-recreate --remove-orphans', default)

    def test_empty_tag_cannot_fall_back_to_compose_latest(self):
        """deploy-app/action.yml fails before any remote pull/build if SHA is absent."""
        self.assertGreaterEqual(DEPLOY_ACTION.count('set -euo pipefail'), 3)
        self.assertIn('[ -n "$IMAGE_TAG" ] || { echo "❌ IMAGE_TAG (github.sha) is empty;', DEPLOY_ACTION)
        self.assertIn('IMAGE_TAG is empty on target; refusing to use compose\'s latest default.', DEPLOY_ACTION)


if __name__ == "__main__":
    unittest.main()
