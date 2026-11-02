import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml
from jinja2 import Environment

ROLE = Path(__file__).resolve().parents[1]

CLI = r'''import json, os, sys
from pathlib import Path
tool = Path(sys.argv[1]).name
args = sys.argv[2:]
state_path = Path(os.environ["FW_STATE"])
state = json.loads(state_path.read_text())
fam = state[tool]
op = args[0]; chain = args[1] if len(args) > 1 else ""
builtin = {"INPUT", "FORWARD", "OUTPUT", "PREROUTING", "POSTROUTING", "DOCKER-USER", "DOCKER"}
def save(): state_path.write_text(json.dumps(state))
def exists(c): return c in fam["chains"] or c in builtin
if op == "-N":
    if exists(chain): sys.exit(1)
    fam["chains"][chain] = []; save()
elif op == "-L": sys.exit(0 if exists(chain) else 1)
elif op == "-F":
    if chain in fam["chains"]: fam["chains"][chain] = []; save()
    else: sys.exit(1)
elif op == "-X":
    if chain in fam["chains"]: del fam["chains"][chain]; save()
    else: sys.exit(1)
elif op in ("-A", "-I", "-D", "-C"):
    rules = fam["chains"].setdefault(chain, [])
    def check(rule):
        for option in ("-i", "-s", "-p", "--dport", "-j"):
            if rule.count(option) > 1:
                print("iptables: multiple " + option + " flags not allowed", file=sys.stderr); sys.exit(2)
    if op == "-I":
        ix = int(args[2]) - 1; rule = args[3:]; check(rule); rules.insert(ix, rule); save()
    elif op == "-A": check(args[2:]); rules.append(args[2:]); save()
    elif op == "-C": sys.exit(0 if args[2:] in rules else 1)
    else:
        try: rules.remove(args[2:]); save()
        except ValueError: sys.exit(1)
elif op == "-E":
    old, new = args[1:3]
    if old not in fam["chains"] or new in fam["chains"]: sys.exit(1)
    fam["chains"][new] = fam["chains"].pop(old)
    for rules in fam["chains"].values():
        for rule in rules:
            if "-j" in rule and rule[rule.index("-j") + 1] == old: rule[rule.index("-j") + 1] = new
    save()
elif op == "-S":
    for c, rules in [(chain, fam["chains"].get(chain, []))]:
        for rule in rules: print("-A " + c + " " + " ".join(rule))
else: sys.exit(0)
'''


def _fixture(tmp_path):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash unavailable")
    values = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    values["runner_tailnet_firewall_health_consumers"] = ["100.75.185.25/32"]
    rendered = Environment(trim_blocks=True, lstrip_blocks=True).from_string((ROLE / "templates/runner-tailnet-firewall.sh.j2").read_text()).render(ansible_managed="test", **values)
    script = tmp_path / "firewall.sh"
    script.write_text(rendered, encoding="utf-8", newline="\n")
    script.chmod(0o755)
    cli = tmp_path / "cli.py"
    cli.write_text(CLI, encoding="utf-8")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("iptables", "ip6tables"):
        exe = bindir / name
        exe.write_text('#!/usr/bin/env bash\nexec python3 "$FW_CLI" "$0" "$@"\n', encoding="utf-8", newline="\n")
        exe.chmod(0o755)
    state = {}
    for name in ("iptables", "ip6tables"):
        state[name] = {"chains": {
            "INPUT": [["-j", "UFW-BEFORE-INPUT"]],
            "FORWARD": [["-j", "DOCKER-USER"], ["-j", "DOCKER"]],
            "DOCKER-USER": [], "DOCKER": [], "POSTROUTING": [["-j", "MASQUERADE"]],
        }}
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(state))
    env = os.environ.copy()
    env.update({"PATH": str(bindir) + os.pathsep + env["PATH"], "FW_CLI": str(cli), "FW_STATE": str(state_path)})
    return bash, script, state_path, env


def _evaluate(family, chain, packet, state, depth=0):
    assert depth < 10
    rules = state[family]["chains"].get(chain, [])
    for rule in rules:
        i = 0; match = True
        while i < len(rule):
            token = rule[i]
            if token in ("-m",): i += 2; continue
            if token == "-i": match &= packet.get("i") == rule[i + 1]; i += 2; continue
            if token == "!" and rule[i + 1] == "-i": match &= packet.get("i") != rule[i + 2]; i += 3; continue
            if token == "-s":
                try: match &= ipaddress.ip_address(packet["src"]) in ipaddress.ip_network(rule[i + 1], strict=False)
                except ValueError: match = False
                i += 2; continue
            if token == "-p": match &= packet.get("proto") == rule[i + 1]; i += 2; continue
            if token == "--dport": match &= str(packet.get("port")) == rule[i + 1]; i += 2; continue
            if token == "--ctstate": match &= packet.get("state") in rule[i + 1].split(","); i += 2; continue
            if token == "-j":
                target = rule[i + 1]
                if not match: break
                if target == "RETURN": return "RETURN"
                if target in ("ACCEPT", "DROP"): return target
                result = _evaluate(family, target, packet, state, depth + 1)
                if result != "RETURN": return result
                break
            i += 1
    # The host firewall owns the eventual INPUT/FORWARD verdict; this runner
    # chain test models packets returning to UFW after our chain has no match.
    return "RETURN"


