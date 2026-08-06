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
    would delete valid monitors — the same safety the failed_apps guard gives."""
    apps = [_project_yaml(tmp_path, "app0")]
    real_budget = kuma_sync.Budget
    monkeypatch.setattr(
        kuma_sync, "Budget",
        lambda *a, **k: type("B", (real_budget,), {"exceeded": lambda self: True})(seconds=1),
    )

    with pytest.raises(SystemExit):
        kuma_sync.sync_monitors_multi(apps, prune=True)

    out = capsys.readouterr().out
    assert "Prune pass" not in out


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
