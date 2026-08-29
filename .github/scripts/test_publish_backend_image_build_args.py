"""Structural regression tests for publish-backend-image's survey Vite args.

Run with: python .github/scripts/test_publish_backend_image_build_args.py
"""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
ACTION = (ROOT / ".github/actions/publish-backend-image/action.yml").read_text(encoding="utf-8")

SURVEY_ARGS = (
    ("vite_survey_app_api_base_url", "VITE_SURVEY_APP_API_BASE_URL"),
    ("vite_survey_app_site_slug", "VITE_SURVEY_APP_SITE_SLUG"),
)


def assert_survey_build_args(action_text):
    """Assert the real action exposes and forwards each survey Vite value."""
    for input_name, env_name in SURVEY_ARGS:
        input_declaration = re.compile(
            rf"^  {re.escape(input_name)}:\n"
            r"    description: .*\n"
            r"    required: false\n"
            r"    default: ''\n",
            re.MULTILINE,
        )
        assert input_declaration.search(action_text), (
            f"{input_name} must be an optional input with an empty-string default"
        )
        assert f"        {env_name}: ${{{{ inputs.{input_name} }}}}" in action_text, (
            f"{input_name} must be exposed to docker build as {env_name}"
        )
        assert f'            --build-arg "{env_name}=${env_name}" \\' in action_text, (
            f"docker build must pass {env_name} from its environment"
        )


class PublishBackendImageBuildArgsTests(unittest.TestCase):
    def test_survey_inputs_are_optional_and_empty_by_default(self):
        assert_survey_build_args(ACTION)

    def test_assertion_fails_if_either_survey_build_arg_is_removed(self):
        """Mutation check: both forwarding lines must be load-bearing."""
        for _, env_name in SURVEY_ARGS:
            build_arg_line = f'            --build-arg "{env_name}=${env_name}" ' + "\\\n"
            mutated = ACTION.replace(build_arg_line, "", 1)
            self.assertNotEqual(mutated, ACTION, f"fixture setup: {env_name} line was not found")
            with self.assertRaises(AssertionError):
                assert_survey_build_args(mutated)


if __name__ == "__main__":
    unittest.main()