def _run(tmp_path, mode="apply", twice=False):
    bash, script, state_path, env = _fixture(tmp_path)
    for _ in range(2 if twice else 1):
        proc = subprocess.run([bash, str(script), mode], env=env, capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(state_path.read_text()), script, env, bash


def test_real_rendered_policy_allows_only_declared_management_health_and_established_replies(tmp_path):
    state, _, _, _ = _run(tmp_path, twice=True)
    v4 = state["iptables"]
    def verdict(src, interface, port=9110, proto="tcp", conn="NEW", family="iptables"):
        return _evaluate(family, "INPUT", {"src": src, "i": interface, "port": port, "proto": proto, "state": conn}, state)
    assert verdict("100.75.185.25", "tsmgmt0") == "ACCEPT"
    assert verdict("100.75.185.26", "tsmgmt0") == "DROP"
    assert verdict("100.75.185.25", "tailscale0") == "DROP"
    assert verdict("203.0.113.5", "eth0") == "RETURN"  # no tailnet source: ufw's default deny decides
    assert verdict("100.75.185.25", "eth0") == "DROP"  # spoofed consumer address on the public interface
    assert verdict("100.109.210.125", "tsmgmt0") == "DROP"  # not a declared consumer
    assert verdict("172.18.0.2", "br-compose") == "RETURN"  # same-host container reaches the responder via ufw (INF-7)
    assert verdict("172.18.0.2", "tailscale0") == "DROP"
    # Replies/established traffic never revives a health connection that is not a declared consumer's.
    assert verdict("100.109.210.125", "tsmgmt0", 9110, conn="ESTABLISHED") == "DROP"
    assert verdict("100.75.185.25", "tsmgmt0", 9110, conn="ESTABLISHED") == "ACCEPT"
    # Management SSH/provisioning, Tailscale transport and job-origin flows are left to ufw / the kernel path, never dropped here.
    assert verdict("100.100.100.1", "tsmgmt0", 22) == "RETURN"
    assert verdict("100.100.100.1", "tailscale0", 22) == "RETURN"
    assert verdict("203.0.113.9", "eth0", 41641, proto="udp") == "RETURN"
    assert verdict("100.109.210.125", "tailscale0", 5432, conn="ESTABLISHED") == "ACCEPT"
    assert verdict("100.75.185.25", "tailscale0", 22, conn="ESTABLISHED") == "ACCEPT"
    assert verdict("100.75.185.25", "tsmgmt0", 22, conn="RELATED") == "ACCEPT"
    assert verdict("203.0.113.5", "eth0", 443) == "RETURN"
    assert verdict("203.0.113.5", "eth0", 22) == "RETURN"
    assert verdict("100.109.210.125", "docker0", 443) == "DROP"
    assert verdict("100.109.210.125", "eth0", 443) == "DROP"
    assert verdict("100.109.210.125", "lo", 443) == "RETURN"
    assert verdict("fd7a:115c:a1e0::25", "eth0", 443, family="ip6tables") == "DROP"
    assert verdict("fd7a:115c:a1e0::25", "tsmgmt0", 22, conn="ESTABLISHED", family="ip6tables") == "ACCEPT"
    assert verdict("203.0.113.5", "eth0", 443, family="ip6tables") == "RETURN"
    assert verdict("fd7a:115c:a1e0::25", "docker0", 443, family="ip6tables") == "DROP"
    assert verdict("fd7a:115c:a1e0::25", "lo", 443, family="ip6tables") == "RETURN"
    for family in ("iptables", "ip6tables"):
        forward = state[family]["chains"]["WM-TAILNET-FWD"]
        assert not any("-j" in rule and rule[rule.index("-j") + 1] == "ACCEPT" for rule in forward)
    assert "WM-TAILNET-IN" in v4["chains"] and "WM-TAILNET-FWD" in v4["chains"]


def test_cleanup_rerun_and_remove_preserve_docker_and_postrouting(tmp_path):
    state, script, env, bash = _run(tmp_path, twice=True)
    for family in ("iptables", "ip6tables"):
        assert "WM-TAILNET-IN" in state[family]["chains"]
        assert "DOCKER-USER" in state[family]["chains"] and "DOCKER" in state[family]["chains"]
        assert state[family]["chains"]["POSTROUTING"] == [["-j", "MASQUERADE"]]


def test_displaced_jump_and_simulated_tailscale_cleanup_do_not_remove_owned_policy(tmp_path):
    state, script, env, bash = _run(tmp_path)
    state_path = Path(env["FW_STATE"])
    for family in ("iptables", "ip6tables"):
        chains = state[family]["chains"]
        chains["INPUT"].insert(0, ["-j", "OTHER"])
        chains["ts-input"] = [["-j", "DROP"]]
        chains["INPUT"].append(["-j", "ts-input"])
    state_path.write_text(json.dumps(state))
    # Model tailscaled --cleanup deleting every ts-* chain and its references.
    state = json.loads(state_path.read_text())
    for family in ("iptables", "ip6tables"):
        chains = state[family]["chains"]
        for name in list(chains):
            if name.startswith("ts-"):
                del chains[name]
        for name, rules in chains.items():
            chains[name] = [r for r in rules if not ("-j" in r and r[r.index("-j") + 1].startswith("ts-"))]
    state_path.write_text(json.dumps(state))
    proc = subprocess.run([bash, str(script)], env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    state = json.loads(state_path.read_text())
    for family in ("iptables", "ip6tables"):
        assert "WM-TAILNET-IN" in state[family]["chains"]
        assert state[family]["chains"]["INPUT"][0] == ["-j", "WM-TAILNET-IN"]
    subprocess.run([bash, str(script), "remove"], env=env, check=True, timeout=120)
    state = json.loads(Path(env["FW_STATE"]).read_text())
    for family in ("iptables", "ip6tables"):
        assert "WM-TAILNET-IN" not in state[family]["chains"]
        assert "DOCKER-USER" in state[family]["chains"] and "DOCKER" in state[family]["chains"]
        assert state[family]["chains"]["POSTROUTING"] == [["-j", "MASQUERADE"]]
