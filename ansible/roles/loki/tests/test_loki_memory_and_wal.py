"""WM-INF-88: keep Loki memory bounded and WAL recovery explicit."""

import re
from pathlib import Path

from jinja2 import Environment
import yaml

ROLE = Path(__file__).resolve().parents[1]
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text(encoding="utf-8"))
CONFIG_TEMPLATE = ROLE / "templates/loki-config.yml.j2"
COMPOSE_TEMPLATE = ROLE / "templates/docker-compose.yml.j2"
TASKS_FILE = ROLE / "tasks/main.yml"


def _render_config(**overrides):
    variables = {**DEFAULTS, **overrides}
    rendered = Environment().from_string(CONFIG_TEMPLATE.read_text(encoding="utf-8")).render(**variables)
    return yaml.safe_load(rendered)


def _render_compose(**overrides):
    variables = {
        **DEFAULTS,
        "ansible_tailscale0": {"ipv4": {"address": "100.64.0.1"}},
        **overrides,
    }
    rendered = Environment().from_string(COMPOSE_TEMPLATE.read_text(encoding="utf-8")).render(**variables)
    return yaml.safe_load(rendered)


def _bytes(value):
    """Parse Loki and Go size spellings used by this role's variables."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(bytes?|b|kib|kb|k|mib|mb|m|gib|gb|g)?\s*", str(value), re.I)
    assert match, value
    number = float(match.group(1))
    unit = (match.group(2) or "b").lower()
    multiplier = {
        "b": 1, "byte": 1, "bytes": 1,
        "k": 1024, "kb": 1000, "kib": 1024,
        "m": 1024**2, "mb": 1_000_000, "mib": 1024**2,
        "g": 1024**3, "gb": 1_000_000_000, "gib": 1024**3,
    }[unit]
    return number * multiplier


def _assert_memory_bounds(**overrides):
    variables = {**DEFAULTS, **overrides}
    config = _render_config(**overrides)
    ceiling = config["ingester"]["wal"]["replay_memory_ceiling"]
    assert ceiling == variables["loki_wal_replay_memory_ceiling"]
    assert _bytes(ceiling) < _bytes(variables["loki_mem_limit"])
    assert _bytes(variables["loki_gomemlimit"]) < _bytes(variables["loki_mem_limit"])


def test_default_memory_limits_are_below_container_limit():
    _assert_memory_bounds()


def test_monitoring_memory_and_retention_override_renders():
    _assert_memory_bounds(loki_mem_limit="768m", loki_gomemlimit="600MiB", loki_retention_days=14)
    config = _render_config(loki_mem_limit="768m", loki_gomemlimit="600MiB", loki_retention_days=14)
    assert config["limits_config"]["retention_period"] == "14d"


def test_compose_uses_role_memory_variables_and_keeps_grafana_cap():
    services = _render_compose(loki_mem_limit="768m", loki_gomemlimit="600MiB")["services"]
    assert services["loki"]["mem_limit"] == "768m"
    assert services["loki"]["environment"]["GOMEMLIMIT"] == "600MiB"
    assert services["grafana"]["mem_limit"] == "256m"


def test_config_retains_existing_keys_and_renders_wal_and_stream_limits():
    config = _render_config()
    # Existing top-level sections and limits remain present alongside ingester.wal.
    assert set(config) == {"auth_enabled", "server", "common", "schema_config", "ingester", "limits_config", "compactor"}
    assert config["auth_enabled"] is False
    assert config["server"] == {"http_listen_port": 3100, "grpc_listen_port": 9096, "log_level": "warn"}
    assert config["schema_config"]["configs"][0]["schema"] == "v13"
    assert config["limits_config"]["ingestion_rate_mb"] == DEFAULTS["loki_ingestion_rate_mb"]
    assert config["limits_config"]["ingestion_burst_size_mb"] == DEFAULTS["loki_ingestion_burst_size_mb"]
    assert config["limits_config"]["per_stream_rate_limit"] == DEFAULTS["loki_per_stream_rate_limit"]
    assert config["limits_config"]["per_stream_rate_limit_burst"] == DEFAULTS["loki_per_stream_rate_limit_burst"]
    assert config["ingester"]["wal"] == {
        "enabled": True,
        "dir": "/loki/wal",
        "replay_memory_ceiling": DEFAULTS["loki_wal_replay_memory_ceiling"],
        "flush_on_shutdown": True,
    }


def test_wal_discard_is_false_by_default_and_guarded_before_pull_and_up():
    assert DEFAULTS["loki_wal_discard"] is False
    tasks = yaml.safe_load(TASKS_FILE.read_text(encoding="utf-8"))
    deploy = tasks[0]["block"]
    names = [task.get("name") for task in deploy]
    stat_index = names.index("Check whether the WAL has already been discarded")
    discard_index = names.index("Discard Loki WAL once when explicitly requested")
    pull_index = names.index("Pull Loki + Grafana images")
    up_index = names.index("Start / update Loki + Grafana stack")
    assert stat_index < discard_index < pull_index < up_index
    stat_task = deploy[stat_index]
    assert stat_task["ansible.builtin.stat"]["path"] == "{{ loki_base_dir }}/.wal-discarded"
    discard = deploy[discard_index]
    assert discard["when"] == ["loki_wal_discard | bool", "not _loki_wal_discard_marker.stat.exists"]
    nested = discard["block"]
    serialized = yaml.safe_dump(nested)
    assert "docker compose stop loki" in serialized
    assert 'find "{{ loki_data_dir }}/wal" -mindepth 1 -delete' in serialized
    assert "{{ loki_data_dir }}/wal" in serialized
    assert "{{ loki_base_dir }}/.wal-discarded" in serialized
    data_subdirs = re.findall(r"loki_data_dir\s*}}/([A-Za-z0-9_-]+)", serialized)
    assert set(data_subdirs) == {"wal"}
    assert any(task.get("ansible.builtin.file", {}).get("mode") == "0755" for task in nested)
    marker = next(task["ansible.builtin.copy"] for task in nested if task.get("name") == "Record one-time WAL discard marker")
    assert marker["owner"] == marker["group"] == "root"
    assert marker["mode"] == "0644"
    assert "ansible_date_time.iso8601" in marker["content"]


def test_wal_discard_stops_and_verifies_loki_before_removing_and_keeps_ownership():
    tasks = yaml.safe_load(TASKS_FILE.read_text(encoding="utf-8"))
    discard = next(t for t in tasks[0]["block"] if t.get("name") == "Discard Loki WAL once when explicitly requested")
    nested = discard["block"]
    names = [task["name"] for task in nested]
    stop = names.index("Stop Loki before discarding its WAL")
    verify = names.index("Assert Loki is not running before the WAL is touched")
    remove = names.index("Remove contents of Loki WAL directory only")
    recreate = names.index("Recreate Loki WAL directory with container ownership")
    marker = names.index("Record one-time WAL discard marker")
    assert stop < verify < remove < recreate < marker
    check = nested[verify]
    assert "status=running" in check["ansible.builtin.command"]["cmd"]
    assert "stdout" in check["failed_when"] and "rc != 0" in check["failed_when"]
    removal = nested[remove]["ansible.builtin.command"]
    assert removal["removes"] == "{{ loki_data_dir }}/wal"  # a missing WAL directory skips, it does not abort
    wal_dir = nested[recreate]["ansible.builtin.file"]
    assert wal_dir["owner"] == "{{ loki_uid }}" and wal_dir["group"] == "{{ loki_uid }}"
    assert wal_dir["state"] == "directory" and wal_dir["path"] == "{{ loki_data_dir }}/wal"


def test_ceiling_and_stream_limits_follow_the_variables():
    config = _render_config(
        loki_wal_replay_memory_ceiling="123MB",
        loki_per_stream_rate_limit="9MB",
        loki_per_stream_rate_limit_burst="11MB",
    )
    assert config["ingester"]["wal"]["replay_memory_ceiling"] == "123MB"
    assert config["limits_config"]["per_stream_rate_limit"] == "9MB"
    assert config["limits_config"]["per_stream_rate_limit_burst"] == "11MB"
