"""INF-43 -- the fixed RestartSec=30 between ephemeral-runner jobs guarded
against an HTTP 409 registration race that jit-config.sh's stale-same-name
delete (step 2b) now handles structurally. This pins the replacement: a
role variable, well under 30, but never 0 (StartLimitIntervalSec=0 means no
start-limit brake, so a persistent 409 would otherwise hammer the API).

Run: pytest ansible/roles/github_runner/tests/
"""

import re

from pathlib import Path

ROLE = Path(__file__).resolve().parents[1]
TEMPLATE = (ROLE / "templates" / "gh-runner@.service.j2").read_text(encoding="utf-8")
DEFAULTS = (ROLE / "defaults" / "main.yml").read_text(encoding="utf-8")


def test_template_uses_the_role_variable_not_a_burned_in_number():
    assert "RestartSec={{ github_runner_restart_sec }}" in TEMPLATE
    assert "RestartSec=30" not in TEMPLATE


def test_default_restart_sec_is_set_and_under_30():
    match = re.search(r"^github_runner_restart_sec:\s*(\d+)\s*$", DEFAULTS, re.MULTILINE)
    assert match, "github_runner_restart_sec must be set in defaults/main.yml"
    value = int(match.group(1))
    assert value < 30, f"github_runner_restart_sec is {value}, expected < 30"


def test_default_restart_sec_is_above_zero():
    """A restart with no pause at all, against StartLimitIntervalSec=0
    (no start-limit brake), is the crash-loop this WO must not reintroduce."""
    match = re.search(r"^github_runner_restart_sec:\s*(\d+)\s*$", DEFAULTS, re.MULTILINE)
    assert match
    assert int(match.group(1)) > 0


def test_restart_always_and_start_limit_disabled_are_unchanged():
    """Regression guard: this WO changes the restart's TIMING, not its
    semantics -- Restart=always and the disabled start-limit must survive."""
    assert "Restart=always" in TEMPLATE
    assert "StartLimitIntervalSec=0" in TEMPLATE
