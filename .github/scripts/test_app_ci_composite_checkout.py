"""Structural regression tests for app-ci's local composite checkout.

Run with: python .github/scripts/test_app_ci_composite_checkout.py
"""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = (ROOT / ".github/workflows/app-ci.yml").read_text(encoding="utf-8")


class CI13StructuralTests(unittest.TestCase):
    def test_publish_composite_uses_a_pinned_checkout_in_a_subfolder(self):
        """A cross-repo caller must not resolve this local action in its repo."""
        checkout = re.search(
            r"^      - name: Checkout workflow-templates \(for composite actions\)\n"
            r"        uses: actions/checkout@[^\n]+\n"
            r"        with:\n"
            r"          repository: bigler-webapps/workflow-templates\n"
            r"          ref: (?P<ref>[^\s#]+).*\n"
            r"          path: (?P<path>[^\s#]+)\s*$",
            CI_WORKFLOW,
            re.MULTILINE,
        )
        self.assertIsNotNone(checkout)
        assert checkout is not None

        checkout_path = checkout.group("path")
        self.assertNotIn(checkout_path, {"", "."})
        self.assertRegex(checkout.group("ref"), r"^[0-9a-f]{40}$")

        publish = re.search(
            r"^      - name: Publish backend image to GHCR\n.*?"
            r"^        uses: (?P<uses>\./[^\s]+)\s*$",
            CI_WORKFLOW,
            re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(publish)
        assert publish is not None
        self.assertIn(f"/{checkout_path}/", publish.group("uses"))
        self.assertLess(checkout.start(), publish.start())


if __name__ == "__main__":
    unittest.main()
