"""S286 monitor-data boundary tests using the existing sync paths and fake APIs."""

import copy

import pytest
import yaml

import kuma_sync


class RecordingApi:
    def __init__(self):
        self.events = []
        self.added = []
        self.edited = []
        self.deleted = []
        self.disconnected = False

    def get_monitors(self):
        self.events.append("get_monitors")
        return []

    def get_notifications(self):
        self.events.append("get_notifications")
        return [
            {"name": "alert-email", "id": 11},
            {"name": "claude-relay-test", "id": 22},
        ]

    def add_monitor(self, **kwargs):
        self.events.append("add_monitor")
        self.added.append(copy.deepcopy(kwargs))

    def edit_monitor(self, monitor_id, **kwargs):
        self.events.append("edit_monitor")
        self.edited.append((monitor_id, copy.deepcopy(kwargs)))

    def delete_monitor(self, monitor_id):
        self.events.append("delete_monitor")
        self.deleted.append(monitor_id)

    def disconnect(self):
        self.disconnected = True


def _write_monitor_config(path, specs):
    path.write_text(
        yaml.safe_dump({"monitors": specs}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def _block_expansion(monkeypatch):
    def unexpected_expansion(_value):
        raise AssertionError("monitor data must never call expandvars")

    monkeypatch.setattr(kuma_sync.os.path, "expandvars", unexpected_expansion)


def _single(api, config_path, *, prune=False):
    kuma_sync.sync_monitors(
        api,
        config_path=str(config_path),
        prune=prune,
        notifications_config_path=str(config_path.parent / "notifications.yml"),
    )


def test_single_sync_preserves_literal_urls_headers_notifications_and_retry_defaults(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("KUMA_SYNC_BUDGET_SECONDS", "0")
    _block_expansion(monkeypatch)
    specs = [
        {
            "name": "café-http",
            "type": "http",
            "url": "https://svc.internal:8443/a%2Fb?q=one%20two",
            "headers": {"X-Label": "München", "X-Trace": "literal $ punctuation"},
            "body": "status = 'ready'",
            "notification_name": "alert-email",
        },
        {
            "name": "ipv6-relay",
            "type": "http",
            "url": "http://[fd00::12]:8080/health?region=tz",
            "notification_name": "claude-relay-test",
        },
        {
            "name": "ping-port",
            "type": "ping",
            "hostname": "worker.internal.",
            "port": "00022",
        },
    ]
    config = _write_monitor_config(tmp_path / "monitor.yml", specs)
    api = RecordingApi()

    _single(api, config)

    assert api.added == [
        {
            "name": "café-http",
            "type": "http",
            "url": "https://svc.internal:8443/a%2Fb?q=one%20two",
            "interval": 60,
            "maxretries": 3,
            "retryInterval": 60,
            "headers": {"X-Label": "München", "X-Trace": "literal $ punctuation"},
            "body": "status = 'ready'",
            "notificationIDList": [11],
        },
        {
            "name": "ipv6-relay",
            "type": "http",
            "url": "http://[fd00::12]:8080/health?region=tz",
            "interval": 60,
            "maxretries": 0,
            "retryInterval": 60,
            "notificationIDList": [22],
        },
        {
            "name": "ping-port",
            "type": "ping",
            "interval": 60,
            "maxretries": 0,
            "retryInterval": 60,
            "hostname": "worker.internal.",
            "port": 22,
            "notificationIDList": [],
        },
    ]


@pytest.mark.parametrize(
    "field,value,valid",
    [
        ("name", "$KUMA_POLICY_SENTINEL", "literal-name"),
        ("url", "https://${KUMA_POLICY_SENTINEL}/health", "https://literal.internal/health"),
        ("hostname", "$KUMA_POLICY_SENTINEL", "literal.internal"),
        ("headers", {"X-Policy": {"nested": "${KUMA_POLICY_SENTINEL}"}}, {"X-Policy": {"nested": "literal"}}),
        ("body", ["plain", {"nested": "$KUMA_POLICY_SENTINEL"}], ["plain", {"nested": "literal"}]),
        ("notification_name", "${KUMA_POLICY_SENTINEL}", "alert-email"),
        ("keyword", "unfinished ${", "literal keyword"),
        ("${KUMA_POLICY_SENTINEL}", "key value", "literal-key"),
    ],
    ids=["name", "url", "hostname", "header", "body", "notification", "malformed", "key"],
)
def test_single_sync_rejects_environment_templates_before_any_api_read(
    tmp_path, monkeypatch, capsys, field, value, valid
):
    monkeypatch.setenv("KUMA_SYNC_BUDGET_SECONDS", "0")
    monkeypatch.setenv("KUMA_POLICY_SENTINEL", "synthetic-policy-value")
    _block_expansion(monkeypatch)
    spec = {"name": "safe-name", "url": "https://safe.example/health"}
    if field == "name":
        spec[field] = value
    elif field.startswith("$"):
        spec[field] = value
    else:
        spec[field] = value
    config = _write_monitor_config(tmp_path / "monitor.yml", [spec])
    api = RecordingApi()

    with pytest.raises(SystemExit) as result:
        _single(api, config, prune=True)

    assert result.value.code == 1
    assert api.events == []
    assert api.added == [] and api.edited == [] and api.deleted == []
    error = capsys.readouterr().err
    assert error.strip() == kuma_sync._MONITOR_DATA_ERROR
    assert "KUMA_POLICY_SENTINEL" not in error
    assert "synthetic-policy-value" not in error

    # A matching literal in the same field must pass the actual sync, not a
    # validator-only control. This also pins the non-forwarded metadata keys.
    if field.startswith("$"):
        del spec[field]
        spec[valid] = value
    else:
        spec[field] = valid
    _write_monitor_config(config, [spec])
    positive = RecordingApi()
    _single(positive, config)
    assert len(positive.added) == 1
    expected_field = {"notification_name": "notificationIDList"}.get(field, field)
    if not field.startswith("$"):
        assert positive.added[0][expected_field] == ([11] if field == "notification_name" else valid)


@pytest.mark.parametrize(
    "url",
    [
        "javascript://safe.example/path",
        "/relative/path",
        "https://user:pass@safe.example/path",
        "https://safe.example:99999/path",
        "https://safe.example:/path",
        "https://safe.example/path\r\nHeader: value",
        " https://safe.example/path",
        "https://safe.example/path ",
    ],
    ids=["scheme", "relative", "userinfo", "port-range", "empty-port", "control", "leading-space", "trailing-space"],
)
def test_single_sync_rejects_invalid_urls_before_api_reads(tmp_path, monkeypatch, url):
    monkeypatch.setenv("KUMA_SYNC_BUDGET_SECONDS", "0")
    config = _write_monitor_config(
        tmp_path / "monitor.yml", [{"name": "safe-name", "url": url}]
    )
    api = RecordingApi()

    with pytest.raises(SystemExit):
        _single(api, config, prune=True)

    assert api.events == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("hostname", "host/path"),
        ("hostname", "host name"),
        ("hostname", "user@host"),
        ("hostname", "bad..host"),
        ("port", True),
        ("port", 65536),
        ("port", "22\n"),
        ("port", "999999999999999999999999999999999999"),
        ("port", 22.5),
    ],
    ids=["host-path", "host-space", "host-userinfo", "host-label", "bool-port", "high-port", "newline-port", "long-port", "float-port"],
)
def test_single_sync_rejects_invalid_host_and_port_scalars(tmp_path, monkeypatch, field, value):
    monkeypatch.setenv("KUMA_SYNC_BUDGET_SECONDS", "0")
    spec = {"name": "safe-name", "type": "ping", "hostname": "worker.internal"}
    spec[field] = value
    config = _write_monitor_config(tmp_path / "monitor.yml", [spec])
    api = RecordingApi()

    with pytest.raises(SystemExit):
        _single(api, config, prune=True)

    assert api.events == []


@pytest.mark.parametrize("body", ["cycle", "deep", "large", "nonfinite"], ids=["cycle", "depth", "size", "nonfinite"])
def test_single_sync_rejects_recursive_oversized_and_nonfinite_yaml_data(
    tmp_path, monkeypatch, body
):
    monkeypatch.setenv("KUMA_SYNC_BUDGET_SECONDS", "0")
    config = tmp_path / "monitor.yml"
    if body == "cycle":
        config.write_text(
            "monitors:\n  - name: recursive\n    url: https://safe.example/\n"
            "    body: &loop [*loop]\n",
            encoding="utf-8",
        )
    else:
        if body == "deep":
            value = "leaf"
            for _ in range(kuma_sync._MONITOR_DATA_MAX_DEPTH + 2):
                value = [value]
        elif body == "large":
            value = "x" * (kuma_sync._MONITOR_DATA_MAX_TEXT_BYTES + 1)
        else:
            value = float("nan")
        _write_monitor_config(
            config,
            [{"name": "bounded", "url": "https://safe.example/", "body": value}],
        )
    api = RecordingApi()

    with pytest.raises(SystemExit):
        _single(api, config, prune=True)

    assert api.events == []


def test_single_sync_detaches_project_derived_specs_before_resolution_or_building(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("KUMA_SYNC_BUDGET_SECONDS", "0")
    source_specs = [
        {
            "name": "detached",
            "url": "https://safe.example/",
            "notification_name": "alert-email",
            "headers": {"X-Label": ["Unicode: Zürich"]},
        }
    ]
    original = copy.deepcopy(source_specs)
    project_yaml = tmp_path / "project.yaml"
    project_yaml.write_text("project_name: detached\n", encoding="utf-8")
    monkeypatch.setattr(kuma_sync, "monitors_from_project_yaml", lambda _path: source_specs)
    api = RecordingApi()

    kuma_sync.sync_monitors(
        api,
        project_yaml_path=str(project_yaml),
        notifications_config_path=str(tmp_path / "notifications.yml"),
    )

    assert source_specs == original
    assert "_notifies_human_ungated" not in source_specs[0]
    assert "notification_ids" not in source_specs[0]
    assert api.added[0]["notificationIDList"] == [11]


def test_single_sync_validates_a_late_bad_row_before_any_monitor_mutation(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMA_SYNC_BUDGET_SECONDS", "0")
    config = _write_monitor_config(
        tmp_path / "monitor.yml",
        [
            {"name": "would-have-been-created", "url": "https://safe.example/"},
            {"name": "bad-late-row", "url": "https://bad.example/$KUMA_POLICY_SENTINEL"},
        ],
    )
    api = RecordingApi()

    with pytest.raises(SystemExit):
        _single(api, config, prune=True)

    assert api.events == []
    assert api.added == [] and api.edited == [] and api.deleted == []


def test_single_sync_rejects_unsupported_yaml_runtime_objects_before_api_reads(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("KUMA_SYNC_BUDGET_SECONDS", "0")
    project_yaml = tmp_path / "project.yaml"
    project_yaml.write_text("project_name: object-check\n", encoding="utf-8")
    monkeypatch.setattr(
        kuma_sync,
        "monitors_from_project_yaml",
        lambda _path: [{"name": "unsupported", "url": "https://safe.example/", "body": object()}],
    )
    api = RecordingApi()

    with pytest.raises(SystemExit):
        kuma_sync.sync_monitors(
            api,
            project_yaml_path=str(project_yaml),
            prune=True,
            notifications_config_path=str(tmp_path / "notifications.yml"),
        )

    assert api.events == []


def _write_project(path, extra):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "project_name": path.parent.name,
                "monitoring": {"skip_auto": True, "extra": extra},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def test_multi_sync_continues_valid_project_and_skips_prune_for_bad_project(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("KUMA_SYNC_BUDGET_SECONDS", "0")
    monkeypatch.setenv("KUMA_POLICY_SENTINEL", "synthetic-policy-value")
    _block_expansion(monkeypatch)
    notifications = tmp_path / "notifications.yml"
    notifications.write_text("notifications: []\n", encoding="utf-8")
    good_path = _write_project(
        tmp_path / "good" / "project.yaml",
        [{"name": "good-health", "url": "https://good.internal/health"}],
    )
    bad_path = _write_project(
        tmp_path / "bad" / "project.yaml",
        [
            {"name": "bad-before-late-error", "url": "https://bad.internal/"},
            {"name": "bad", "url": "https://${KUMA_POLICY_SENTINEL}/health"},
        ],
    )
    api = RecordingApi()
    monkeypatch.setattr(kuma_sync, "_login", lambda: api)

    with pytest.raises(SystemExit) as result:
        kuma_sync.sync_monitors_multi(
            [good_path, bad_path],
            prune=True,
            notifications_config_path=str(notifications),
        )

    assert result.value.code == 1
    assert [entry["name"] for entry in api.added] == ["good-health"]
    assert api.events.count("get_monitors") == 1
    assert "delete_monitor" not in api.events
    assert api.disconnected is True
    output = capsys.readouterr()
    assert kuma_sync._MONITOR_DATA_ERROR in output.err
    assert "KUMA_POLICY_SENTINEL" not in output.err
    assert "synthetic-policy-value" not in output.err
    assert "Prune skipped" in output.err


@pytest.mark.parametrize("template", ["$1", "$123", "${1}", "$9suffix"],
                         ids=["positional", "digits", "braced-digit", "digit-prefix"])
def test_numeric_shell_references_refuse_before_monitor_reads(tmp_path, monkeypatch, template):
    monkeypatch.setenv("KUMA_SYNC_BUDGET_SECONDS", "0")
    _block_expansion(monkeypatch)
    config = _write_monitor_config(tmp_path / "monitor.yml", [{
        "name": "numeric-reference", "url": "https://literal.internal/", "keyword": template,
    }])
    api = RecordingApi()
    with pytest.raises(SystemExit):
        _single(api, config, prune=True)
    assert api.events == []
    _write_monitor_config(config, [{
        "name": "numeric-reference", "url": "https://literal.internal/", "keyword": "price $ 12",
    }])
    _single(api, config)
    assert api.added[0]["keyword"] == "price $ 12"


def test_literal_internal_service_underscores_preserve_old_destinations(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMA_SYNC_BUDGET_SECONDS", "0")
    _block_expansion(monkeypatch)
    config = _write_monitor_config(tmp_path / "monitor.yml", [
        {"name": "service-url", "url": "https://service_name.internal:8443/health"},
        {"name": "service-port", "type": "port", "hostname": "_service.internal", "port": 443},
    ])
    api = RecordingApi()
    _single(api, config)
    assert len(api.added) == 2
    assert api.added[0]["url"] == "https://service_name.internal:8443/health"
    assert api.added[1]["hostname"] == "_service.internal"
    assert api.added[1]["port"] == 443


def test_multi_sync_literal_success_disconnects(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMA_SYNC_BUDGET_SECONDS", "0")
    notifications = tmp_path / "notifications.yml"
    notifications.write_text("notifications: []\n", encoding="utf-8")
    path = _write_project(
        tmp_path / "literal" / "project.yaml",
        [{"name": "literal", "url": "https://literal.internal/health"}],
    )
    api = RecordingApi()
    monkeypatch.setattr(kuma_sync, "_login", lambda: api)

    kuma_sync.sync_monitors_multi(
        [path], notifications_config_path=str(notifications)
    )

    assert [entry["name"] for entry in api.added] == ["literal"]
    assert api.disconnected is True


def test_trusted_notification_config_still_expands_synthetic_env_values(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMA_TEST_ONLY_SMTP_PORT", "2465")
    monkeypatch.setenv("KUMA_TEST_ONLY_SMTP_SECURE", "true")
    config = tmp_path / "synthetic-notifications.yml"
    config.write_text(
        "notifications:\n"
        "  - name: synthetic-smtp\n"
        "    type: smtp\n"
        "    config:\n"
        "      smtpPort: \"${KUMA_TEST_ONLY_SMTP_PORT}\"\n"
        "      smtpSecure: \"${KUMA_TEST_ONLY_SMTP_SECURE}\"\n",
        encoding="utf-8",
    )

    class NotificationApi:
        def get_notifications(self):
            return []

        def add_notification(self, **kwargs):
            self.added = kwargs

    api = NotificationApi()
    kuma_sync.sync_notifications(api, config)

    assert api.added["smtpPort"] == 2465
    assert api.added["smtpPort"].__class__ is int
    assert api.added["smtpSecure"] is True


def test_long_zero_padded_port_reaches_actual_api_as_integer(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMA_SYNC_BUDGET_SECONDS", "0")
    config = _write_monitor_config(tmp_path / "monitor.yml", [{
        "name": "padded-port", "type": "port", "hostname": "192.168.1.9",
        "port": "0" * 5000 + "443",
    }])
    api = RecordingApi()
    _single(api, config)
    assert len(api.added) == 1
    assert api.added[0]["hostname"] == "192.168.1.9"
    assert type(api.added[0]["port"]) is int
    assert api.added[0]["port"] == 443


def test_valid_project_derivation_preserves_update_idempotence_and_pruning(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMA_SYNC_BUDGET_SECONDS", "0")
    _block_expansion(monkeypatch)
    project = tmp_path / "project.yaml"
    project.write_text(yaml.safe_dump({
        "project_name": "derived",
        "environments": {
            "production": {"domains": ["prod.internal"], "server": "test"},
            "staging": {"domains": ["stage.internal"]},
        },
    }), encoding="utf-8")
    api = RecordingApi()
    kuma_sync.sync_monitors(api, project_yaml_path=str(project),
                            notifications_config_path=str(tmp_path / "notifications.yml"))
    expected = copy.deepcopy(api.added)
    assert [(row["name"], row["url"]) for row in expected] == [
        ("derived-frontend", "https://prod.internal"),
        ("derived-healthz", "https://prod.internal/api/healthz"),
        ("derived-staging-frontend", "https://stage.internal"),
        ("derived-staging-healthz", "https://stage.internal/api/healthz"),
    ]
    assert [row["notificationIDList"] for row in expected] == [[22], [22], [], []]
    existing = [dict(row, id=index + 1) for index, row in enumerate(expected)]
    existing[1]["url"] = "https://old.internal/"
    existing.append({"name": "stale", "id": 99})
    api.added.clear()
    api.get_monitors = lambda: copy.deepcopy(existing)
    kuma_sync.sync_monitors(api, project_yaml_path=str(project), prune=True,
                            notifications_config_path=str(tmp_path / "notifications.yml"))
    assert api.added == []
    assert api.edited == [(2, expected[1])]
    assert api.deleted == [99]


@pytest.mark.parametrize("bad_value", ["${KUMA_POLICY_SENTINEL}", "https://user@bad.internal/"],
                         ids=["environment", "userinfo"])
def test_multi_bad_first_app_never_mutates_and_later_valid_app_still_runs(
    tmp_path, monkeypatch, bad_value,
):
    monkeypatch.setenv("KUMA_SYNC_BUDGET_SECONDS", "0")
    _block_expansion(monkeypatch)
    bad = _write_project(tmp_path / "bad" / "project.yaml", [
        {"name": "bad", "url": bad_value},
    ])
    good = _write_project(tmp_path / "good" / "project.yaml", [
        {"name": "ipv6", "url": "https://[fd00::2]:65535/health"},
    ])
    notifications = tmp_path / "notifications.yml"
    notifications.write_text("notifications: []\n", encoding="utf-8")
    api = RecordingApi()
    monkeypatch.setattr(kuma_sync, "_login", lambda: api)
    with pytest.raises(SystemExit) as result:
        kuma_sync.sync_monitors_multi([bad, good], prune=True,
                                     notifications_config_path=str(notifications))
    assert result.value.code == 1
    assert [row["name"] for row in api.added] == ["ipv6"]
    assert api.added[0]["url"] == "https://[fd00::2]:65535/health"
    assert api.deleted == [] and api.edited == []
    assert api.disconnected is True
