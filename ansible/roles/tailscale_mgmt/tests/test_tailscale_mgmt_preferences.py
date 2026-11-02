from pathlib import Path
import json
import yaml
from jinja2 import Environment

ROLE = Path(__file__).resolve().parents[1]


def test_cleanup_names_mgmt_tun_and_up_optional_flags_survive_reset():
    unit = Environment(trim_blocks=True, lstrip_blocks=True).from_string((ROLE / "templates/tailscaled-mgmt.service.j2").read_text()).render(
        tailscale_mgmt_socket="/run/mgmt.sock", tailscale_mgmt_tun="tsmgmt0", tailscale_mgmt_statedir="/var/lib/mgmt", tailscale_mgmt_port=0)
    assert "--cleanup --socket=/run/mgmt.sock --tun=tsmgmt0" in unit
    assert unit.count("--tun=tsmgmt0") == 3
    tasks = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
    up = next(t for t in tasks if t.get("name") == "Bring mgmt tailscaled up")["ansible.builtin.command"]["argv"]
    rendered = yaml.safe_load(Environment(trim_blocks=True, lstrip_blocks=True).from_string(up).render(
        tailscale_mgmt_socket="/run/mgmt.sock", tailscale_mgmt_authkey="key", tailscale_mgmt_tag="tag:x",
        tailscale_mgmt_hostname="runner", tailscale_mgmt_netfilter_mode="off", tailscale_mgmt_accept_dns="true"))
    assert rendered[-3:] == ["--reset", "--netfilter-mode=off", "--accept-dns=true"]
    plain = yaml.safe_load(Environment(trim_blocks=True, lstrip_blocks=True).from_string(up).render(
        tailscale_mgmt_socket="/run/mgmt.sock", tailscale_mgmt_authkey="key", tailscale_mgmt_tag="tag:x",
        tailscale_mgmt_hostname="runner", tailscale_mgmt_netfilter_mode="", tailscale_mgmt_accept_dns=""))
    assert plain == ["tailscale", "--socket=/run/mgmt.sock", "up", "--authkey=key", "--advertise-tags=tag:x", "--hostname=runner", "--ssh=true", "--reset"]


def _tasks():
    return yaml.safe_load((ROLE / "tasks/main.yml").read_text())


def _needs_set(prefs, **declared):
    apply = next(t for t in _tasks() if t.get("name") == "Apply declared management Tailscale preferences")
    condition = next(c for c in apply["when"] if "_tailscale_mgmt_prefs.rc != 0" in c)
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    env = Environment(trim_blocks=True, lstrip_blocks=True)
    context = dict(defaults, _tailscale_mgmt_prefs={"rc": 0}, _tailscale_mgmt_prefs_json=prefs, **declared)
    return env.from_string("{{ " + condition + " }}").render(**context) == "True"


DECLARED = dict(tailscale_mgmt_netfilter_mode="off", tailscale_mgmt_accept_dns="true", tailscale_mgmt_auto_update="false")


def test_converged_management_preferences_do_not_trigger_a_set_in_either_prefs_rendering():
    for mode in (0, "off"):
        prefs = {"NetfilterMode": mode, "CorpDNS": True, "AutoUpdate": {"Check": True, "Apply": False}}
        assert _needs_set(prefs, **DECLARED) is False


def test_any_drifted_management_preference_triggers_a_set():
    base = {"NetfilterMode": 0, "CorpDNS": True, "AutoUpdate": {"Check": True, "Apply": False}}
    assert _needs_set(dict(base, NetfilterMode=2), **DECLARED) is True
    assert _needs_set(dict(base, CorpDNS=False), **DECLARED) is True
    assert _needs_set(dict(base, AutoUpdate={"Check": True, "Apply": True}), **DECLARED) is True


def test_set_uses_the_management_socket_and_the_restart_handler_is_configurable():
    apply = next(t for t in _tasks() if t.get("name") == "Apply declared management Tailscale preferences")
    env = Environment(trim_blocks=True, lstrip_blocks=True)
    rendered = yaml.safe_load(env.from_string(apply["ansible.builtin.command"]["argv"]).render(
        tailscale_mgmt_socket="/run/mgmt.sock", **DECLARED))
    assert rendered == ["tailscale", "--socket=/run/mgmt.sock", "set", "--netfilter-mode=off", "--accept-dns=true", "--auto-update=false"]
    handlers = yaml.safe_load((ROLE / "handlers/main.yml").read_text())
    restart = next(h for h in handlers if h.get("name") == "restart tailscaled-mgmt")
    assert restart["when"] == "tailscale_mgmt_restart_on_unit_change | bool"
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    assert defaults["tailscale_mgmt_restart_on_unit_change"] is True
