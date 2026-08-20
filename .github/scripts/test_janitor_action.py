"""Structural regression test for the janitor composite action's rsync scope.

Run with: python .github/scripts/test_janitor_action.py
"""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
JANITOR_ACTION = (ROOT / ".github/actions/janitor/action.yml").read_text(encoding="utf-8")


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


if __name__ == "__main__":
    unittest.main()
