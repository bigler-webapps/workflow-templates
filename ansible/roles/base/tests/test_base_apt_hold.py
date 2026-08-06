"""INF-5 — structural guards for the base role's transient apt hold.

These assert the shape that makes the fix correct, not merely that the YAML
parses: the hold must be set BEFORE the apt upgrade, and the release must sit
under an `always:` key so no failure path can leave a host frozen on an old
daemon. There is deliberately no test of the hold's runtime effect — that is
only observable on a real host with a pending upgrade (the operator-gated
`ansible-provision` acceptance test in the work order).

Run: pytest ansible/roles/base/tests/
"""

from pathlib import Path

import pytest
import yaml

ROLE = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def tasks():
    return yaml.safe_load((ROLE / "tasks" / "main.yml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def defaults():
    return yaml.safe_load((ROLE / "defaults" / "main.yml").read_text(encoding="utf-8"))


def _find_block(tasks, name_fragment):
    """Depth-first search for a task dict whose name contains the fragment."""
    stack = list(tasks)
    while stack:
        task = stack.pop(0)
        if not isinstance(task, dict):
            continue
        if name_fragment in (task.get("name") or ""):
            return task
        for key in ("block", "always", "rescue"):
            stack.extend(task.get(key) or [])
    return None


def test_hold_list_defaults_to_tailscale(defaults):
    assert "tailscale" in defaults["base_apt_hold_packages"]


def test_upgrade_block_holds_before_upgrading(tasks):
    block = _find_block(tasks, "excluding transport-critical packages")
    assert block is not None, "the guarded apt block is missing"

    names = [t.get("name", "") for t in block["block"]]
    hold_idx = next(i for i, n in enumerate(names) if n.startswith("Hold packages"))
    apt_idx = next(i for i, n in enumerate(names) if n == "Apt update and upgrade")
    assert hold_idx < apt_idx, "the hold must be set before the apt upgrade runs"


def test_release_is_in_an_always_section(tasks):
    block = _find_block(tasks, "excluding transport-critical packages")
    always = block.get("always")
    assert always, "the release must live under `always:`, not in the happy path"

    release = always[0]
    assert release["ansible.builtin.dpkg_selections"]["selection"] == "install"


def test_hold_and_release_cover_the_same_set(tasks):
    block = _find_block(tasks, "excluding transport-critical packages")
    hold = _find_block(block["block"], "Hold packages")
    release = _find_block(block["always"], "Release the transient")
    # Both loop over the computed effective set, so a package can never be held
    # without being released by the same run.
    assert "base_apt_hold_effective" in hold["loop"]
    assert "base_apt_hold_effective" in release["loop"]


def test_safety_net_is_armed_before_the_upgrade(tasks):
    """R1: `always` cannot run if the controller dies mid-play, which would
    leave a real dpkg hold behind — and both designated upgrade paths respect
    holds silently. A host-side timer bounds that window."""
    block = _find_block(tasks, "excluding transport-critical packages")
    names = [t.get("name", "") for t in block["block"]]
    net_idx = next(i for i, n in enumerate(names) if "safety net" in n)
    apt_idx = next(i for i, n in enumerate(names) if n == "Apt update and upgrade")
    assert net_idx < apt_idx, "the safety net must be armed before anything can fail"

    arm = _find_block(block["block"], "safety net")
    cmd = arm["ansible.builtin.command"]["cmd"]
    assert "systemd-run" in cmd
    assert "apt-mark unhold" in cmd
    # Anonymous unit: a --unit name would collide on back-to-back provisions.
    assert "--unit" not in cmd


def test_safety_net_window_is_configurable_and_exceeds_a_play(defaults):
    assert defaults["base_apt_hold_safety_net"].endswith("min")
    assert int(defaults["base_apt_hold_safety_net"].removesuffix("min")) >= 30


def test_only_installed_packages_are_held(tasks):
    """A fresh host has no tailscale yet (the tailscale role runs later), so
    holding it there would be a spurious change."""
    block = _find_block(tasks, "excluding transport-critical packages")
    effective = _find_block(block["block"], "Determine the effective hold set")
    expr = effective["ansible.builtin.set_fact"]["base_apt_hold_effective"]
    assert "ansible_facts.packages" in expr
