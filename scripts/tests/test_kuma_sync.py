"""CI-4 — tests for kuma_sync's change detection, retry, and run budget.

`scripts/kuma_sync.py` had no tests at all. `_monitor_changed` is pure logic over
two dicts and is the highest-value thing here to pin down: when it is wrong in
one direction every run rewrites all ~57 monitors against Kuma's single-threaded
Socket.IO server; when it is wrong in the other direction real drift silently
stops being corrected.

Run: pytest scripts/tests/
"""

import pytest

import kuma_sync
from conftest import MonitorType


def base_kwargs(**overrides):
    kwargs = {
        "name": "hram-frontend",
        "type": "http",
        "url": "https://hram.ch",
        "interval": 60,
        "maxretries": 3,
        "retryInterval": 60,
    }
    kwargs.update(overrides)
    return kwargs


def existing_from(kwargs, **overrides):
    """An 'unchanged' Kuma monitor: same values, but `type` echoed as the enum
    the client library converts it to on read."""
    monitor = dict(kwargs)
    monitor["type"] = MonitorType(kwargs["type"])
    monitor["id"] = 42
    monitor.update(overrides)
    return monitor


# --- the root cause -------------------------------------------------------

def test_enum_type_from_kuma_is_not_a_change():
    """THE regression. `str(MonitorType.HTTP)` is 'MonitorType.HTTP', so the old
    comparison reported every monitor as changed on every run — 0 `unchanged`
    ever observed, in either run of the CI-4 evidence."""
    kwargs = base_kwargs()
    assert kuma_sync._monitor_changed(kwargs, existing_from(kwargs)) is False


def test_str_of_the_enum_really_would_have_differed():
    """Guards the test above from becoming tautological: assert the enum's
    str() genuinely does not equal the desired value, i.e. the bug was real."""
    assert str(MonitorType.HTTP) != "http"
    assert MonitorType.HTTP.value == "http"


def test_a_genuinely_different_type_is_still_a_change():
    kwargs = base_kwargs(type="http")
    existing = existing_from(kwargs, type=MonitorType.PING)
    assert kuma_sync._monitor_changed(kwargs, existing) is True


def test_plain_string_type_from_kuma_still_compares_equal():
    """The fork may return a plain string rather than an enum; both must work."""
    kwargs = base_kwargs()
    existing = dict(kwargs)
    assert kuma_sync._monitor_changed(kwargs, existing) is False


# --- notificationIDList ---------------------------------------------------

@pytest.mark.parametrize("current", [[3], ["3"], {"3": True}, {3: True}])
def test_notification_id_list_equal_forms_are_unchanged(current):
    kwargs = base_kwargs(notificationIDList=[3])
    assert kuma_sync._monitor_changed(kwargs, existing_from(kwargs, notificationIDList=current)) is False


def test_notification_id_list_different_id_set_is_a_change():
    kwargs = base_kwargs(notificationIDList=[3])
    existing = existing_from(kwargs, notificationIDList=[4])
    assert kuma_sync._monitor_changed(kwargs, existing) is True


def test_notification_unassigned_in_kuma_is_a_change():
    """`{"3": false}` means notification 3 is NOT assigned. Treating it as
    assigned would stop detecting someone un-assigning a relay in the Kuma UI —
    exactly the drift this sync exists to correct (CI-4 Risk 1)."""
    kwargs = base_kwargs(notificationIDList=[3])
    existing = existing_from(kwargs, notificationIDList={"3": False})
    assert kuma_sync._monitor_changed(kwargs, existing) is True


def test_notification_missing_from_kuma_is_a_change():
    kwargs = base_kwargs(notificationIDList=[3])
    existing = existing_from(kwargs)
    existing.pop("notificationIDList", None)
    assert kuma_sync._monitor_changed(kwargs, existing) is True


# --- pre-existing behaviour that must survive -----------------------------

def test_int_coercion_still_treats_string_numbers_as_equal():
    kwargs = base_kwargs(interval=60)
    assert kuma_sync._monitor_changed(kwargs, existing_from(kwargs, interval="60")) is False


def test_int_change_is_detected():
    kwargs = base_kwargs(interval=60)
    assert kuma_sync._monitor_changed(kwargs, existing_from(kwargs, interval=120)) is True


def test_non_numeric_where_an_int_is_expected_is_a_change():
    kwargs = base_kwargs(interval=60)
    assert kuma_sync._monitor_changed(kwargs, existing_from(kwargs, interval="soon")) is True


def test_accepted_statuscodes_order_insensitive_but_content_sensitive():
    kwargs = base_kwargs(accepted_statuscodes=["200", "201"])
    unchanged = existing_from(kwargs, accepted_statuscodes=["201", "200"])
    changed = existing_from(kwargs, accepted_statuscodes=["200"])
    assert kuma_sync._monitor_changed(kwargs, unchanged) is False
    assert kuma_sync._monitor_changed(kwargs, changed) is True


def test_absent_and_none_are_treated_as_empty_string():
    kwargs = base_kwargs(keyword="")
    existing = existing_from(kwargs)
    existing.pop("keyword", None)
    assert kuma_sync._monitor_changed(kwargs, existing) is False


@pytest.mark.parametrize(
    "desired,current,changed",
    [(False, 0, False), (True, 1, False), (True, 0, True), (False, 1, True)],
)
def test_boolean_values_go_through_the_int_branch(desired, current, changed):
    """A YAML value CAN arrive as a Python bool: PyYAML resolves an unquoted
    `keyword: off` in `monitoring.extra` to False. `bool` is an `int` subclass,
    so it lands in the int branch — the behaviour that predates this WO.

    A dedicated bool branch was added during CI-4 and then removed, because it
    silently changed that behaviour (`bool("true")` is True where `int("true")`
    raises). This pins the surviving semantics so the branch is not reintroduced
    on the assumption that bools never occur.
    """
    kwargs = base_kwargs(keyword=desired)
    existing = existing_from(kwargs, keyword=current)
    assert kuma_sync._monitor_changed(kwargs, existing) is changed


