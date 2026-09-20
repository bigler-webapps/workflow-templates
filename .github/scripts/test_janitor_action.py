"""Structural regression test for the janitor composite action's rsync scope.

Run with: python .github/scripts/test_janitor_action.py
"""

import shutil
import subprocess
from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
ACTION_PATH = ROOT / ".github/actions/janitor/action.yml"
JANITOR_ACTION = ACTION_PATH.read_text(encoding="utf-8")
BASH = shutil.which("bash")


def _validate_inputs_script() -> str:
    """The exact `run:` shell block of the 'Validate inputs' step, parsed
    from the real YAML rather than sliced by string offset -- so this stays
    correct if the file is reordered."""
    doc = yaml.safe_load(JANITOR_ACTION)
    for step in doc["runs"]["steps"]:
        if step.get("name") == "🔎 Validate inputs":
            return step["run"]
    raise AssertionError("'Validate inputs' step not found in janitor/action.yml")


def _run_validation(image_retention: str) -> subprocess.CompletedProcess:
    """Executes the real validation script under bash with a benign, valid
    value for every OTHER input (so only image_retention varies) and returns
    the completed process. Behavioral, not textual -- this is what actually
    catches a broken regex, unlike asserting the pattern string is present."""
    import os

    env = {
        "PATH": os.environ.get("PATH", ""),
        "INPUT_TARGET": "staging",
        "INPUT_SSH_USER": "deploy",
        "INPUT_SSH_HOST": "staging.tail990d7f.ts.net",
        "INPUT_TS_OAUTH_CLIENT_ID": "x",
        "INPUT_TS_OAUTH_SECRET": "x",
        "INPUT_IMAGE_RETENTION": image_retention,
    }
    return subprocess.run(
        [BASH, "-c", _validate_inputs_script()],
        env=env,
        capture_output=True,
        encoding="utf-8",
    )


class INF42JanitorActionSyncScopeTests(unittest.TestCase):
    def test_janitor_action_excludes_runner_janitor_from_the_app_server_sync(self):
        """INF-42 review finding: the janitor action rsyncs the WHOLE
        webapp-ops-scripts checkout to every janitor-role app server
        (main-prod, contact-prod, innoservice-prod, staging). Without an
        explicit exclude, runner_janitor.sh -- which prunes volumes, safe
        only on the CI runner -- would be deployed onto production disks
        even though no workflow there executes it. "Nothing calls it" is
        not the same as "it isn't there"; this pins the exclude itself."""
        self.assertIn("rsync", JANITOR_ACTION)
        self.assertIn("--exclude='runner_janitor.sh'", JANITOR_ACTION)


class ImageRetentionPassthroughTests(unittest.TestCase):
    """WM-INF-72-adjacent (staging disk pressure, 2026-09-20): staging's
    janitor kept the same JANITOR_IMAGE_RETENTION=3 default as every prod
    host, with no way to tune it per host. These pin the passthrough that
    lets a caller override it, and the validation gate in front of it --
    this value is interpolated into a remote SSH command string, so an
    unvalidated value reaching that line would be a command-injection
    surface, not just a bad number."""

    def test_image_retention_input_is_declared(self):
        self.assertIn("image_retention:", JANITOR_ACTION)

    def test_image_retention_defaults_to_empty_not_a_number(self):
        # Empty means "defer to janitor.sh's own default" -- backwards
        # compatible for every caller that doesn't set this.
        idx = JANITOR_ACTION.index("image_retention:")
        block = JANITOR_ACTION[idx : idx + 400]
        self.assertIn("default: ''", block)

    def test_image_retention_is_validated_before_use(self):
        # Must be empty or digits-only (no leading zero). Removing this gate
        # is what turns the input into an injection surface at the ssh step
        # below.
        self.assertIn("INPUT_IMAGE_RETENTION", JANITOR_ACTION)
        self.assertIn("[[ \"$INPUT_IMAGE_RETENTION\" =~ ^([1-9][0-9]*)?$ ]]", JANITOR_ACTION)

    def test_validation_accepts_empty_and_valid_integers(self):
        # Behavioral, not textual: runs the REAL validation script under
        # bash. Empty is the default every non-staging host sends.
        for value in ("", "1", "2", "30"):
            result = _run_validation(value)
            self.assertEqual(
                result.returncode, 0,
                f"image_retention={value!r} should pass, got: {result.stdout}",
            )

    def test_validation_rejects_zero_negative_leading_zero_and_non_numeric(self):
        for value in ("0", "-1", "02", "abc", "3.5"):
            result = _run_validation(value)
            self.assertNotEqual(
                result.returncode, 0,
                f"image_retention={value!r} should be rejected but passed",
            )

    def test_validation_rejects_a_multiline_value_even_when_the_first_line_is_numeric(self):
        # Real bug, caught in review before this ever shipped: `printf '%s'
        # "$v" | grep -qE '^[0-9]*$'` anchors PER LINE, not to the whole
        # buffer, so a value like "2\nrm -rf /" -- headed by a numeric line
        # -- would clear that gate and then be interpolated straight into
        # the remote SSH command. `[[ =~ ]]` anchors to the whole string and
        # has no such gap; this pins that it stays that way.
        result = _run_validation("2\nrm -rf /")
        self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_validation_passes_before_the_env_is_exhausted(self):
        # Sanity check on the harness itself: a run with every input valid
        # reaches the final echo, so a failure above is really about
        # image_retention and not a broken test fixture.
        result = _run_validation("2")
        self.assertIn("Inputs validated", result.stdout)

    def test_image_retention_reaches_janitor_sh_as_the_env_var_it_reads(self):
        self.assertIn("JANITOR_IMAGE_RETENTION='${IMAGE_RETENTION}'", JANITOR_ACTION)

    def test_validation_step_runs_before_the_ssh_step(self):
        # A value that reaches the ssh command unvalidated is the actual
        # risk here -- order matters, not just presence of both pieces.
        validate_idx = JANITOR_ACTION.index("🔎 Validate inputs")
        ssh_idx = JANITOR_ACTION.index("JANITOR_IMAGE_RETENTION='${IMAGE_RETENTION}'")
        self.assertLess(validate_idx, ssh_idx)


if __name__ == "__main__":
    unittest.main()
