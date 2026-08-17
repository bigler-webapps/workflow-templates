"""Tests for the restic-check-local / restic-check-b2 composite actions (INF-23).

Structural checks read the raw YAML text. The behavioural check actually runs
the B2 action's check step as a real bash script with `restic` stubbed on
PATH -- this is the one that would have caught a failure path that silently
exits 0 (INF-17's lesson, cited by the WO itself).

Run: python .github/scripts/test_restic_check_actions.py
"""

import os
import re
import shutil
import stat
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LOCAL_ACTION = (ROOT / ".github/actions/restic-check-local/action.yml").read_text(encoding="utf-8")
B2_ACTION = (ROOT / ".github/actions/restic-check-b2/action.yml").read_text(encoding="utf-8")
BASH = shutil.which("bash")


def _extract_run_block(yaml_text: str, step_name_marker: str) -> str:
    """Pulls the `run: |` block's raw bash out of a single composite-action
    step, identified by a substring of its `name:` line. Good enough for this
    repo's simple one-step-per-name structure (same technique test_janitor.py
    and test_deploy_app_action.py already use for structural assertions)."""
    after_name = yaml_text.split(step_name_marker, 1)[1]
    run_marker = "run: |\n"
    body = after_name.split(run_marker, 1)[1]
    lines = []
    for line in body.splitlines():
        if line.strip() and not line.startswith("        "):
            break
        lines.append(line[8:] if line.startswith("        ") else line)
    return "\n".join(lines)


class RestikCheckStructuralTests(unittest.TestCase):
    def test_neither_action_ever_mutates_the_repository(self):
        # Scoped to the actual steps (`runs:` onward) -- the description
        # block legitimately documents these commands in prose as things the
        # action must NOT do.
        for name, action in (("local", LOCAL_ACTION), ("b2", B2_ACTION)):
            steps_only = action.split("\nruns:", 1)[1]
            for forbidden in ("restic repair", "restic prune", "restic forget", "restic unlock"):
                self.assertNotIn(forbidden, steps_only, f"{name} action must never run '{forbidden}'")

    def test_local_action_reads_all_data_every_run(self):
        self.assertIn("restic check --read-data\n", LOCAL_ACTION)
        self.assertNotIn("read-data-subset", LOCAL_ACTION)

    def test_b2_action_uses_a_rotating_subset_not_a_full_read(self):
        self.assertIn("--read-data-subset=", B2_ACTION)
        self.assertNotIn("restic check --read-data\n", B2_ACTION)

    def test_b2_action_is_not_matrixed_per_target(self):
        # The single-repo action must take no `target` input -- if it did,
        # a caller could accidentally matrix it and triple the egress cost
        # for the exact same shared repository (the mistake this split
        # exists to prevent).
        self.assertNotIn("target:", B2_ACTION.split("outputs:" if "outputs:" in B2_ACTION else "runs:", 1)[0])

    def test_local_action_targets_the_local_repo_path_not_b2(self):
        self.assertIn("restic_repo_local", LOCAL_ACTION)
        self.assertNotIn("b2_key_id", LOCAL_ACTION)
        self.assertNotIn("AWS_ACCESS_KEY_ID", LOCAL_ACTION)


class RestikCheckBehaviouralTests(unittest.TestCase):
    """Reproduces Required Test 2 literally: stub restic to fail, assert the
    check step actually goes non-zero and the finding is visible in output.
    Against a step whose failure path was silently swallowed (e.g. a stray
    `|| true`), this test fails."""

    def setUp(self):
        if not BASH:
            self.skipTest("no bash available on PATH")

    def _run_b2_check_script(self, tmp_path, restic_stub_body, subset_n="3"):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "restic"
        stub.write_text(restic_stub_body, encoding="utf-8", newline="\n")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

        script = _extract_run_block(B2_ACTION, "🌐 B2 repository check")
        script_path = tmp_path / "check_b2.sh"
        script_path.write_text(script, encoding="utf-8", newline="\n")

        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
        env.update(
            RESTIC_REPOSITORY="s3:example/repo",
            RESTIC_PASSWORD="x",
            AWS_ACCESS_KEY_ID="key",
            AWS_SECRET_ACCESS_KEY="secret",
            READ_DATA_SUBSET_N=subset_n,
            READ_DATA_SUBSET_OF="7",
        )
        return subprocess.run(
            [BASH, str(script_path)],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )

    def test_a_damaged_pack_actually_fails_the_check_step(self, tmp_path=None):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            result = self._run_b2_check_script(
                tmp_path,
                "#!/usr/bin/env bash\n"
                "echo 'Pack ID does not match, want 112abe12bf, got 0000000000'\n"
                "exit 1\n",
            )
        self.assertNotEqual(result.returncode, 0, "a damaged pack must fail the step, not exit 0")
        self.assertIn("B2 REPO CHECK FAILED", result.stdout)
        self.assertIn("112abe12bf", result.stdout, "the pack id must reach the workflow output")

    def test_a_healthy_repository_passes(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            result = self._run_b2_check_script(
                tmp_path,
                "#!/usr/bin/env bash\necho 'no errors were found'\nexit 0\n",
            )
        self.assertEqual(result.returncode, 0)
        self.assertIn("B2 repository OK", result.stdout)

    def test_the_configured_subset_is_actually_passed_to_restic(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            result = self._run_b2_check_script(
                tmp_path,
                "#!/usr/bin/env bash\n"
                "for a in \"$@\"; do echo \"ARG:$a\"; done\n"
                "exit 0\n",
                subset_n="5",
            )
        self.assertIn("ARG:--read-data-subset=5/7", result.stdout)


if __name__ == "__main__":
    unittest.main()