def test_yaml_really_can_produce_a_bool_for_a_whitelisted_key():
    """Guards the rationale above from drifting back to 'bools never occur'."""
    import yaml
    assert yaml.safe_load("keyword: off")["keyword"] is False


def test_a_real_url_change_is_detected():
    kwargs = base_kwargs()
    assert kuma_sync._monitor_changed(kwargs, existing_from(kwargs, url="https://other.ch")) is True


def test_the_differing_key_is_logged(capsys):
    """Scope 0: the run log must name the key and both values, so the next
    permanent diff is answered by reading the log, not the source."""
    kwargs = base_kwargs()
    kuma_sync._monitor_changed(kwargs, existing_from(kwargs, url="https://other.ch"), "hram-frontend")
    err = capsys.readouterr().err
    assert "hram-frontend" in err and "url" in err and "other.ch" in err


# --- retry / reconnect ----------------------------------------------------

class FakeApi:
    def __init__(self, fail_times=0, exc=None):
        self.fail_times = fail_times
        self.calls = 0
        self.exc = exc or RuntimeError("timeout")

    def get_monitors(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc
        return [{"name": "ok"}]


class FakeSession:
    def __init__(self, apis, budget=None):
        self._apis = list(apis)
        self.api = self._apis.pop(0)
        self.reconnects = 0
        self.budget = budget or kuma_sync.Budget(seconds=0)

    def reconnect(self):
        self.reconnects += 1
        if self._apis:
            self.api = self._apis.pop(0)
        return self.api


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(kuma_sync.time, "sleep", lambda _s: None)


def test_retry_reconnects_between_attempts_and_uses_the_new_connection():
    """The old code retried a BOUND method of the dead api object, so once the
    socket wedged every remaining attempt was a guaranteed failure."""
    dead, fresh = FakeApi(fail_times=99), FakeApi(fail_times=0)
    session = FakeSession([dead, fresh])

    assert kuma_sync._retry_call("get_monitors", session, "get_monitors") == [{"name": "ok"}]
    assert session.reconnects == 1
    assert fresh.calls == 1


def test_retry_gives_up_and_reraises_the_last_exception():
    session = FakeSession([FakeApi(fail_times=99) for _ in range(6)])
    with pytest.raises(RuntimeError):
        kuma_sync._retry_call("get_monitors", session, "get_monitors")
    assert session.reconnects == 4  # 5 attempts => 4 reconnects between them


@pytest.mark.parametrize("message", ["monitor not found", "permission denied"])
def test_retry_fast_paths_unrecoverable_errors_without_reconnecting(message):
    session = FakeSession([FakeApi(fail_times=99, exc=RuntimeError(message))])
    with pytest.raises(RuntimeError):
        kuma_sync._retry_call("get_monitors", session, "get_monitors")
    assert session.reconnects == 0


# --- run budget -----------------------------------------------------------

def test_budget_check_is_a_noop_while_inside_the_ceiling():
    kuma_sync.Budget(seconds=600).check("somewhere")


def test_budget_raises_with_a_clear_message_once_exhausted(monkeypatch):
    budget = kuma_sync.Budget(seconds=1)
    monkeypatch.setattr(budget, "elapsed", lambda: 99.0)
    with pytest.raises(kuma_sync.BudgetExceeded) as exc:
        budget.check("edit_monitor(hram-frontend)")
    message = str(exc.value)
    assert "edit_monitor(hram-frontend)" in message and "1s" in message


def test_budget_of_zero_disables_the_ceiling(monkeypatch):
    budget = kuma_sync.Budget(seconds=0)
    monkeypatch.setattr(budget, "elapsed", lambda: 10_000.0)
    budget.check("anywhere")


def test_budget_default_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("KUMA_SYNC_BUDGET_SECONDS", "42")
    assert kuma_sync.Budget().seconds == 42


def test_retry_aborts_on_budget_instead_of_sleeping_out_the_backoff():
    budget = kuma_sync.Budget(seconds=1)
    budget.elapsed = lambda: 99.0
    session = FakeSession([FakeApi(fail_times=99)], budget=budget)
    with pytest.raises(kuma_sync.BudgetExceeded):
        kuma_sync._retry_call("get_monitors", session, "get_monitors")
    assert session.reconnects == 0


# --- budget at the sync_monitors_multi level ------------------------------
#
# The unit tests above prove Budget and _retry_call behave; they do NOT prove
# the abort survives the generic `except Exception` handlers between the raise
# site and the top of the run. That gap hid a real bug: BudgetExceeded was being
# rewritten as a per-app failure and the run limped on through the remaining
# apps, exiting with "N app(s) failed" instead of the budget message.


class WedgedApi:
    """Every call fails, the way a wedged Socket.IO connection does."""

    def __getattr__(self, _name):
        def _boom(*_args, **_kwargs):
            raise RuntimeError("timeout")
        return _boom

    def disconnect(self):
        pass


def _project_yaml(tmp_path, name):
    path = tmp_path / name
    path.mkdir()
    (path / "project.yaml").write_text(
        "project_name: {n}\n"
        "environments:\n"
        "  production:\n"
        "    domains: [{n}.example.ch]\n".format(n=name),
        encoding="utf-8",
    )
    return str(path / "project.yaml")


@pytest.fixture
def wedged_kuma(monkeypatch):
    monkeypatch.setattr(kuma_sync, "_login", lambda: WedgedApi())
    monkeypatch.setenv("KUMA_SYNC_BUDGET_SECONDS", "0")


def test_multi_aborts_with_the_budget_message_not_a_per_app_failure(
    tmp_path, monkeypatch, capsys, wedged_kuma
):
    """R1: the abort must reach the top and produce the budget message."""
    apps = [_project_yaml(tmp_path, f"app{i}") for i in range(3)]

    # Budget already blown before the first call.
    real_budget = kuma_sync.Budget
    monkeypatch.setattr(
        kuma_sync, "Budget",
        lambda *a, **k: type("B", (real_budget,), {"exceeded": lambda self: True})(seconds=1),
    )

    with pytest.raises(SystemExit) as exc:
        kuma_sync.sync_monitors_multi(apps, prune=True)

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Aborted:" in err and "run budget" in err
    assert "app(s) failed" not in err, "budget abort was misreported as per-app failures"


def test_multi_budget_abort_skips_the_prune(tmp_path, monkeypatch, capsys, wedged_kuma):
    """An aborted run has an incomplete declared-name set; pruning against it
    would delete valid monitors — the same safety the failed_apps guard gives.

    The prune-skip alone is NOT enough to assert: a BudgetExceeded that is
    mis-caught as a per-app failure also lands in `failed_apps`, which already
    skips the prune via the pre-existing safety net. So this asserts the abort
    was *identified as a budget abort*, otherwise it would pass even with the
    guard removed and pin nothing.
    """
    apps = [_project_yaml(tmp_path, "app0")]
    real_budget = kuma_sync.Budget
    monkeypatch.setattr(
        kuma_sync, "Budget",
        lambda *a, **k: type("B", (real_budget,), {"exceeded": lambda self: True})(seconds=1),
    )

    with pytest.raises(SystemExit):
        kuma_sync.sync_monitors_multi(apps, prune=True)

    captured = capsys.readouterr()
    assert "Prune pass" not in captured.out
    assert "Aborted:" in captured.err and "run budget" in captured.err
    assert "app(s) failed" not in captured.err


def test_multi_budget_abort_survives_the_recovery_branch(tmp_path, monkeypatch, capsys):
    """The SECOND swallow site: the recovery branch's re-fetch of `existing`.

    Its guard was originally untested — removing it left the whole suite green.
    Arming the budget on a *reconnect* does not reach it, because `_retry_call`
    reconnects too, so the budget fires during the write's own retries and never
    gets as far as the recovery branch. Arming it on the SECOND `get_monitors`
    pins the re-fetch exactly: call 1 is the per-app fetch, call 2 can only be
    the recovery re-fetch.
    """
    monkeypatch.setattr(kuma_sync.time, "sleep", lambda _s: None)

    budget = kuma_sync.Budget(seconds=1)
    budget.armed = False
    monkeypatch.setattr(budget, "exceeded", lambda: budget.armed)
    monkeypatch.setattr(kuma_sync, "Budget", lambda *a, **k: budget)

    # Shared across reconnects: _login hands back a fresh object each time.
    state = {"get_calls": 0}

    class RecoveryPathApi:
        def get_monitors(self):
            state["get_calls"] += 1
            if state["get_calls"] == 1:
                return []      # empty -> the monitor takes the add_monitor path
            budget.armed = True   # the re-fetch is where the budget lands
            raise RuntimeError("timeout")

        def add_monitor(self, **_kwargs):
            raise RuntimeError("timeout")

        def disconnect(self):
            pass

    monkeypatch.setattr(kuma_sync, "_login", RecoveryPathApi)

    with pytest.raises(SystemExit) as exc:
        kuma_sync.sync_monitors_multi([_project_yaml(tmp_path, "app0")], prune=True)

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert state["get_calls"] >= 2, "the recovery re-fetch never ran — the guard is not reached"
    assert "Aborted:" in err and "run budget" in err
    assert "Recovery failed" not in err, "budget abort was swallowed as a recovery failure"
    assert "app(s) failed" not in err


def test_prune_checks_the_budget_before_each_delete(monkeypatch):
    """Without a proactive check the budget can only fire reactively, from
    inside a delete's retry — i.e. after a delete already failed — so an abort
    would stop at an arbitrary point mid-prune."""
    checked = []

    class RecordingBudget(kuma_sync.Budget):
        def check(self, where):
            checked.append(where)

    class PruneApi:
        def get_monitors(self):
            return [{"name": "stale-1", "id": 1}, {"name": "stale-2", "id": 2}]

        def delete_monitor(self, _id):
            return None

        def disconnect(self):
            pass

    monkeypatch.setattr(kuma_sync, "_login", PruneApi)
    monkeypatch.setattr(kuma_sync, "Budget", lambda *a, **k: RecordingBudget(seconds=600))

    kuma_sync.sync_monitors_multi([], prune=True)

    deletes = [w for w in checked if w.startswith("before delete_monitor(")]
    assert len(deletes) == 2, f"expected a check before each delete, got {checked}"


def test_multi_still_reports_ordinary_app_failures_normally(tmp_path, capsys, monkeypatch):
    """The guard must not swallow genuine per-app errors into the budget path."""
    monkeypatch.setattr(kuma_sync, "_login", lambda: WedgedApi())
    monkeypatch.setenv("KUMA_SYNC_BUDGET_SECONDS", "0")  # budget disabled
    monkeypatch.setattr(kuma_sync.time, "sleep", lambda _s: None)

    with pytest.raises(SystemExit) as exc:
        kuma_sync.sync_monitors_multi([_project_yaml(tmp_path, "app0")], prune=True)

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "app(s) failed" in err
    assert "Aborted:" not in err


# --- CI-5: default + relay notification resolution ------------------------
#
# Kuma does not apply a `default: true` notification to a monitor created
# through the socket API, so every monitor the sync created had an
# explicitly EMPTY notificationIDList. `_load_default_notification_names` +
# `_resolve_notification_ids` close that, resolving defaults from the
# declared notifications.yml (not Kuma's live isDefault — Risk 4: the
# declared config must stay the source of truth) and resolving
# `notification_name` identically on both the single-app and `multi` paths.


def test_load_default_notification_names_from_file(tmp_path):
    path = tmp_path / "notifications.yml"
    path.write_text(
        "notifications:\n"
        "  - name: discord-alerts\n"
        "    type: discord\n"
        "    default: true\n"
        "  - name: claude-relay-main-prod\n"
        "    type: webhook\n"
        "    default: false\n",
        encoding="utf-8",
    )
    assert kuma_sync._load_default_notification_names(path) == {"discord-alerts"}


def test_load_default_notification_names_missing_file_warns(tmp_path, capsys):
    missing = tmp_path / "does-not-exist.yml"
    assert kuma_sync._load_default_notification_names(missing) == set()
    err = capsys.readouterr().err
    assert "not found" in err


def test_resolve_notification_ids_attaches_defaults_with_no_relay():
    """A created monitor's kwargs must contain the default notification ids
    even when it declares no explicit relay (CI-5 scope 1)."""
    spec = {"name": "hram-frontend"}
    kuma_sync._resolve_notification_ids(spec, {}, [1, 2])
    assert spec["notification_ids"] == [1, 2]


def test_resolve_notification_ids_unions_default_and_relay():
    """With an explicit notification_name, kwargs contain BOTH the default
    ids and the resolved relay id (CI-5 scope 1)."""
    spec = {"name": "hram-frontend", "notification_name": "claude-relay-main-prod"}
    notifications_by_name = {"claude-relay-main-prod": 5}
    kuma_sync._resolve_notification_ids(spec, notifications_by_name, [1])
    assert spec["notification_ids"] == [1, 5]
    assert "notification_name" not in spec  # popped, not just read


def test_resolve_notification_ids_unresolvable_relay_keeps_defaults(capsys):
    """An unknown notification_name warns and does NOT silently produce an
    empty assignment — the declared defaults still land."""
    spec = {"name": "hram-frontend", "notification_name": "claude-relay-ghost"}
    kuma_sync._resolve_notification_ids(spec, {}, [1], monitor_name="hram-frontend")
    assert spec["notification_ids"] == [1]
    err = capsys.readouterr().err
    assert "hram-frontend" in err and "claude-relay-ghost" in err and "not found" in err


def test_resolve_notification_ids_no_defaults_no_relay_sets_empty_list():
    """CI-8: neither defaults nor an explicit notification_name declared at
    all -- this is a deliberate "no channel" statement (INF-9's routine
    monitors), so notification_ids IS set, to an empty list, so a stale
    Kuma-side attachment (e.g. from a channel that used to be default: true)
    gets cleared on the next sync rather than surviving forever."""
    spec = {"name": "hram-frontend"}
    kuma_sync._resolve_notification_ids(spec, {}, [])
    assert spec["notification_ids"] == []


def test_resolve_notification_ids_unresolvable_relay_and_no_defaults_protects_existing(capsys):
    """CI-5 property preserved under CI-8: an explicit notification_name WAS
    given but did not resolve, and there ARE no defaults either -- this
    reads as "probably broken" (typo, or notifications.yml misconfigured),
    not "deliberately empty", so notification_ids is NOT set at all and any
    existing Kuma-side assignment is left untouched."""
    spec = {"name": "hram-frontend", "notification_name": "claude-relay-ghost"}
    kuma_sync._resolve_notification_ids(spec, {}, [], monitor_name="hram-frontend")
    assert "notification_ids" not in spec
    err = capsys.readouterr().err
    assert "claude-relay-ghost" in err and "not found" in err


def test_resolve_notification_ids_list_resolves_all_entries_and_unions_defaults():
    """CI-6 item 1: a list of names resolves every entry, unioned with the
    declared defaults — the shape INF-9 needs (alert-email alongside an
    already-declared claude-relay-<server> on the same monitor)."""
    spec = {"name": "cockpit-frontend", "notification_name": ["claude-relay-main-prod", "alert-email"]}
    notifications_by_name = {"claude-relay-main-prod": 5, "alert-email": 7}
    kuma_sync._resolve_notification_ids(spec, notifications_by_name, [1])
    assert spec["notification_ids"] == [1, 5, 7]
    assert "notification_name" not in spec


def test_resolve_notification_ids_string_form_still_works_unchanged():
    """The singular string form must resolve byte-identical to before — the
    contract every existing caller (prod_relay in particular) relies on."""
    spec = {"name": "hram-frontend", "notification_name": "claude-relay-main-prod"}
    notifications_by_name = {"claude-relay-main-prod": 5}
    kuma_sync._resolve_notification_ids(spec, notifications_by_name, [1])
    assert spec["notification_ids"] == [1, 5]


def test_resolve_notification_ids_list_with_one_unknown_name_warns_and_keeps_rest(capsys):
    """A mixed list with one unresolvable name warns and skips only that
    entry — the others, and the defaults, still apply."""
    spec = {"name": "cockpit-frontend", "notification_name": ["claude-relay-main-prod", "ghost-channel"]}
    notifications_by_name = {"claude-relay-main-prod": 5}
    kuma_sync._resolve_notification_ids(spec, notifications_by_name, [1], monitor_name="cockpit-frontend")
    assert spec["notification_ids"] == [1, 5]
    err = capsys.readouterr().err
    assert "cockpit-frontend" in err and "ghost-channel" in err and "not found" in err


def test_resolve_notification_ids_list_with_no_defaults_still_non_empty():
    """A monitor with a list and no defaults still gets a non-empty
    assignment (CI-5 property extended to the list form)."""
    spec = {"name": "cockpit-healthz", "notification_name": ["alert-email"]}
    notifications_by_name = {"alert-email": 7}
    kuma_sync._resolve_notification_ids(spec, notifications_by_name, [])
    assert spec["notification_ids"] == [7]


def test_resolve_notification_ids_list_all_unresolvable_and_no_defaults_sets_nothing(capsys):
    """CI-5 property survives for the list form: if nothing resolves and
    there are no defaults, no notification_ids key is set at all — never an
    empty list masquerading as a real assignment."""
    spec = {"name": "hram-frontend", "notification_name": ["ghost-one", "ghost-two"]}
    kuma_sync._resolve_notification_ids(spec, {}, [], monitor_name="hram-frontend")
    assert "notification_ids" not in spec


def test_extra_entry_notification_name_flows_through_to_notification_id_list(tmp_path):
    """A `monitoring.extra` entry can declare a `notification_name` and it
    reaches `notificationIDList` (CI-5 scope 3) — no separate code path is
    needed once resolution is generic; `monitors_from_project_yaml` already
    passes arbitrary extra keys through."""
    project_yaml = tmp_path / "project.yaml"
    project_yaml.write_text(
        "project_name: infrastructure\n"
        "monitoring:\n"
        "  skip_auto: true\n"
        "  extra:\n"
        "    - name: main-prod-health-disk\n"
        "      url: http://main-prod.example.ts.net/health/disk\n"
        "      notification_name: claude-relay-main-prod\n",
        encoding="utf-8",
    )
    specs = kuma_sync.monitors_from_project_yaml(project_yaml)
    assert specs[0]["notification_name"] == "claude-relay-main-prod"

    notifications_by_name = {"claude-relay-main-prod": 9}
    kuma_sync._resolve_notification_ids(specs[0], notifications_by_name, [1])
    kwargs = kuma_sync._build_monitor_kwargs(specs[0])
    assert kwargs["notificationIDList"] == [1, 9]


# --- INF-59/INF-60: max_retries default keys off whether the monitor ------
# --- notifies a human with nothing downstream to filter it, NOT whether ---
# --- it merely carries any notification_name at all -----------------------


def test_build_monitor_kwargs_defaults_to_zero_retries_without_a_channel():
    spec = {"name": "no-channel", "url": "https://example.ch"}
    kwargs = kuma_sync._build_monitor_kwargs(spec)
    assert kwargs["maxretries"] == 0


def test_build_monitor_kwargs_keeps_the_ladder_with_a_channel():
    spec = {"name": "has-channel", "url": "https://example.ch", "_notifies_human_ungated": True}
    kwargs = kuma_sync._build_monitor_kwargs(spec)
    assert kwargs["maxretries"] == 3


def test_build_monitor_kwargs_explicit_max_retries_wins_either_way():
    channel_spec = {
        "name": "explicit-channelled", "url": "https://example.ch",
        "_notifies_human_ungated": True, "max_retries": 7,
    }
    no_channel_spec = {
        "name": "explicit-no-channel", "url": "https://example.ch", "max_retries": 7,
    }
    assert kuma_sync._build_monitor_kwargs(channel_spec)["maxretries"] == 7
    assert kuma_sync._build_monitor_kwargs(no_channel_spec)["maxretries"] == 7


def test_build_monitor_kwargs_pops_the_stash_so_it_never_reaches_the_api():
    spec = {"name": "no-channel", "url": "https://example.ch", "_notifies_human_ungated": False}
    kuma_sync._build_monitor_kwargs(spec)
    assert "_notifies_human_ungated" not in spec


# --- INF-60: the actual regression -- a machine channel (claude-relay-*,
# --- cockpit-webhook) must NOT count as "notifies a human ungated" --------


def test_notifies_human_ungated_true_for_alert_email():
    assert kuma_sync._notifies_human_ungated({"notification_name": "alert-email"}) is True


def test_notifies_human_ungated_false_for_a_machine_relay():
    """The actual INF-60 bug: claude-relay-<server> is a machine channel,
    not a person's inbox."""
    assert kuma_sync._notifies_human_ungated(
        {"notification_name": "claude-relay-main-prod"}
    ) is False


def test_notifies_human_ungated_false_for_cockpit_webhook():
    assert kuma_sync._notifies_human_ungated({"notification_name": "cockpit-webhook"}) is False


def test_notifies_human_ungated_false_with_no_declaration_at_all():
    assert kuma_sync._notifies_human_ungated({"name": "x"}) is False


def test_notifies_human_ungated_true_when_the_human_channel_is_one_of_several():
    """A monitor carrying both a machine relay and alert-email -- the human
    channel wins, list-form declaration (CI-6)."""
    spec = {"notification_name": ["claude-relay-main-prod", "alert-email"]}
    assert kuma_sync._notifies_human_ungated(spec) is True


def test_derived_production_monitor_with_only_claude_relay_resolves_to_zero_retries(tmp_path):
    """The actual fitness-monitor-shaped regression this WO fixes: a normal
    auto-derived production monitor, with prod_relay's claude-relay-<server>
    auto-attached and nothing else declared, must resolve to max_retries=0 --
    asserted against the real derivation's output (Risk 4), not a hand-built
    spec dict."""
    project_yaml = tmp_path / "project.yaml"
    project_yaml.write_text(
        "project_name: fitness-monitor\n"
        "environments:\n"
        "  production:\n"
        "    server: main-prod\n"
        "    domains:\n"
        "      - fitness-monitor.example.ch\n",
        encoding="utf-8",
    )
    specs = kuma_sync.monitors_from_project_yaml(project_yaml)
    frontend = next(s for s in specs if s["name"] == "fitness-monitor-frontend")
    assert frontend["notification_name"] == "claude-relay-main-prod"  # prod_relay, unchanged

    frontend["_notifies_human_ungated"] = kuma_sync._notifies_human_ungated(frontend)
    kwargs = kuma_sync._build_monitor_kwargs(dict(frontend))
    assert kwargs["maxretries"] == 0


def test_derived_production_monitor_with_alert_email_extra_override_keeps_the_ladder(tmp_path):
    """The INF-58 shape: a monitoring.extra entry naming alert-email
    explicitly (like main-prod-health-disk) must still resolve to 3, proven
    against the real derivation."""
    project_yaml = tmp_path / "project.yaml"
    project_yaml.write_text(
        "project_name: infrastructure\n"
        "monitoring:\n"
        "  skip_auto: true\n"
        "  extra:\n"
        "    - name: main-prod-health-disk\n"
        "      url: http://main-prod.example.ts.net/health/disk\n"
        "      notification_name: alert-email\n",
        encoding="utf-8",
    )
    specs = kuma_sync.monitors_from_project_yaml(project_yaml)
    spec = specs[0]
    spec["_notifies_human_ungated"] = kuma_sync._notifies_human_ungated(spec)
    kwargs = kuma_sync._build_monitor_kwargs(dict(spec))
    assert kwargs["maxretries"] == 3


def test_monitors_from_project_yaml_no_longer_injects_a_max_retries_default(tmp_path):
    """INF-59: the shared `base` dict must not set max_retries any more --
    that decision now belongs solely to _build_monitor_kwargs, which needs
    to see it ABSENT to compute the channel-based default correctly."""
    project_yaml = tmp_path / "project.yaml"
    project_yaml.write_text(
        "project_name: app0\n"
        "monitoring:\n"
        "  skip_auto: true\n"
        "  extra:\n"
        "    - name: app0-frontend\n"
        "      url: https://app0.example.ch\n",
        encoding="utf-8",
    )
    specs = kuma_sync.monitors_from_project_yaml(project_yaml)
    assert "max_retries" not in specs[0]


def test_monitoring_defaults_override_still_applies_a_max_retries_floor(tmp_path):
    """An explicit `monitoring.defaults.max_retries` in project.yaml must
    still reach every auto-derived/extra monitor via base.update(overrides),
    unchanged by INF-59."""
    project_yaml = tmp_path / "project.yaml"
    project_yaml.write_text(
        "project_name: app0\n"
        "monitoring:\n"
        "  skip_auto: true\n"
        "  defaults:\n"
        "    max_retries: 9\n"
        "  extra:\n"
        "    - name: app0-frontend\n"
        "      url: https://app0.example.ch\n",
        encoding="utf-8",
    )
    specs = kuma_sync.monitors_from_project_yaml(project_yaml)
    assert specs[0]["max_retries"] == 9
    kwargs = kuma_sync._build_monitor_kwargs(specs[0])
    assert kwargs["maxretries"] == 9


@pytest.mark.parametrize("declared_name", ["alert-email", ["alert-email", "claude-relay-main-prod"]])
def test_human_ungated_stash_reads_the_declared_name_not_live_resolution(declared_name):
    """Guards the reviewer-relevant edge case: _notifies_human_ungated must
    be computed from the ORIGINAL declared notification_name, before
    _resolve_notification_ids pops and resolves it -- a Kuma-API hiccup
    resolving names must not be able to make an already-channelled monitor
    look channel-less."""
    spec = {"name": "flaky-resolve", "url": "https://example.ch", "notification_name": declared_name}
    spec["_notifies_human_ungated"] = kuma_sync._notifies_human_ungated(spec)
    # Simulate the degrade path: nothing resolves (empty notifications_by_name),
    # but the ORIGINAL declaration is what the stash must have captured.
    kuma_sync._resolve_notification_ids(spec, {}, [], notifications_resolved=False)
    kwargs = kuma_sync._build_monitor_kwargs(spec)
    assert kwargs["maxretries"] == 3  # still the channelled default


def test_existing_monitor_missing_declared_defaults_is_detected_as_changed():
    """CI-5 scope 4: a monitor created before this fix has an empty
    notificationIDList in Kuma. Once defaults are resolved into kwargs, that
    monitor must be reported as changed so the sync repairs it — no separate
    'repair' code path, this is the pre-existing CI-4 id-set comparison
    doing its job once the field is actually populated."""
    kwargs = base_kwargs(notificationIDList=[7])
    existing = existing_from(kwargs, notificationIDList={})
    assert kuma_sync._monitor_changed(kwargs, existing) is True


class RecordingApi:
    """Records every add_monitor call's kwargs; used to prove the `multi`
    path resolves notification_name identically to the single path."""

    def __init__(self, notifications):
        self._notifications = notifications
        self.added = []

    def get_notifications(self):
        return self._notifications

    def get_monitors(self):
        return []

    def add_monitor(self, **kwargs):
        self.added.append(kwargs)

    def disconnect(self):
        pass


def test_multi_resolves_default_notifications_like_single_path(tmp_path, monkeypatch):
    """The `multi` path never called get_notifications() at all before this
    WO — every CI-created monitor had an empty notificationIDList. This
    proves it now resolves the same declared default as sync_monitors()."""
    notif_path = tmp_path / "notifications.yml"
    notif_path.write_text(
        "notifications:\n"
        "  - name: discord-alerts\n"
        "    type: discord\n"
        "    default: true\n",
        encoding="utf-8",
    )
    api = RecordingApi([{"name": "discord-alerts", "id": 7}])
    monkeypatch.setattr(kuma_sync, "_login", lambda: api)
    monkeypatch.setenv("KUMA_SYNC_BUDGET_SECONDS", "0")

    app_path = _project_yaml(tmp_path, "app0")
    kuma_sync.sync_monitors_multi([app_path], notifications_config_path=str(notif_path))

    assert api.added, "add_monitor was never called"
    for kwargs in api.added:
        assert kwargs["notificationIDList"] == [7]


def test_multi_resolves_extra_entry_relay_like_single_path(tmp_path, monkeypatch):
    """The multi path resolves an explicit notification_name (declared on a
    monitoring.extra entry) exactly like sync_monitors() does — union with
    the declared default, not a replacement of it."""
    notif_path = tmp_path / "notifications.yml"
    notif_path.write_text(
        "notifications:\n"
        "  - name: discord-alerts\n"
        "    type: discord\n"
        "    default: true\n",
        encoding="utf-8",
    )
    project_yaml = tmp_path / "app0" / "project.yaml"
    project_yaml.parent.mkdir()
    project_yaml.write_text(
        "project_name: app0\n"
        "monitoring:\n"
        "  skip_auto: true\n"
        "  extra:\n"
        "    - name: app0-relay-monitor\n"
        "      url: http://app0.example.ch/health\n"
        "      notification_name: claude-relay-main-prod\n",
        encoding="utf-8",
    )
    api = RecordingApi([
        {"name": "discord-alerts", "id": 7},
        {"name": "claude-relay-main-prod", "id": 9},
    ])
    monkeypatch.setattr(kuma_sync, "_login", lambda: api)
    monkeypatch.setenv("KUMA_SYNC_BUDGET_SECONDS", "0")

    kuma_sync.sync_monitors_multi(
        [str(project_yaml)], notifications_config_path=str(notif_path)
    )

    assert len(api.added) == 1
    assert api.added[0]["notificationIDList"] == [7, 9]


# --- CI-6 item 2: typed notification-config coercion after env expansion --

def test_smtp_port_string_is_coerced_to_int():
    assert kuma_sync._coerce_typed_config({"smtpPort": "587"}) == {"smtpPort": 587}


def test_smtp_secure_string_is_coerced_to_bool():
    assert kuma_sync._coerce_typed_config({"smtpSecure": "true"}) == {"smtpSecure": True}
    assert kuma_sync._coerce_typed_config({"smtpSecure": "false"}) == {"smtpSecure": False}


def test_unlisted_string_field_is_untouched():
    """A field not on the known-typed list stays a string even if it looks
    numeric — coercion is driven by an explicit list, not "looks like a
    number" (CI-6 Risk 2)."""
    cfg = kuma_sync._coerce_typed_config({"discordWebhookUrl": "12345", "smtpHost": "smtp.resend.com"})
    assert cfg == {"discordWebhookUrl": "12345", "smtpHost": "smtp.resend.com"}


def test_already_typed_values_pass_through():
    """A literal (already int/bool, not routed through ${...}) is left as-is
    — only strings get coerced."""
    assert kuma_sync._coerce_typed_config({"smtpPort": 2465, "smtpSecure": True}) == {
        "smtpPort": 2465,
        "smtpSecure": True,
    }


def test_sync_notifications_coerces_env_expanded_smtp_port(monkeypatch, tmp_path):
    """End-to-end: a config that routes smtpPort through ${...} syncs without
    the TypeError this WO fixes — proving item 2 rather than the literal
    workaround it replaces."""
    monkeypatch.setenv("SMTP_PORT", "2465")
    monkeypatch.setenv("SMTP_SECURE", "true")
    config_path = tmp_path / "notifications.yml"
    config_path.write_text(
        "notifications:\n"
        "  - name: alert-email\n"
        "    type: smtp\n"
        "    default: false\n"
        "    config:\n"
        "      smtpPort: \"${SMTP_PORT}\"\n"
        "      smtpSecure: \"${SMTP_SECURE}\"\n",
        encoding="utf-8",
    )

    class RecordingNotificationApi:
        def get_notifications(self):
            return []

        def add_notification(self, **kwargs):
            self.added = kwargs

    api = RecordingNotificationApi()
    kuma_sync.sync_notifications(api, config_path)

    assert api.added["smtpPort"] == 2465
    assert api.added["smtpPort"].__class__ is int
    assert api.added["smtpSecure"] is True


# --- CI-8: don't wipe notifications when the fetch itself failed ----------

def test_resolve_notification_ids_unresolved_fetch_never_sets_the_key_even_with_no_names():
    """notifications_resolved=False means the notifications LIST fetch
    itself failed — not that nothing is declared. A monitor with no
    notification_name must NOT be wiped to [] just because the degrade path
    couldn't tell us what it would have resolved to."""
    spec = {"name": "hram-frontend"}
    kuma_sync._resolve_notification_ids(spec, {}, [], notifications_resolved=False)
    assert "notification_ids" not in spec


def test_resolve_notification_ids_unresolved_fetch_protects_explicit_names_too():
    spec = {"name": "cockpit-frontend", "notification_name": ["claude-relay-main-prod", "alert-email"]}
    kuma_sync._resolve_notification_ids(spec, {}, [], notifications_resolved=False)
    assert "notification_ids" not in spec


def test_multi_get_notifications_failure_does_not_wipe_existing_default_only_monitor(
    tmp_path, monkeypatch
):
    """CI-8 Risk / reviewer P1: a transient get_notifications() failure in
    sync_monitors_multi's degrade path must not wipe a default-only
    monitor's real, non-empty notificationIDList in Kuma — the fetch
    failing is not the same as the config declaring nothing."""

    class FlakyNotificationsApi:
        def get_notifications(self):
            raise ConnectionError("Kuma notifications endpoint down")

        def get_monitors(self):
            return [{
                "id": 1,
                "name": "app0-frontend",
                "type": MonitorType("http"),
                "url": "https://app0.example.ch",
                "interval": 60,
                "maxretries": 0,  # INF-59: no notification_name declared -- channel-less default
                "retryInterval": 60,
                "notificationIDList": {"7": True},  # real existing assignment
            }]

        def edit_monitor(self, monitor_id, **kwargs):
            raise AssertionError(
                f"edit_monitor must not be called — a get_notifications() failure "
                f"must not be treated as 'nothing declared' (kwargs={kwargs})"
            )

        def add_monitor(self, **kwargs):
            raise AssertionError(f"add_monitor must not be called — the monitor already exists (kwargs={kwargs})")

        def disconnect(self):
            pass

    api = FlakyNotificationsApi()
    monkeypatch.setattr(kuma_sync, "_login", lambda: api)
    monkeypatch.setenv("KUMA_SYNC_BUDGET_SECONDS", "0")

    # skip_auto + a single explicit `extra` entry, no notification_name — the
    # exact "default-only monitor, no explicit relay" shape this risk is about.
    app_dir = tmp_path / "app0"
    app_dir.mkdir()
    (app_dir / "project.yaml").write_text(
        "project_name: app0\n"
        "monitoring:\n"
        "  skip_auto: true\n"
        "  extra:\n"
        "    - name: app0-frontend\n"
        "      url: https://app0.example.ch\n",
        encoding="utf-8",
    )
    kuma_sync.sync_monitors_multi([str(app_dir / "project.yaml")])  # must not raise — assertions live in edit_monitor/add_monitor


# --- INF-48: monitors derive from ALL production domains, not domains[0] ------


def _write_project_yaml(tmp_path, body):
    path = tmp_path / "project.yaml"
    path.write_text(body, encoding="utf-8")
    return path


SINGLE_DOMAIN = """project_name: solo
environments:
  production:
    server: main-prod
    domains:
      - "solo.example.ch"
"""

MULTI_DOMAIN = """project_name: hram
environments:
  production:
    server: research-prod
    domains:
      - "hram.ch"
      - "www.hram.ch"
      - "imara.tz"
      - "www.imara.tz"
      - "imara.or.tz"
      - "www.imara.or.tz"
"""


def test_single_domain_shape_is_unchanged(tmp_path):
    """The existing shape must be provably untouched. kuma-sync prunes by NAME,
    so a renamed primary would delete and recreate the monitor, losing its
    history — the reason INF-48 only ADDS names."""
    specs = kuma_sync.monitors_from_project_yaml(_write_project_yaml(tmp_path, SINGLE_DOMAIN))
    assert [s["name"] for s in specs] == ["solo-frontend", "solo-healthz"]
    assert specs[0]["url"] == "https://solo.example.ch"
    assert specs[1]["url"] == "https://solo.example.ch/api/healthz"


def test_every_production_domain_gets_a_frontend_monitor(tmp_path):
    """The defect INF-48 closes: `primary = prod_domains[0]` monitored hram on
    hram.ch alone while its four IMARA hostnames 404ed for eight days."""
    specs = kuma_sync.monitors_from_project_yaml(_write_project_yaml(tmp_path, MULTI_DOMAIN))
    monitored = {s["url"].removeprefix("https://") for s in specs if s["name"].endswith(("frontend",)) or "-frontend-" in s["name"]}
    assert monitored == {
        "hram.ch",
        "www.hram.ch",
        "imara.tz",
        "www.imara.tz",
        "imara.or.tz",
        "www.imara.or.tz",
    }


def test_primary_keeps_its_names_and_is_the_only_healthz(tmp_path):
    """Aliases get a frontend check only — a healthz probe per alias would hit
    the SAME backend for no extra signal. What an alias fails at independently
    is routing, which a frontend GET already covers."""
    specs = kuma_sync.monitors_from_project_yaml(_write_project_yaml(tmp_path, MULTI_DOMAIN))
    names = [s["name"] for s in specs]
    assert names[0] == "hram-frontend"
    assert names[1] == "hram-healthz"
    assert [n for n in names if n.endswith("-healthz")] == ["hram-healthz"]
    assert names[2:] == [
        "hram-frontend-www-hram-ch",
        "hram-frontend-imara-tz",
        "hram-frontend-www-imara-tz",
        "hram-frontend-imara-or-tz",
        "hram-frontend-www-imara-or-tz",
    ]


def test_alias_monitors_carry_the_production_relay(tmp_path):
    """An alias that goes down must page the same per-server relay as the
    primary, or the extra coverage produces alerts nobody receives."""
    specs = kuma_sync.monitors_from_project_yaml(_write_project_yaml(tmp_path, MULTI_DOMAIN))
    assert {s["notification_name"] for s in specs} == {"claude-relay-research-prod"}


def test_monitor_names_are_unique(tmp_path):
    """Names are the sync's identity key; a collision would make two specs
    fight over one Kuma monitor on every run."""
    specs = kuma_sync.monitors_from_project_yaml(_write_project_yaml(tmp_path, MULTI_DOMAIN))
    names = [s["name"] for s in specs]
    assert len(names) == len(set(names))


def test_extra_still_overrides_an_alias_monitor(tmp_path):
    """The `extra`-wins override must cover the new alias names too, not just
    the two original auto-derived ones."""
    body = MULTI_DOMAIN + (
        "monitoring:\n"
        "  extra:\n"
        "    - name: hram-frontend-imara-tz\n"
        "      url: https://imara.tz/custom\n"
    )
    specs = kuma_sync.monitors_from_project_yaml(_write_project_yaml(tmp_path, body))
    matching = [s for s in specs if s["name"] == "hram-frontend-imara-tz"]
    assert len(matching) == 1
    assert matching[0]["url"] == "https://imara.tz/custom"


@pytest.mark.parametrize(
    "domain,slug",
    [
        ("imara.tz", "imara-tz"),
        ("www.imara.or.tz", "www-imara-or-tz"),
        ("Staging-HRAM.bigler-consult.ch", "staging-hram-bigler-consult-ch"),
    ],
)
def test_domain_slug(domain, slug):
    assert kuma_sync._domain_slug(domain) == slug


def test_colliding_alias_slugs_raise_instead_of_overwriting(tmp_path):
    """Two domains that slugify identically must fail loudly. A silent overwrite
    would leave one domain uncovered while a monitor for it appears to exist —
    the failure mode INF-48 exists to end, reintroduced one level down."""
    body = """project_name: collide
environments:
  production:
    server: main-prod
    domains:
      - "primary.example.ch"
      - "a-b.example.ch"
      - "a.b.example.ch"
"""
    with pytest.raises(ValueError, match="silently overwrite"):
        kuma_sync.monitors_from_project_yaml(_write_project_yaml(tmp_path, body))
