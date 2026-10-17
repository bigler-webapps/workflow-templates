"""WM-SEC-8 runner per-job tailnet binding structural and template tests."""

from pathlib import Path

import yaml
from jinja2 import Environment

ROLE = Path(__file__).resolve().parents[1]
ROOT = ROLE.parents[2]
MGMT = ROOT / "ansible" / "roles" / "tailscale_mgmt"


def _render(path, values):
    return Environment().from_string(path.read_text(encoding="utf-8")).render(
        ansible_managed="managed by Ansible", **values
    )


def _defaults():
    return yaml.safe_load((ROLE / "defaults" / "main.yml").read_text(encoding="utf-8"))


def test_ssh_config_binds_default_tailnet_patterns_to_job_interface():
    values = _defaults()
    rendered = _render(ROLE / "templates" / "tailnet-binding-ssh.conf.j2", values)
    assert "Host *.tail990d7f.ts.net 100.*" in rendered
    assert "BindInterface tailscale0" in rendered
    assert "tsmgmt0" not in rendered


def test_route_script_routes_job_sourced_traffic_by_source_address_not_by_device():
    """BindInterface binds the interface ADDRESS, so an `oif` rule would never match."""
    values = dict(_defaults(), tailnet_binding_mgmt_address="100.71.144.113")
    rendered = _render(ROLE / "templates" / "tailnet-binding-route.sh.j2", values)
    code = [line.strip() for line in rendered.splitlines() if line.strip() and not line.strip().startswith("#")]
    for command in (
        'ip route replace "$CIDR" dev "$TUN" table "$TABLE"',
        'ip rule add from "$MGMT_ADDR/32" lookup "$SHARED_TABLE" priority "$MGMT_PRIORITY"',
        'ip rule add from "$CIDR" lookup "$TABLE" priority "$JOB_PRIORITY"',
    ):
        assert command in code, command  # an executable line, not text inside a comment
    assert 'ip route replace "$CIDR" dev "$TUN" table "$TABLE"' in rendered
    assert 'CIDR="100.64.0.0/10"' in rendered and 'TUN="tailscale0"' in rendered and 'TABLE="53"' in rendered
    assert 'MGMT_ADDR="100.71.144.113"' in rendered
    assert 'ip rule add from "$MGMT_ADDR/32" lookup "$SHARED_TABLE" priority "$MGMT_PRIORITY"' in rendered
    assert 'ip rule add from "$CIDR" lookup "$TABLE" priority "$JOB_PRIORITY"' in rendered
    assert 'SHARED_TABLE="52"' in rendered
    assert " oif " not in rendered.split("set -euo pipefail", 1)[1]
    # The management-source rule must be evaluated before the job-source rule.
    assert values["tailnet_binding_mgmt_rule_priority"] < values["tailnet_binding_rule_priority"]
    # Both priorities are replaced, never appended, so a changed management address leaves no stale rule.
    assert rendered.index('ip rule del priority "$MGMT_PRIORITY"') < rendered.index("ip rule add from")
    assert rendered.index('ip rule del priority "$JOB_PRIORITY"') < rendered.index("ip rule add from")
    mgmt_defaults = yaml.safe_load((MGMT / "defaults" / "main.yml").read_text(encoding="utf-8"))
    taken = {mgmt_defaults["tailscale_mgmt_route_priority"], 5210, 5230, 5250, 5270}
    assert values["tailnet_binding_rule_priority"] not in taken
    assert values["tailnet_binding_mgmt_rule_priority"] not in taken
    assert 5250 < values["tailnet_binding_mgmt_rule_priority"] < values["tailnet_binding_rule_priority"] < 5270


def test_route_script_refuses_an_empty_management_address():
    values = dict(_defaults(), tailnet_binding_mgmt_address="")
    rendered = _render(ROLE / "templates" / "tailnet-binding-route.sh.j2", values)
    guard = rendered.index('if [ -z "$MGMT_ADDR" ]')
    assert guard < rendered.index("ip route replace")
    assert "exit 1" in rendered[guard:guard + 200]


def test_management_address_defaults_to_the_tsmgmt0_fact():
    assert "ansible_tsmgmt0.ipv4.address" in _defaults()["tailnet_binding_mgmt_address"]


def test_unit_tracks_default_tailscaled_only():
    rendered = _render(ROLE / "templates" / "tailnet-binding-route.service.j2", _defaults())
    assert "After=tailscaled.service" in rendered
    assert "Requires=tailscaled.service" in rendered
    assert "PartOf=tailscaled.service" in rendered
    assert "tailscaled-mgmt" not in rendered


def test_tasks_deploy_then_reassert_then_verify_ssh_binding_and_gate_every_block():
    tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text(encoding="utf-8"))
    names = [task.get("name", "") for task in tasks]
    ssh = next(i for i, name in enumerate(names) if "SSH client binding" in name)
    script = next(i for i, name in enumerate(names) if "route script" in name)
    unit = next(i for i, name in enumerate(names) if "route systemd unit" in name)
    reassert = next(i for i, name in enumerate(names) if name.startswith("Reassert per-job"))
    guard = next(i for i, name in enumerate(names) if "Verify OpenSSH" in name)
    assert ssh < script < unit < reassert < guard
    assert tasks[ssh]["ansible.builtin.template"]
    assert tasks[script]["ansible.builtin.template"]
    assert tasks[unit]["ansible.builtin.template"]
    assert tasks[reassert]["ansible.builtin.systemd"]["state"] == "restarted"
    assert tasks[guard]["ansible.builtin.command"]["argv"][:2] == ["ssh", "-G"]
    assert tasks[guard]["changed_when"] is False
    assert "bindinterface" in tasks[guard]["failed_when"] and "tailnet_binding_interface" in tasks[guard]["failed_when"]
    assert "_tailnet_binding_ssh_config.rc != 0" in tasks[guard]["failed_when"]
    assert tasks[reassert]["ansible.builtin.systemd"]["enabled"] is True
    assert tasks[reassert]["ansible.builtin.systemd"]["name"] == "tailnet-binding-route.service"
    for task in (tasks[ssh], tasks[script], tasks[unit], tasks[reassert], tasks[guard]):
        condition = task.get("when", [])
        condition = [condition] if isinstance(condition, str) else condition
        assert "tailnet_binding_enabled | bool" in condition


def test_role_defaults_enable_binding():
    assert _defaults()["tailnet_binding_enabled"] is True


def test_management_route_pin_is_opt_out_and_scoped_to_pin_tasks():
    defaults = yaml.safe_load((MGMT / "defaults" / "main.yml").read_text(encoding="utf-8"))
    assert defaults["tailscale_mgmt_route_pin"] is True
    tasks = yaml.safe_load((MGMT / "tasks" / "main.yml").read_text(encoding="utf-8"))
    pin_names = {
        "Deploy tsmgmt-route-pin script",
        "Deploy tsmgmt-route-pin systemd unit",
        "Reassert tsmgmt-route-pin",
    }
    found = set()
    for task in tasks:
        if not isinstance(task, dict):
            continue
        name = task.get("name")
        if name in pin_names:
            found.add(name)
            condition = task["when"]
            condition = [condition] if isinstance(condition, str) else condition
            assert "tailscale_mgmt_route_pin | bool" in condition
        else:
            assert "tailscale_mgmt_route_pin" not in str(task.get("when", ""))
    assert found == pin_names
