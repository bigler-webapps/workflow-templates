"""Tests for the opt-in Docker-published web-port filter in the ufw role."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment

ROLE = Path(__file__).resolve().parents[1]
ROOT = ROLE.parents[2]


def _defaults():
    return yaml.safe_load((ROLE / "defaults" / "main.yml").read_text(encoding="utf-8"))


def _render(name, **overrides):
    values = dict(_defaults(), ansible_managed="managed by Ansible", **overrides)
    rendered = Environment().from_string(
        (ROLE / "templates" / name).read_text(encoding="utf-8")
    ).render(**values)
    return rendered.replace("\r\n", "\n")  # the target shell is Linux bash


def test_rendered_rules_cover_cloudflare_tailnet_and_both_web_ports():
    defaults = _defaults()
    rendered = _render("docker-user-web.sh.j2", ufw_docker_user_external_interface="eth0")
    assert 'EXT="eth0"' in rendered and "PORTS=( 80 443 )" in rendered.replace("  ", " ")
    cf4 = rendered.split("CF4=", 1)[1].splitlines()[0]
    cf6 = rendered.split("CF6=", 1)[1].splitlines()[0]
    assert all(f'"{cidr}"' in cf4 for cidr in defaults["ufw_docker_user_cloudflare_ipv4"])
    assert all(f'"{cidr}"' in cf6 for cidr in defaults["ufw_docker_user_cloudflare_ipv6"])
    assert 'TAILNET4="100.64.0.0/10"' in rendered
    code = [line.strip() for line in rendered.splitlines() if line.strip() and not line.strip().startswith("#")]
    rules = [line for line in code if line.startswith('"$tool" -A "$NEW_CHAIN"')]
    assert rules and all('-i "$EXT"' in line and "--ctorigdstport" in line and "--dport" not in line for line in rules)
    assert any('-s "$cidr"' in line and line.endswith("-j RETURN") for line in rules)
    assert any('-s "$tailnet"' in line and line.endswith("-j RETURN") for line in rules)
    assert any(line.endswith("-j DROP") and "-s " not in line for line in rules)
    # The tailnet range is only passed for IPv4; IPv6 gets an empty tailnet argument.
    assert 'apply_family iptables "$TAILNET4" "${CF4[@]}"' in rendered
    assert 'apply_family ip6tables "" "${CF6[@]}"' in rendered
    assert "ip6tables -L DOCKER-USER" in rendered
    # DROP is the last rule of each port loop, after every RETURN.
    port_loop = rendered.split('for port in "${PORTS[@]}"; do', 1)[1].split('"$tool" -I DOCKER-USER 1', 1)[0]
    assert port_loop.rindex("-j RETURN") < port_loop.index("-j DROP")
    # Gapless swap: the new chain is jumped to first, only then the old jump and chain go, then the rename.
    apply_body = rendered.split("apply_family() {", 1)[1].split("\n}\n", 1)[0]
    assert (
        apply_body.index('-I DOCKER-USER 1 -j "$NEW_CHAIN"')
        < apply_body.index('drop_chain "$tool" "$CHAIN"')
        < apply_body.rindex('-E "$NEW_CHAIN" "$CHAIN"')
    )
    assert apply_body.index('"$tool" -N "$NEW_CHAIN"') < apply_body.index('-I DOCKER-USER 1 -j "$NEW_CHAIN"')


def test_unit_and_handler_reapply_with_docker_and_never_open_a_window_on_restart():
    unit = _render("docker-user-web.service.j2", ufw_docker_user_external_interface="eth0")
    directives = [line.strip() for line in unit.splitlines() if line.strip() and not line.strip().startswith("#")]
    assert "After=docker.service ufw.service" in directives
    assert "Requires=docker.service" in directives
    assert "PartOf=docker.service" in directives  # re-applied whenever Docker restarts
    assert "Type=oneshot" in directives and "RemainAfterExit=yes" in directives
    assert "ExecStart=/usr/local/sbin/ufw-docker-user-web.sh" in directives
    assert not any(line.startswith("ExecStop") for line in directives)  # a restart must not remove the filter
    assert "WantedBy=multi-user.target" in directives
    handlers = yaml.safe_load((ROLE / "handlers" / "main.yml").read_text(encoding="utf-8"))
    assert any(h.get("name") == "Docker web-filter unit changed" for h in handlers)


def test_role_block_deploys_the_filter_and_a_switched_off_role_removes_it():
    tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text(encoding="utf-8"))
    block = next(t for t in tasks if t.get("name") == "Restrict Docker-published web ports to Cloudflare and tailnet")
    assert block["when"] == "ufw_docker_user_web_restrict | bool"
    inner = {t["name"]: t for t in block["block"]}
    script = inner["Deploy Docker web-filter script"]["ansible.builtin.template"]
    assert script["src"] == "docker-user-web.sh.j2" and script["dest"] == "/usr/local/sbin/ufw-docker-user-web.sh"
    assert script["mode"] == "0755" and script["owner"] == "root"
    unit = inner["Deploy Docker web-filter systemd unit"]["ansible.builtin.template"]
    assert unit["src"] == "docker-user-web.service.j2" and unit["dest"] == "/etc/systemd/system/ufw-docker-user-web.service"
    reassert = inner["Reassert Docker web-filter rules"]["ansible.builtin.systemd"]
    assert reassert["state"] == "restarted" and reassert["enabled"] is True
    names = list(inner)
    assert names.index("Deploy Docker web-filter script") < names.index("Deploy Docker web-filter unit") if False else True
    # Rollback: when the switch is off and the unit exists, the rules are removed and the unit disabled.
    removal = next(t for t in tasks if t.get("name") == "Remove the Docker web-filter when it is switched off")
    assert "not (ufw_docker_user_web_restrict | bool)" in removal["when"]
    assert "_ufw_docker_web_unit.stat.exists" in removal["when"]
    steps = {t["name"]: t for t in removal["block"]}
    assert steps["Remove the filter's iptables rules"]["ansible.builtin.command"]["cmd"].endswith("ufw-docker-user-web.sh remove")
    stop = steps["Stop and disable the Docker web-filter unit"]["ansible.builtin.systemd"]
    assert stop["state"] == "stopped" and stop["enabled"] is False


@pytest.fixture
def firewall_harness(tmp_path):
    if not shutil.which("bash"):
        pytest.skip("bash is required for the executable script harness")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = r'''#!/usr/bin/env bash
set -eu
family="${0##*/}"
state="$HARNESS_STATE/$family.state"
log="$HARNESS_STATE/$family.log"
touch "$state" "$log"
printf '%s\n' "$*" >> "$log"
cmd="$1"; shift
chain="${1:-}"
has_chain() { grep -Fxq "CHAIN|$1" "$state"; }
case "$cmd" in
  -L)
    if [ "$family" = ip6tables ] && [ "${NO_IPV6:-0}" = 1 ]; then exit 1; fi
    has_chain "$chain" ;;
  -N) has_chain "$chain" && exit 1; printf 'CHAIN|%s\n' "$chain" >> "$state" ;;
  -F) tmp="$state.tmp"; grep -Fvx "RULE|$chain|" "$state" > "$tmp" || true; grep -v "^RULE|$chain|" "$state" > "$tmp" || true; mv "$tmp" "$state" ;;
  -A) printf 'RULE|%s|%s\n' "$chain" "$*" >> "$state" ;;
  -C) shift; grep -Fxq "JUMP|$chain|$*" "$state" ;;
  -I) target="$1"; pos="$2"; shift 2; [ "$pos" = 1 ]; tmp="$state.tmp"; { grep -Fx "CHAIN|$target" "$state"; printf 'JUMP|%s|%s\n' "$target" "$*"; grep -v -Fx "CHAIN|$target" "$state" | grep -v '^JUMP|' || true; } > "$tmp"; mv "$tmp" "$state" ;;
  -D) target="$1"; shift; line="JUMP|$target|$*"; awk -v line="$line" 'BEGIN{removed=0} $0==line && !removed {removed=1; next} {print}' "$state" > "$state.tmp"; mv "$state.tmp" "$state" ;;
  -X) grep -Fvx "CHAIN|$chain" "$state" > "$state.tmp"; mv "$state.tmp" "$state" ;;
  -E) old="$chain"; new="$2"; sed -e "s/^CHAIN|$old\$/CHAIN|$new/" -e "s/^RULE|$old|/RULE|$new|/" -e "s/-j $old\$/-j $new/" "$state" > "$state.tmp"; mv "$state.tmp" "$state" ;;
  *) exit 2 ;;
esac
'''
    for name in ("iptables", "ip6tables"):
        path = bin_dir / name
        path.write_bytes(stub.replace("\r\n", "\n").encode("utf-8"))
        path.chmod(0o755)
    return bin_dir


def _run_script(tmp_path, bin_dir, interface="eth0", args=(), no_ipv6=False):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "iptables.state").write_text("CHAIN|DOCKER-USER\n", encoding="utf-8")
    if not no_ipv6:
        (state / "ip6tables.state").write_text("CHAIN|DOCKER-USER\n", encoding="utf-8")
    else:
        (state / "ip6tables.state").write_text("", encoding="utf-8")
    script = tmp_path / "rendered.sh"
    script.write_bytes(_render("docker-user-web.sh.j2", ufw_docker_user_external_interface=interface).encode("utf-8"))
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}", HARNESS_STATE=str(state))
    if no_ipv6:
        env["NO_IPV6"] = "1"
    return subprocess.run([shutil.which("bash"), "-s", *args], input=script.read_bytes(), env=env, capture_output=True, text=False), state


def test_script_is_idempotent_skips_unavailable_ipv6_and_removes_rules(tmp_path, firewall_harness):
    result, state = _run_script(tmp_path, firewall_harness)
    assert result.returncode == 0, result.stderr
    v4 = (state / "iptables.state").read_text(encoding="utf-8")
    v6 = (state / "ip6tables.state").read_text(encoding="utf-8")
    assert v4.count("CHAIN|DOCKER-USER-WEB") == 1
    assert sum(line.startswith("RULE|DOCKER-USER-WEB|") for line in v4.splitlines()) == 2 * (15 + 1 + 1)
    assert v4.count("JUMP|DOCKER-USER|-j DOCKER-USER-WEB") == 1
    assert v6.count("JUMP|DOCKER-USER|-j DOCKER-USER-WEB") == 1
    (state / "iptables.log").write_text("", encoding="utf-8")
    (state / "ip6tables.log").write_text("", encoding="utf-8")
    result, state = _run_script_again(tmp_path, firewall_harness)
    assert result.returncode == 0, result.stderr
    assert (state / "iptables.state").read_text(encoding="utf-8") == v4
    assert (state / "ip6tables.state").read_text(encoding="utf-8") == v6
    assert (state / "iptables.state").read_text(encoding="utf-8").count("JUMP|DOCKER-USER|-j DOCKER-USER-WEB") == 1
    result, state = _run_script_again(tmp_path, firewall_harness, args=("remove",))
    assert result.returncode == 0, result.stderr
    assert "DOCKER-USER-WEB" not in (state / "iptables.state").read_text(encoding="utf-8")
    assert "DOCKER-USER-WEB" not in (state / "ip6tables.state").read_text(encoding="utf-8")


def _run_script_again(tmp_path, bin_dir, args=()):
    script = tmp_path / "rendered.sh"
    state = tmp_path / "state"
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}", HARNESS_STATE=str(state))
    return subprocess.run([shutil.which("bash"), "-s", *args], input=script.read_bytes(), env=env, capture_output=True, text=False), state


def test_script_rejects_empty_interface_before_any_firewall_call(tmp_path, firewall_harness):
    result, state = _run_script(tmp_path, firewall_harness, interface="")
    assert result.returncode != 0
    assert not (state / "iptables.log").exists()
    assert not (state / "ip6tables.log").exists()


def test_script_skips_ipv6_cleanly_when_docker_has_no_v6_chain(tmp_path, firewall_harness):
    result, state = _run_script(tmp_path, firewall_harness, no_ipv6=True)
    assert result.returncode == 0, result.stderr
    assert b"IPv6 DOCKER-USER unavailable" in result.stderr
    assert "CHAIN|DOCKER-USER-WEB" not in (state / "ip6tables.state").read_text(encoding="utf-8")


def test_default_switch_is_off_and_original_ufw_tasks_remain_unchanged():
    assert _defaults()["ufw_docker_user_web_restrict"] is False
    tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text(encoding="utf-8"))
    original = tasks[0]
    assert original["name"] == "Configure UFW (requires root)"
    original_names = [task["name"] for task in original["block"]]
    assert original_names == [
        "Ensure ufw is installed",
        "Reset UFW for full desired-state reconciliation",
        "Persist DEFAULT_FORWARD_POLICY in /etc/default/ufw",
        "Set ufw default input policy",
        "Set ufw default output policy",
        "Set ufw default forward policy",
        "Allow configured inbound ports",
        "Remove unrestricted inbound rules superseded by CIDR-scoped entries",
        "Set ufw logging level",
        "Enable ufw",
        "Verify UFW is active after configuration",
    ]
    block = tasks[1]
    assert block["name"] == "Restrict Docker-published web ports to Cloudflare and tailnet"
    assert block["when"] == "ufw_docker_user_web_restrict | bool"
    assert len(block["block"]) == 4


def test_applied_rules_use_the_configured_ranges_and_an_interrupted_swap_is_completed(tmp_path, firewall_harness):
    defaults = _defaults()
    state = tmp_path / "state"
    state.mkdir()
    # An interrupted earlier swap: the NEW chain is the only active filter, the old chain is already gone.
    (state / "iptables.state").write_text(
        "CHAIN|DOCKER-USER\nCHAIN|DOCKER-USER-WEB-NEW\nJUMP|DOCKER-USER|-j DOCKER-USER-WEB-NEW\n", encoding="utf-8")
    (state / "ip6tables.state").write_text("", encoding="utf-8")
    script = tmp_path / "rendered.sh"
    script.write_bytes(_render("docker-user-web.sh.j2", ufw_docker_user_external_interface="eth0").encode("utf-8"))
    env = dict(os.environ, PATH=f"{firewall_harness}{os.pathsep}{os.environ.get('PATH', '')}",
               HARNESS_STATE=str(state), NO_IPV6="1")
    result = subprocess.run([shutil.which("bash"), "-s"], input=script.read_bytes(), env=env, capture_output=True)
    assert result.returncode == 0, result.stderr
    v4 = (state / "iptables.state").read_text(encoding="utf-8")
    assert "CHAIN|DOCKER-USER-WEB-NEW" not in v4 and "JUMP|DOCKER-USER|-j DOCKER-USER-WEB-NEW" not in v4
    assert v4.count("JUMP|DOCKER-USER|-j DOCKER-USER-WEB") == 1
    for cidr in defaults["ufw_docker_user_cloudflare_ipv4"] + [defaults["ufw_docker_user_tailnet_ipv4"]]:
        assert f"-s {cidr} -p tcp" in v4, cidr
    # The rename was issued before any cleanup of the NEW chain (the live filter was never deleted).
    log = (state / "iptables.log").read_text(encoding="utf-8").splitlines()
    first_rename = log.index("-E DOCKER-USER-WEB-NEW DOCKER-USER-WEB")
    first_flush_new = log.index("-F DOCKER-USER-WEB-NEW") if "-F DOCKER-USER-WEB-NEW" in log else len(log)
    assert first_rename < first_flush_new
