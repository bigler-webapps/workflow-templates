"""Structural regression tests for the backup/verify decoupling (INF-24, INF-25).

Run with: python .github/scripts/test_backup_action.py
"""

from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
BACKUP_ACTION = (ROOT / ".github/actions/backup/action.yml").read_text(encoding="utf-8")


class INF24StructuralTests(unittest.TestCase):
    def test_verify_gates_on_snapshot_created_not_run_backup_outcome(self):
        """The defect: verification used to skip whenever ANY part of
        backup.py failed, including a retention-only failure after a real
        snapshot was already created. It must instead follow the per-run
        signal backup.py now emits."""
        self.assertIn("if: steps.run_backup.outputs.snapshot_created == 'true'", BACKUP_ACTION)
        # The old, too-broad condition must be gone from the verify step's
        # own `if:` line specifically (explanatory comments above it may
        # still mention the old form in prose).
        verify_step = BACKUP_ACTION.split("name: 🔍 Verify backup", 1)[1].split("name: 🔄", 1)[0]
        if_line = next(ln for ln in verify_step.splitlines() if ln.strip().startswith("if:"))
        self.assertNotIn("steps.run_backup.outcome", if_line)

    def test_run_backup_step_captures_output_and_sets_the_new_output(self):
        """The step must observe backup.py's own stdout (not just its exit
        code) to know whether a B2 snapshot was created this run."""
        run_backup_step = BACKUP_ACTION.split("name: 💾 Run backup", 1)[1].split("name: 🔍 Verify backup", 1)[0]
        self.assertIn('echo "snapshot_created=true" >> "$GITHUB_OUTPUT"', run_backup_step)
        self.assertIn('echo "snapshot_created=false" >> "$GITHUB_OUTPUT"', run_backup_step)
        # The step's own pass/fail semantics (continue-on-error catches this)
        # must still reflect backup.py's real exit code, not always succeed
        # just because the output-capture logic ran.
        self.assertIn('exit "$RC"', run_backup_step)
        self.assertIn('RC=${PIPESTATUS[0]}', run_backup_step)

    def test_marker_grep_is_not_anchored_to_line_start(self):
        """INF-25: backup.py's log() helper prepends "[HH:MM:SS] " to every
        line it prints, including the B2_SNAPSHOT_CREATED marker. A `^`-
        anchored grep can never match a timestamp-prefixed line -- this is
        exactly what made INF-24's fix a no-op the first time it ran for
        real (verify silently stopped running, everywhere, while the job
        still reported success). Assert the actual detection command
        against a realistic log() line, not just the source text, so a
        future edit can't reintroduce an anchor and still pass a purely
        structural check."""
        run_backup_step = BACKUP_ACTION.split("name: 💾 Run backup", 1)[1].split("name: 🔍 Verify backup", 1)[0]
        grep_line = next(ln for ln in run_backup_step.splitlines() if "grep -q" in ln)
        match = re.search(r"grep -q '([^']*)'", grep_line)
        self.assertIsNotNone(match, "could not find the grep -q '...' pattern in the run_backup step")
        pattern = match.group(1)
        # backup.py's log() helper prepends "[HH:MM:SS] " to every line,
        # including this marker -- that is the realistic shape, not a bare
        # "B2_SNAPSHOT_CREATED=..." line at column 0.
        realistic_line = "[15:07:33] B2_SNAPSHOT_CREATED=6ed9983f"
        result = subprocess.run(
            ["grep", "-q", pattern],
            input=realistic_line + "\n",
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode, 0,
            f"pattern {pattern!r} did not match a realistic log()-prefixed line: {realistic_line!r}",
        )
        # Negative case: an overly broad fix (empty pattern, `.*`, a bare
        # `B2_` prefix) would also satisfy the positive assertion above
        # while reintroducing false positives on unrelated log() lines.
        unrelated_line = "[15:06:28] Local: backup OK (snapshot=aa8492ac)"
        negative_result = subprocess.run(
            ["grep", "-q", pattern],
            input=unrelated_line + "\n",
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(
            negative_result.returncode, 0,
            f"pattern {pattern!r} incorrectly matched an unrelated log() line: {unrelated_line!r}",
        )

    def test_run_backup_step_does_not_abort_before_capturing_output(self):
        """`set -e` on the step's own shell would abort at the ssh command's
        non-zero exit, before the script ever reaches the grep/output logic
        below it -- the capture would never run on exactly the failure path
        this WO cares about."""
        run_backup_step = BACKUP_ACTION.split("name: 💾 Run backup", 1)[1].split("name: 🔍 Verify backup", 1)[0]
        set_line = next(ln for ln in run_backup_step.splitlines() if ln.strip().startswith("set -"))
        self.assertEqual(set_line.strip(), "set -uo pipefail")

    def test_run_backup_step_keeps_the_log_streaming_live(self):
        """A plain OUTPUT="$(ssh ...)" command substitution buffers the
        entire multi-minute SSH session before anything reaches the Actions
        log -- `tee` must be used so the log stays live while output is still
        captured for the marker grep."""
        run_backup_step = BACKUP_ACTION.split("name: 💾 Run backup", 1)[1].split("name: 🔍 Verify backup", 1)[0]
        self.assertIn('| tee "$OUTPUT_FILE"', run_backup_step)
        # Only the actual ssh invocation line matters here -- explanatory
        # comments above it may legitimately show the rejected form in prose.
        ssh_line = next(ln for ln in run_backup_step.splitlines() if ln.strip().startswith("ssh -o"))
        self.assertNotIn('OUTPUT="$(', ssh_line)

    def test_retention_failure_still_fails_the_run_overall(self):
        """Decoupling verify from run_backup's outcome must not make a
        retention failure invisible -- the final fail-gate still reads
        run_backup's real outcome, unchanged by this WO."""
        fail_gate = BACKUP_ACTION.split("name: 🚨 Fail target at end", 1)[1]
        self.assertIn('[ "$RUN_BACKUP_OUTCOME" = "failure" ]', fail_gate)

    def test_summary_distinguishes_snapshot_created_from_verify_outcome(self):
        """'Snapshot created, verified, retention failed' and 'snapshot never
        created' must not summarise to the same red -- both can show
        RUN_BACKUP_OUTCOME=failure, so the summary needs a field that tells
        them apart."""
        summary_step = BACKUP_ACTION.split("name: 🧾 Summarize target result", 1)[1].split(
            "name: 🚨 Fail target at end", 1
        )[0]
        self.assertIn("SNAPSHOT_CREATED: ${{ steps.run_backup.outputs.snapshot_created }}", summary_step)
        self.assertIn('B2 Snapshot Created', summary_step)


if __name__ == "__main__":
    unittest.main()
