"""Regression tests for host-scoped latest snapshot resolution (INF-31).

Run with: python .github/scripts/test_restore_source_export.py
"""

from pathlib import Path
import re
import shlex
import unittest


ROOT = Path(__file__).resolve().parents[2]
ACTION_TEXT = (ROOT / ".github/actions/restore-source-export/action.yml").read_text(encoding="utf-8")
RESOLVE_STEP = ACTION_TEXT.split("id: resolve", 1)[1].split("    - name:", 1)[0]


def _snapshot_resolution_script() -> str:
    match = re.search(
        r'^        if \[ "\$SNAPSHOT_INPUT" = "latest" \]; then\n.*?^        fi\n',
        RESOLVE_STEP,
        flags=re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError("could not find the snapshot-resolution conditional")
    return match.group(0)


def _restic_snapshot_args(snapshot_input: str, source_target: str) -> list[str]:
    """Evaluate which restic argv the Bash conditional selects."""
    conditional = _snapshot_resolution_script()
    latest_branch, explicit_branch = conditional.split("else", 1)
    branch = latest_branch if snapshot_input == "latest" else explicit_branch
    match = re.search(r"restic snapshots .*?(?=\s+\|\s+jq)", branch)
    if match is None:
        raise AssertionError("could not find the restic snapshots invocation")
    values = {
        "$SNAPSHOT_INPUT": snapshot_input,
        "$SOURCE_TARGET": source_target,
    }
    return [values.get(arg, arg) for arg in shlex.split(match.group(0))]


class RestoreSourceExportSnapshotTests(unittest.TestCase):
    def test_latest_uses_source_target_as_restic_host_filter(self):
        self.assertIn("SOURCE_TARGET: ${{ inputs.source_target }}", RESOLVE_STEP)
        self.assertEqual(
            _restic_snapshot_args("latest", source_target="main-prod"),
            ["restic", "snapshots", "--host", "main-prod", "--json"],
        )

    def test_explicit_snapshot_id_is_not_host_filtered(self):
        args = _restic_snapshot_args("abc123", source_target="main-prod")
        self.assertEqual(args, ["restic", "snapshots", "abc123", "--json"])
        self.assertNotIn("--host", args)

    def test_latest_branch_has_no_host_blind_snapshot_query(self):
        latest_branch = _snapshot_resolution_script().split("else", 1)[0]
        self.assertNotRegex(latest_branch, r"restic snapshots\s+--json")
        self.assertRegex(
            latest_branch,
            r'restic snapshots\s+--host\s+"\$SOURCE_TARGET"\s+--json',
        )


if __name__ == "__main__":
    unittest.main()
