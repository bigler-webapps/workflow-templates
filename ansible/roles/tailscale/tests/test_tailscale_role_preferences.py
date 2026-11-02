from pathlib import Path
import json
import yaml
from jinja2 import Environment

ROLE = Path(__file__).resolve().parents[1]


def _tasks():
    tasks = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
    flattened = []
    for task in tasks:
        flattened.extend(task.get("block", [task]))
    return flattened


def test_up_argv_keeps_current_flags_when_optional_values_empty_and_adds_declared_flags():
    task = next(t for t in _tasks() if t.get("name") == "Bring Tailscale up")
    expr = task["ansible.builtin.command"]["argv"]
    env = Environment(trim_blocks=True, lstrip_blocks=True)
    base = dict(tailscale_authkey="", tailscale_hostname="host", tailscale_ssh=False, tailscale_accept_routes=False,
                tailscale_advertise_tags=[], tailscale_advertise_routes=[], tailscale_netfilter_mode="", tailscale_accept_dns="")
    plain = yaml.safe_load(env.from_string(expr).render(**base))
    assert plain == ["tailscale", "up", "--reset", "--authkey=", "--hostname=host", "--ssh=false", "--accept-routes=false"]
    changed = dict(base, tailscale_netfilter_mode="off", tailscale_accept_dns="false")
    assert yaml.safe_load(env.from_string(expr).render(**changed))[-2:] == ["--netfilter-mode=off", "--accept-dns=false"]


def _needs_set(prefs, **declared):
    """Evaluate the `when` of the apply task for a given `tailscale debug prefs` document."""
    tasks = _tasks()
    apply = next(t for t in tasks if t.get("name") == "Apply declared Tailscale preferences")
    condition = next(c for c in apply["when"] if "_tailscale_prefs.rc != 0" in c)
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    env = Environment(trim_blocks=True, lstrip_blocks=True)
    context = dict(defaults, _tailscale_prefs={"rc": 0}, _tailscale_prefs_json=prefs, **declared)
    return env.from_string("{{ " + condition + " }}").render(**context) == "True"


DECLARED = dict(tailscale_netfilter_mode="off", tailscale_accept_dns="false", tailscale_auto_update="false")


def test_converged_preferences_do_not_trigger_a_set_in_either_prefs_rendering():
    # `tailscale debug prefs` prints the enum value (0 = off) or the name depending on the release; Apply is an opt.Bool.
    for mode in (0, "off"):
        prefs = {"NetfilterMode": mode, "CorpDNS": False, "AutoUpdate": {"Check": True, "Apply": False}}
        assert _needs_set(prefs, **DECLARED) is False


def test_any_drifted_preference_triggers_a_set():
    base = {"NetfilterMode": 0, "CorpDNS": False, "AutoUpdate": {"Check": True, "Apply": False}}
    assert _needs_set(dict(base, NetfilterMode=2), **DECLARED) is True
    assert _needs_set(dict(base, CorpDNS=True), **DECLARED) is True
    assert _needs_set(dict(base, AutoUpdate={"Check": True, "Apply": True}), **DECLARED) is True
    assert _needs_set(dict(base, AutoUpdate={"Check": True}), **DECLARED) is True
    assert _needs_set({"NetfilterMode": 0, "CorpDNS": False, "AutoUpdate": False}, **DECLARED) is False


def test_unset_variables_never_enforce_anything_and_set_is_declared_flags_only():
    tasks = _tasks()
    apply = next(t for t in tasks if t.get("name") == "Apply declared Tailscale preferences")
    argv = apply["ansible.builtin.command"]["argv"]
    env = Environment(trim_blocks=True, lstrip_blocks=True)
    rendered = yaml.safe_load(env.from_string(argv).render(tailscale_netfilter_mode="off", tailscale_accept_dns="", tailscale_auto_update="false"))
    assert rendered == ["tailscale", "set", "--netfilter-mode=off", "--auto-update=false"]
    for name in ("Read Tailscale preferences before enforcing runner settings", "Apply declared Tailscale preferences", "Assert Tailscale preferences"):
        task = next(t for t in tasks if t.get("name") == name)
        assert any("not ansible_check_mode" in str(c) for c in task.get("when", [])) or name.startswith("Read")
