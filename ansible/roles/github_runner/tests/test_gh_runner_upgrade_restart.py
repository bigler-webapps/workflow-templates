"""Regression checks for the non-blocking post-upgrade runner restart."""

from pathlib import Path

import yaml


ROLE = Path(__file__).resolve().parents[1]
CI_TASKS = yaml.safe_load((ROLE / "tasks" / "ci.yml").read_text(encoding="utf-8"))
SERVICE_TEMPLATE = (ROLE / "templates" / "gh-runner@.service.j2").read_text(encoding="utf-8")
DRAIN_TEMPLATE = (ROLE / "templates" / "gh-runner-drain.sh.j2").read_text(encoding="utf-8")


def test_binary_upgrade_restart_is_non_blocking_and_upgrade_only():
    restarts = [task for task in CI_TASKS if task.get("name") == "Restart gh-runner slot services after binary upgrade"]
    assert len(restarts) == 1

    restart = restarts[0]
    systemd = restart["ansible.builtin.systemd"]
    assert systemd["state"] == "restarted"
    assert systemd["no_block"] is True
    assert restart["loop"] == "{{ range(1, github_runner_slots + 1) | list }}"
    assert restart["when"] == "_runner_needs_update | bool"


def test_no_other_task_restarts_or_uses_no_block():
    for task in CI_TASKS:
        systemd = task.get("ansible.builtin.systemd", {})
        if task.get("name") == "Restart gh-runner slot services after binary upgrade":
            continue
        assert systemd.get("state") != "restarted"
        assert "no_block" not in systemd


def test_enable_and_start_remains_blocking_for_new_slots():
    starters = [task for task in CI_TASKS if task.get("name") == "Enable and start gh-runner slot services"]
    assert len(starters) == 1
    systemd = starters[0]["ansible.builtin.systemd"]
    assert systemd["state"] == "started"
    assert "no_block" not in systemd


def test_service_and_drain_templates_keep_their_existing_contracts():
    assert "TimeoutStartSec=infinity" in SERVICE_TEMPLATE
    assert "Restart=always" in SERVICE_TEMPLATE
    assert "StartLimitIntervalSec=0" in SERVICE_TEMPLATE
    assert "extend)" in DRAIN_TEMPLATE
    assert "end)" in DRAIN_TEMPLATE


def test_the_unit_still_waits_on_the_drain_through_the_jit_broker():
    """The wait is what makes a blocking restart deadlock, and what the provision relies on: the unit's
    ExecStartPre runs the broker, and both brokers wait while the drain marker is valid."""
    assert "ExecStartPre=" in SERVICE_TEMPLATE and "bin/jit-config.sh" in SERVICE_TEMPLATE
    for name in ("jit-config.sh.j2", "jit-config-issuer.sh.j2"):
        broker = (ROLE / "templates" / name).read_text(encoding="utf-8")
        assert 'DRAIN_MARKER="/run/gh-runner.drain"' in broker, name
        assert 'while [[ -f "${DRAIN_MARKER}" ]]' in broker, name
