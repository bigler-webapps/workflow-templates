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
        'ip route replace "$CIDR" dev "$MGMT_TUN" table "$MGMT_TABLE"',
        'ip rule add from "$MGMT_ADDR/32" lookup "$MGMT_TABLE" priority "$MGMT_PRIORITY"',
        'ip rule add from "$CIDR" lookup "$TABLE" priority "$JOB_PRIORITY"',
    ):
        assert command in code, command  # an executable line, not text inside a comment
    assert 'ip route replace "$CIDR" dev "$TUN" table "$TABLE"' in rendered
    assert 'CIDR="100.64.0.0/10"' in rendered and 'TUN="tailscale0"' in rendered and 'TABLE="53"' in rendered
    assert 'MGMT_ADDR="100.71.144.113"' in rendered
    assert 'ip route replace "$CIDR" dev "$MGMT_TUN" table "$MGMT_TABLE"' in rendered
    assert 'CIDR="100.64.0.0/10"' in rendered and 'MGMT_TUN="tsmgmt0"' in rendered and 'MGMT_TABLE="54"' in rendered
    assert 'ip rule add from "$MGMT_ADDR/32" lookup "$MGMT_TABLE" priority "$MGMT_PRIORITY"' in rendered
    assert 'ip rule add from "$CIDR" lookup "$TABLE" priority "$JOB_PRIORITY"' in rendered
    assert "lookup \"$SHARED_TABLE\"" not in rendered
    assert " oif " not in rendered.split("set -euo pipefail", 1)[1]
    # The management-source rule must be evaluated before the job-source rule.
    assert values["tailnet_binding_mgmt_rule_priority"] < values["tailnet_binding_rule_priority"]
    # Mgmt rule is present before selector-specific stale-rule deletion; reruns add nothing.
    assert rendered.index('ip rule add from "$MGMT_ADDR/32" lookup "$MGMT_TABLE"') < rendered.index('ip rule del from "$selector"')
    assert rendered.index('if ! ip -4 rule show | grep -Fq') < rendered.index('ip rule add from "$MGMT_ADDR/32"')
    assert 'ip rule del priority "$MGMT_PRIORITY"' not in rendered
    assert rendered.index('ip rule del priority "$JOB_PRIORITY"') < rendered.index('ip rule add from "$CIDR"')
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


def test_unit_tracks_both_tailscaled_daemons():
    rendered = _render(ROLE / "templates" / "tailnet-binding-route.service.j2", _defaults())
    assert "After=tailscaled.service tailscaled-mgmt.service" in rendered
    assert "Wants=tailscaled-mgmt.service" in rendered
    assert "Requires=tailscaled.service" in rendered
    assert "PartOf=tailscaled.service tailscaled-mgmt.service" in rendered


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


IP_RULE_SIMULATOR = r"""#!/usr/bin/env bash
# A tiny `ip` that keeps the policy rules in $RULES_FILE as "<prio>:<TAB>from <src> lookup <table>" lines.
echo "ip $*" >> "$IP_LOG"
args=("$@")
[ "${args[0]:-}" = "-4" ] && args=("${args[@]:1}")
case "${args[0]:-}:${args[1]:-}" in
  link:show) exit 0 ;;
  route:replace) exit 0 ;;
  rule:show) cat "$RULES_FILE"; exit 0 ;;
  rule:add)
    # rule add from <src> lookup <table> priority <prio>
    src="${args[3]%/32}"; table="${args[5]}"; prio="${args[7]}"
    printf '%s:\tfrom %s lookup %s\n' "$prio" "$src" "$table" >> "$RULES_FILE"
    sort -s -t: -k1,1n "$RULES_FILE" -o "$RULES_FILE"
    exit 0 ;;
  rule:del)
    if [ "${args[2]}" = from ]; then
      pattern="^${args[7]}:.from ${args[3]} lookup ${args[5]}\$"
    else
      pattern="^${args[3]}:"
    fi
    line="$(grep -n -m1 -E "$pattern" "$RULES_FILE" | cut -d: -f1)"
    [ -n "$line" ] || exit 2
    sed -i "${line}d" "$RULES_FILE"
    exit 0 ;;
esac
exit 0
"""


def _run_route_script(tmp_path, rules, runs=1):
    import os
    import shutil
    import stat
    import subprocess

    import pytest

    bash = shutil.which("bash")
    if not bash:
        pytest.skip("no bash available on PATH")
    values = _defaults()
    values["tailnet_binding_mgmt_address"] = "100.71.144.113"
    script = tmp_path / "route.sh"
    script.write_text(_render(ROLE / "templates" / "tailnet-binding-route.sh.j2", values), encoding="utf-8", newline="\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ip_stub = bin_dir / "ip"
    ip_stub.write_text(IP_RULE_SIMULATOR, encoding="utf-8", newline="\n")
    ip_stub.chmod(ip_stub.stat().st_mode | stat.S_IEXEC)
    rules_file = tmp_path / "rules.txt"
    rules_file.write_text("".join(f"{line}\n" for line in rules), encoding="utf-8", newline="\n")
    logs = []
    for index in range(runs):
        log = tmp_path / f"ip-{index}.log"
        env = os.environ.copy()
        env.update({"PATH": str(bin_dir) + os.pathsep + env["PATH"], "RULES_FILE": str(rules_file), "IP_LOG": str(log)})
        proc = subprocess.run([bash, str(script)], env=env, capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        logs.append(log.read_text(encoding="utf-8").splitlines())
    return rules_file.read_text(encoding="utf-8").splitlines(), logs


def test_route_script_swaps_the_mgmt_rule_add_before_remove_and_is_idempotent(tmp_path):
    """Executes the rendered script against a simulated rule table that starts with the OLD layout
    (mgmt source -> shared table 52) and runs it twice."""
    start = ["5254:\tfrom 100.71.144.113 lookup 52", "5255:\tfrom 100.64.0.0/10 lookup 53"]
    final, (first, second) = _run_route_script(tmp_path, start, runs=2)

    assert final == ["5254:\tfrom 100.71.144.113 lookup 54", "5255:\tfrom 100.64.0.0/10 lookup 53"]

    add_new = next(i for i, call in enumerate(first) if call.startswith("ip rule add from 100.71.144.113/32 lookup 54 priority 5254"))
    del_old = next(i for i, call in enumerate(first) if call.startswith("ip rule del from 100.71.144.113 lookup 52 priority 5254"))
    assert add_new < del_old, "the new mgmt rule must exist before the old one is removed"
    assert not any(call.startswith("ip rule del priority 5254") for call in first), "no blanket delete at 5254"
    assert any(call == "ip route replace 100.64.0.0/10 dev tsmgmt0 table 54" for call in first)

    assert not any(call.startswith("ip rule add from 100.71.144.113/32") for call in second), "a re-run adds no second mgmt rule"
    assert not any(call.startswith("ip rule del from 100.71.144.113") for call in second), "a re-run removes nothing"


def test_route_script_removes_a_stale_mgmt_address_but_keeps_the_current_rule(tmp_path):
    start = ["5254:\tfrom 100.71.144.113 lookup 54", "5254:\tfrom 100.99.99.99 lookup 52", "5255:\tfrom 100.64.0.0/10 lookup 53"]
    final, (calls,) = _run_route_script(tmp_path, start)
    assert final == ["5254:\tfrom 100.71.144.113 lookup 54", "5255:\tfrom 100.64.0.0/10 lookup 53"]
    assert not any(call.startswith("ip rule add from 100.71.144.113/32") for call in calls)
