#!/usr/bin/env python3
"""Sync Uptime Kuma notifications or monitors from a YAML config file.

Used by the workflow-templates composite actions:
    - sync-kuma-notifications
    - register-kuma-monitors

Authentication uses the dedicated automation user (KUMA_AUTOMATION_USER /
KUMA_AUTOMATION_PASSWORD). API-key auth is NOT supported by Kuma for
monitor/notification CRUD — only Socket.IO with user+password is.

Idempotency: matched by `name`. Existing entries are updated, new ones
created. Entries present in Kuma but missing from the config are NOT
deleted (use `--prune` to enable deletion).

Config formats
==============

Notifications (e.g. monitoring/notifications.yml):

    notifications:
      - name: discord-alerts
        type: discord
        default: true              # apply to all existing + future monitors
        config:
          discordUsername: "Kuma"
          discordWebhookUrl: "${DISCORD_WEBHOOK_URL}"  # env-var expansion

Monitors (e.g. monitoring/monitor.yml):

    monitors:
      - name: hram-frontend
        type: http
        url: https://hram.ch
        interval: 60               # seconds
        max_retries: 3
        retry_interval: 60         # seconds
"""

import argparse
import os
import sys
import time
from enum import Enum
from pathlib import Path

import yaml

try:
    from uptime_kuma_api import UptimeKumaApi
except ImportError:
    # We install the Kuma-2-compatible fork `uptime-kuma-api-v2` (PyPI),
    # not the abandoned upstream `uptime-kuma-api`. The import name stays
    # the same — see comments in
    # .github/actions/register-kuma-monitors/action.yml for context.
    print(
        "❌ uptime-kuma-api-v2 not installed. "
        "Run: pip install uptime-kuma-api-v2",
        file=sys.stderr,
    )
    sys.exit(1)


def _expand_env(value):
    """Recursively expand env-var references like ${FOO} in strings."""
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _login():
    url = os.environ.get("KUMA_URL")
    user = os.environ.get("KUMA_AUTOMATION_USER")
    password = os.environ.get("KUMA_AUTOMATION_PASSWORD")

    missing = [k for k, v in {
        "KUMA_URL": url,
        "KUMA_AUTOMATION_USER": user,
        "KUMA_AUTOMATION_PASSWORD": password,
    }.items() if not v]
    if missing:
        print(f"❌ Missing env vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    # Kuma's Socket.IO login is flaky under load: the server is a single Node
    # process, so on a busy host the HTTP reachability check passes but the
    # `login` Socket.IO call times out (socketio.exceptions.TimeoutError).
    # Raise the per-call timeout and retry with linear backoff before failing.
    last_exc = None
    for attempt in range(1, 6):
        api = None
        try:
            api = UptimeKumaApi(url, timeout=30)
            api.login(user, password)
            return api
        except Exception as exc:  # noqa: BLE001 — socketio.TimeoutError et al.
            last_exc = exc
            if api is not None:
                try:
                    api.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            # Wrong credentials won't recover by retrying — fail fast instead
            # of burning ~50s and spamming failed-login events on the server.
            if any(s in str(exc).lower() for s in ("incorrect", "password", "credential")):
                break
            if attempt < 5:
                wait = min(5 * attempt, 25)
                print(
                    f"⚠️  Kuma login attempt {attempt}/5 failed "
                    f"({type(exc).__name__}); retrying in {wait}s...",
                    file=sys.stderr,
                )
                time.sleep(wait)
    print(f"❌ Kuma login failed after 5 attempts: {last_exc}", file=sys.stderr)
    sys.exit(1)


class BudgetExceeded(Exception):
    """The run's overall wall-clock ceiling was reached (CI-4 scope 3)."""


class Budget:
    """Wall-clock ceiling for one sync run.

    CI-4: a wedged Kuma used to produce a 24-minute red run (57 monitors x up to
    ~50s of retries each) instead of a fast, legible failure. Calibrated against
    the observed runs rather than a round number: the healthy run in the CI-4
    evidence performed a FULL write pass of all 57 monitors and finished well
    inside 10 minutes, while the failing run burned 24. Once the permanent diff
    is fixed a healthy run writes almost nothing and should be far quicker
    still, so 10 minutes stays a generous ceiling for the worst legitimate case
    while cutting the pathological one to well under half.
    """

    def __init__(self, seconds=None):
        if seconds is None:
            seconds = int(os.environ.get("KUMA_SYNC_BUDGET_SECONDS", "600"))
        self.seconds = seconds
        self._start = time.monotonic()

    def elapsed(self):
        return time.monotonic() - self._start

    def exceeded(self):
        return self.seconds > 0 and self.elapsed() > self.seconds

    def check(self, where):
        if self.exceeded():
            raise BudgetExceeded(
                f"run budget of {self.seconds}s exhausted after "
                f"{self.elapsed():.0f}s (at: {where}). Kuma is not keeping up — "
                "failing fast instead of retrying a wedged server."
            )


class Session:
    """Holds the LIVE api object so a retry can re-resolve its target after a
    reconnect.

    CI-4 scope 2: `_retry_call` used to receive an already-bound method
    (`api.edit_monitor`). Reconnecting inside the retry loop produced a new api
    object, but the bound method still pointed at the dead one — so every
    remaining attempt was a guaranteed failure, and a local rebind could not
    propagate back to the caller either. Retries now resolve the method by NAME
    against whatever connection this holder currently owns.
    """

    def __init__(self, api, budget=None):
        self.api = api
        self.budget = budget or Budget()

    def reconnect(self):
        try:
            self.api.disconnect()
        except Exception:  # noqa: BLE001
            pass
        # _login() is terminal on unrecoverable failure (it sys.exit()s after
        # its own 5 attempts, fast-pathing bad credentials). That is the same
        # behaviour every other _login() call site in this script already has.
        self.api = _login()
        return self.api

    def call(self, label, method, *args, **kwargs):
        return _retry_call(label, self, method, *args, **kwargs)

    def disconnect(self):
        try:
            self.api.disconnect()
        except Exception:  # noqa: BLE001
            pass


def _retry_call(label, session, method, *args, **kwargs):
    """Retry `session.api.<method>` up to 5 times, RECONNECTING between attempts.

    Mirrors _login's disconnect-before-retry, but the reconnect has to live on
    the session (see Session) — retrying a wedged Socket.IO connection with the
    same object is time spent on a guaranteed failure.
    """
    last = None
    for attempt in range(1, 6):
        try:
            return getattr(session.api, method)(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if any(s in str(exc).lower() for s in ("not found", "does not exist", "permission")):
                raise
            if attempt < 5:
                # Don't spend the backoff on a run that is already over budget.
                session.budget.check(f"{label} attempt {attempt}/5")
                wait = attempt * 5
                print(
                    f"⚠️  Kuma {label} attempt {attempt}/5 failed "
                    f"({type(exc).__name__}); reconnecting and retrying in {wait}s...",
                    file=sys.stderr,
                )
                time.sleep(wait)
                session.reconnect()
    raise last


def _unwrap(value):
    """Enum members compare by VALUE, never by str().

    CI-4 root cause: `uptime_kuma_api` converts a monitor's `type` into a
    `MonitorType` enum ON READ, while we send the plain string "http". The old
    comparison used `str(current)`, and for a `(str, Enum)` member that is
    "MonitorType.HTTP" — not "http". So every monitor differed on `type` on
    every run, `unchanged` was never reported once, and each run performed a
    full write pass of the whole monitor set against Kuma's single-threaded
    Socket.IO server. Comparing `.value` is what the old comment at this spot
    only claimed to do.
    """
    return value.value if isinstance(value, Enum) else value


def _norm_scalar(value):
    """Normalise one scalar for comparison.

    Absent/None from Kuma is treated as empty string so optional fields the
    server does not echo back don't trigger spurious edits when the desired
    value is empty too (pre-existing behaviour, deliberately preserved).
    """
    value = _unwrap(value)
    return "" if value is None else str(value)


def _norm_id_set(value):
    """Normalise a notification-id collection to a set of id strings.

    Kuma represents `notificationIDList` as an object keyed by id; the client
    library converts it to a list of ints on read; older/other paths may hand
    back either. All three forms must compare equal when they name the same ids.

    A dict value of `false` means the notification is NOT assigned, so it is
    excluded — otherwise un-assigning a notification in the Kuma UI would stop
    being detected as drift, which is exactly the correction this sync exists to
    make (CI-4 Risk 1).
    """
    value = _unwrap(value)
    if value is None:
        return set()
    if isinstance(value, dict):
        return {str(_unwrap(k)) for k, v in value.items() if v}
    if isinstance(value, (list, tuple, set)):
        return {str(_unwrap(v)) for v in value}
    return {str(value)}


# Fields holding a collection of notification ids, compared as an id SET.
_ID_SET_KEYS = frozenset({"notificationIDList"})


def _monitor_changed(kwargs, existing_monitor, monitor_name=""):
    """Return True if any key in kwargs differs from the existing monitor's value.

    Logs WHICH key differed and both values (CI-4 scope 0): when this reports a
    change, the reason must be visible in the run log rather than inferred from
    the code months later.
    """
    def differs(key, desired_repr, current_repr):
        label = f" [{monitor_name}]" if monitor_name else ""
        print(
            f"↻ diff{label} on '{key}': desired={desired_repr!r} current={current_repr!r}",
            file=sys.stderr,
        )
        return True

    for key, desired in kwargs.items():
        current = existing_monitor.get(key)

        if key in _ID_SET_KEYS:
            desired_ids, current_ids = _norm_id_set(desired), _norm_id_set(current)
            if desired_ids != current_ids:
                return differs(key, sorted(desired_ids), sorted(current_ids))
        elif isinstance(desired, int):
            try:
                current_i = int(_unwrap(current))
            except (TypeError, ValueError):
                return differs(key, desired, current)
            if desired != current_i:
                return differs(key, desired, current_i)
        elif isinstance(desired, list):
            current_u = _unwrap(current)
            if not isinstance(current_u, list):
                return differs(key, desired, current)
            desired_l = sorted(_norm_scalar(x) for x in desired)
            current_l = sorted(_norm_scalar(x) for x in current_u)
            if desired_l != current_l:
                return differs(key, desired_l, current_l)
        else:
            desired_s, current_s = _norm_scalar(desired), _norm_scalar(current)
            if desired_s != current_s:
                return differs(key, desired_s, current_s)

    return False


def _build_monitor_kwargs(spec):
    """Build the kwargs dict for add/edit_monitor from a normalized spec dict."""
    kwargs = {
        "name": spec["name"],
        "type": spec.get("type", "http"),
        "url": spec.get("url"),
        "interval": int(spec.get("interval", 60)),
        # Default 5 (was 3): a monitored box surviving a maintenance reboot cycle
        # (kernel reboot + Docker + container warmup) is briefly unreachable for
        # several minutes; 5 x 60s of confirmed failures before firing "down" rides
        # that out without masking a real outage for long. Per-monitor override via
        # `max_retries` in project.yaml still wins.
        "maxretries": int(spec.get("max_retries", 5)),
        "retryInterval": int(spec.get("retry_interval", 60)),
    }
    if "accepted_statuscodes" in spec:
        kwargs["accepted_statuscodes"] = spec["accepted_statuscodes"]
    if "hostname" in spec:
        kwargs["hostname"] = spec["hostname"]
    if "port" in spec:
        kwargs["port"] = int(spec["port"])
    if "keyword" in spec:
        kwargs["keyword"] = spec["keyword"]
    if "method" in spec:
        kwargs["method"] = spec["method"]
    if "body" in spec:
        kwargs["body"] = spec["body"]
    if "headers" in spec:
        kwargs["headers"] = spec["headers"]
    # notification_ids: list of Kuma notification IDs to assign to this monitor.
    # Populated by sync_monitors after resolving notification names → IDs.
    if "notification_ids" in spec:
        kwargs["notificationIDList"] = spec["notification_ids"]
    return {k: v for k, v in kwargs.items() if v is not None}


def sync_notifications(api, config_path, prune=False):
    with open(config_path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    specs = data.get("notifications", [])
    if not specs:
        print("ℹ️  No notifications declared in config; nothing to do.")
        return

    existing = {n["name"]: n for n in api.get_notifications()}
    declared_names = set()

    for raw_spec in specs:
        spec = _expand_env(raw_spec)
        name = spec["name"]
        declared_names.add(name)
        ntype = spec["type"]
        is_default = bool(spec.get("default", False))
        cfg = spec.get("config", {}) or {}

        kwargs = {
            "name": name,
            "type": ntype,
            "isDefault": is_default,
            "applyExisting": is_default,
            **cfg,
        }

        if name in existing:
            api.edit_notification(existing[name]["id"], **kwargs)
            print(f"✏️  updated notification: {name}")
        else:
            api.add_notification(**kwargs)
            print(f"➕ created notification: {name}")

    if prune:
        for name, entry in existing.items():
            if name not in declared_names:
                api.delete_notification(entry["id"])
                print(f"🗑️  deleted stale notification: {name}")


def monitors_from_project_yaml(project_yaml_path):
    """Derive monitor specs from project.yaml.

    Convention (no monitoring: section in project.yaml → all defaults):
      <name_prefix>-frontend            → https://<prod-primary>
      <name_prefix>-healthz             → https://<prod-primary><healthz_path>
      <name_prefix>-staging-frontend    → https://<staging-primary>      (if defined)
      <name_prefix>-staging-healthz     → https://<staging-primary><healthz_path>  (if defined)

    name_prefix       defaults to project_name
    healthz_path      defaults to /api/healthz
    prod-primary      is environments.production.domains[0]
    staging-primary   is environments.staging.domains[0] (if exists; otherwise skipped silently)

    Overrides via optional `monitoring:` section in project.yaml:
      monitoring:
        name_prefix: jg-ferien        # use a different prefix than project_name
        healthz_path: /healthz        # use a different healthz endpoint
        defaults:                     # override monitor defaults
          interval: 30
          max_retries: 5
        extra:                        # additional monitors beyond auto-generated
          - name: youngrgamechangersbs
            url: https://younggamechangersbs.ch
        # Optional: skip the auto-generated entries entirely (e.g. if you want
        # custom names that don't fit the prefix pattern).
        skip_auto: false
        # Optional: skip the auto-generated staging monitors only (keep prod).
        # Useful for apps where staging is internal-only and shouldn't be on
        # the public-uptime dashboard. No effect when skip_auto=true (in that
        # case no auto-derived monitors are emitted at all).
        skip_staging: false
    """
    with open(project_yaml_path, "r", encoding="utf-8") as fh:
        pj = yaml.safe_load(fh) or {}

    project_name = pj.get("project_name")
    if not project_name:
        raise ValueError(f"{project_yaml_path}: missing 'project_name'.")

    monitoring = pj.get("monitoring") or {}
    name_prefix = monitoring.get("name_prefix", project_name)
    healthz_path = monitoring.get("healthz_path", "/api/healthz")
    overrides = monitoring.get("defaults") or {}
    extra = monitoring.get("extra") or []
    skip_auto = bool(monitoring.get("skip_auto", False))
    skip_staging = bool(monitoring.get("skip_staging", False))

    base = {
        "type": "http",
        "interval": 60,
        "max_retries": 3,
        "retry_interval": 60,
    }
    base.update(overrides)

    specs = []
    if not skip_auto:
        envs = pj.get("environments") or {}

        # Production monitors (required).
        prod = envs.get("production") or {}
        prod_domains = prod.get("domains") or []
        if not prod_domains:
            raise ValueError(
                f"{project_yaml_path}: environments.production.domains is empty; "
                "cannot derive auto-monitors. Set monitoring.skip_auto=true or "
                "add at least one domain."
            )
        primary = prod_domains[0]
        # Derive the relay notification name from the production server so
        # kuma_sync can assign the correct per-server relay to each monitor.
        # Convention: claude-relay-{server} (e.g. claude-relay-main-prod).
        prod_server = prod.get("server")
        prod_relay = {"notification_name": f"claude-relay-{prod_server}"} if prod_server else {}
        specs.append({**base, **prod_relay, "name": f"{name_prefix}-frontend", "url": f"https://{primary}"})
        specs.append({**base, **prod_relay, "name": f"{name_prefix}-healthz",  "url": f"https://{primary}{healthz_path}"})

        # Staging monitors (opt-out via monitoring.skip_staging=true, or
        # implicit-skip when environments.staging.domains is missing/empty).
        if not skip_staging:
            staging = envs.get("staging") or {}
            staging_domains = staging.get("domains") or []
            if staging_domains:
                staging_primary = staging_domains[0]
                specs.append({**base, "name": f"{name_prefix}-staging-frontend", "url": f"https://{staging_primary}"})
                specs.append({**base, "name": f"{name_prefix}-staging-healthz",  "url": f"https://{staging_primary}{healthz_path}"})

    # Track auto-derived names so explicit `extra` entries can cleanly
    # override them. This prevents the silent double-write that would
    # otherwise occur when an operator declares e.g. `monitoring.extra:
    # [{name: myapp-staging-frontend, url: ...}]` while staging-auto-derive
    # is also active — both specs would land in the list, sync_monitors
    # would issue two edit_monitor calls per CI run, and the final state
    # would depend on iteration order. With explicit override, the `extra`
    # entry wins and a single notice goes to stderr.
    auto_names = {s["name"] for s in specs}

    for entry in extra:
        merged = {**base, **entry}
        if "url" not in merged or "name" not in merged:
            raise ValueError(
                f"{project_yaml_path}: monitoring.extra entry missing 'name' or 'url': {entry!r}"
            )
        if merged["name"] in auto_names:
            print(
                f"ℹ️  monitoring.extra entry '{merged['name']}' overrides "
                f"auto-derived monitor of the same name (extra wins).",
                file=sys.stderr,
            )
            specs = [s for s in specs if s["name"] != merged["name"]]
        specs.append(merged)

    return specs


def sync_monitors(api, config_path=None, project_yaml_path=None, prune=False):
    """Sync monitors. Source is either a monitor.yml or a project.yaml.

    If config_path is given and exists → use legacy monitor.yml format.
    Else if project_yaml_path is given → derive monitors from project.yaml.
    Else → error.
    """
    if config_path and Path(config_path).exists():
        with open(config_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        specs = data.get("monitors", [])
    elif project_yaml_path and Path(project_yaml_path).exists():
        specs = monitors_from_project_yaml(project_yaml_path)
        print(f"ℹ️  Derived {len(specs)} monitor spec(s) from {project_yaml_path}")
    else:
        print(
            "ℹ️  No monitors config found "
            f"(checked monitor.yml='{config_path}', project.yaml='{project_yaml_path}'); "
            "nothing to do."
        )
        return

    if not specs:
        print("ℹ️  No monitors declared in config; nothing to do.")
        return

    session = api if isinstance(api, Session) else Session(api)

    existing = {m["name"]: m for m in session.call("get_monitors", "get_monitors")}
    declared_names = set()

    # Build name→id map for notifications once so per-monitor relay assignment
    # can resolve names without a separate API call per monitor.
    notifications_by_name = {
        n["name"]: n["id"]
        for n in session.call("get_notifications", "get_notifications")
    }

    for raw_spec in specs:
        spec = _expand_env(raw_spec)
        name = spec["name"]
        declared_names.add(name)

        # Resolve notification_name → notification_ids before building kwargs.
        if "notification_name" in spec:
            notif_name = spec.pop("notification_name")
            notif_id = notifications_by_name.get(notif_name)
            if notif_id is not None:
                spec["notification_ids"] = [notif_id]
            else:
                print(
                    f"⚠️  notification '{notif_name}' not found in Kuma "
                    f"(monitor: {name}) — skipping relay assignment",
                    file=sys.stderr,
                )

        kwargs = _build_monitor_kwargs(spec)

        if name in existing:
            if _monitor_changed(kwargs, existing[name], name):
                session.call(f"edit_monitor({name})", "edit_monitor", existing[name]["id"], **kwargs)
                print(f"✏️  updated monitor: {name}")
            else:
                print(f"✓ unchanged: {name}")
        else:
            session.call(f"add_monitor({name})", "add_monitor", **kwargs)
            print(f"➕ created monitor: {name}")

    if prune:
        for name, entry in existing.items():
            if name not in declared_names:
                session.call(f"delete_monitor({name})", "delete_monitor", entry["id"])
                print(f"🗑️  deleted stale monitor: {name}")


def sync_monitors_multi(project_yaml_paths, prune=False):
    """Sync monitors for multiple apps in ONE Kuma session (one login/disconnect).

    Processes each project.yaml sequentially.  Per-app errors are caught and
    reported without aborting the remaining apps.  Prune is skipped when any
    app failed — running it with an incomplete declared-name set would delete
    valid monitors belonging to the failed apps.
    """
    session = Session(_login())
    all_declared_names = set()
    failed_apps = []
    budget_hit = None

    try:
        for path_str in project_yaml_paths:
            path = Path(path_str)
            if not path.exists():
                print(f"⚠️  project.yaml not found: {path} — skipping", file=sys.stderr)
                failed_apps.append(str(path))
                continue

            app_name = path.parent.name
            print(f"\n{'=' * 60}")
            print(f"📦  {app_name}")
            print(f"{'=' * 60}")

            # Parse errors (missing project_name, empty domains, bad YAML) must not
            # propagate into the prune block with an incomplete declared-name set.
            try:
                specs = monitors_from_project_yaml(path)
            except Exception as exc:  # noqa: BLE001
                print(f"❌ Failed to parse {path}: {exc}", file=sys.stderr)
                failed_apps.append(app_name)
                continue
            print(f"ℹ️  Derived {len(specs)} monitor spec(s) from {path}")

            try:
                existing = {m["name"]: m for m in session.call("get_monitors", "get_monitors")}
            except BudgetExceeded:
                # A blown run budget is NOT a per-app failure. Without this the
                # generic handler below would rewrite it as "failed to fetch
                # monitors", append to failed_apps, and CONTINUE to the next app
                # — turning the intended fast abort into a slow limp through the
                # remaining apps with a misleading final message.
                raise
            except Exception as exc:  # noqa: BLE001
                print(f"❌ Failed to fetch monitors for {app_name}: {exc}", file=sys.stderr)
                failed_apps.append(app_name)
                continue

            app_failed = False
            for raw_spec in specs:
                spec = _expand_env(raw_spec)
                name = spec["name"]
                all_declared_names.add(name)
                kwargs = _build_monitor_kwargs(spec)

                # Fail fast rather than grinding through the remaining monitors
                # once the run is already over its ceiling.
                session.budget.check(f"before monitor {name}")

                for recovery_attempt in range(1, 3):
                    try:
                        if name in existing:
                            if _monitor_changed(kwargs, existing[name], name):
                                session.call(
                                    f"edit_monitor({name})",
                                    "edit_monitor",
                                    existing[name]["id"],
                                    **kwargs,
                                )
                                print(f"✏️  updated monitor: {name}")
                            else:
                                print(f"✓ unchanged: {name}")
                        else:
                            session.call(f"add_monitor({name})", "add_monitor", **kwargs)
                            print(f"➕ created monitor: {name}")
                        break
                    except BudgetExceeded:
                        # Not a per-monitor failure — the whole run is over.
                        raise
                    except Exception as exc:  # noqa: BLE001
                        if recovery_attempt == 1 and any(
                            s in str(exc).lower() for s in ("not logged in", "timeout")
                        ):
                            print("⚠️  Session drop, re-logging in...", file=sys.stderr)
                            try:
                                session.reconnect()
                                existing = {
                                    m["name"]: m
                                    for m in session.call("get_monitors", "get_monitors")
                                }
                            except BudgetExceeded:
                                raise  # see the guard above — not an app failure
                            except Exception as rec_exc:  # noqa: BLE001
                                print(f"❌ Recovery failed for {app_name}: {rec_exc}", file=sys.stderr)
                                app_failed = True
                                break
                        else:
                            print(f"❌ Failed to sync monitor {name}: {exc}", file=sys.stderr)
                            app_failed = True
                            break

                if app_failed:
                    break

            if app_failed:
                failed_apps.append(app_name)

        if prune:
            if failed_apps:
                print(
                    f"\n⚠️  Prune skipped — {len(failed_apps)} app(s) failed: "
                    f"{', '.join(failed_apps)}. "
                    "Running prune with an incomplete declared-name set would delete valid monitors.",
                    file=sys.stderr,
                )
            else:
                print(f"\n{'=' * 60}")
                print("🗑️  Prune pass")
                print(f"{'=' * 60}")
                existing_all = {
                    m["name"]: m
                    for m in session.call("get_monitors", "get_monitors")
                }
                for name, entry in existing_all.items():
                    if name not in all_declared_names:
                        # Same proactive check as the sync loop. Without it the
                        # budget could only fire reactively, from inside a
                        # delete's retry — i.e. only after a delete had already
                        # failed — so an abort here would stop at an arbitrary
                        # point mid-prune. Checking first makes the stop
                        # deterministic. Deletions already made stand (they only
                        # ever remove monitors absent from the declared set, so
                        # nothing valid is lost); the remainder is simply left
                        # for the next run.
                        session.budget.check(f"before delete_monitor({name})")
                        session.call(f"delete_monitor({name})", "delete_monitor", entry["id"])
                        print(f"🗑️  deleted stale monitor: {name}")

    except BudgetExceeded as exc:
        # The prune block is intentionally skipped: an aborted run has an
        # incomplete declared-name set, and pruning against it would delete
        # valid monitors — the same safety the failed_apps guard provides.
        budget_hit = exc
    finally:
        session.disconnect()

    if budget_hit is not None:
        print(f"\n❌ Aborted: {budget_hit}", file=sys.stderr)
        sys.exit(1)

    if failed_apps:
        print(f"\n❌ {len(failed_apps)} app(s) failed: {', '.join(failed_apps)}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Sync Uptime Kuma notifications or monitors from YAML.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sn = sub.add_parser("notifications", help="Sync notification channels")
    sn.add_argument("--config", required=True, help="Path to notifications.yml")
    sn.add_argument("--prune", action="store_true",
                    help="Delete Kuma notifications absent from the config")

    sm = sub.add_parser("monitors", help="Sync monitors")
    sm.add_argument(
        "--config",
        required=False,
        default="monitoring/monitor.yml",
        help="Path to monitor.yml (legacy). If absent on disk, fall back to --project-yaml.",
    )
    sm.add_argument(
        "--project-yaml",
        required=False,
        default="project.yaml",
        help="Path to project.yaml. Used to derive monitors when --config is missing.",
    )
    sm.add_argument("--prune", action="store_true",
                    help="Delete Kuma monitors absent from the config")

    sm_multi = sub.add_parser(
        "multi",
        help="Sync monitors for multiple apps in one Kuma session (no concurrent connections)",
    )
    sm_multi.add_argument(
        "project_yamls",
        nargs="+",
        metavar="PROJECT_YAML",
        help="Paths to project.yaml files to process sequentially",
    )
    sm_multi.add_argument(
        "--prune",
        action="store_true",
        help="Delete Kuma monitors absent from all declared configs (combined set)",
    )

    args = parser.parse_args()

    if args.command == "multi":
        sync_monitors_multi(args.project_yamls, prune=args.prune)
        return

    session = Session(_login())
    try:
        if args.command == "notifications":
            config_path = Path(args.config)
            if not config_path.exists():
                print(f"❌ Config file not found: {config_path}", file=sys.stderr)
                sys.exit(1)
            sync_notifications(session.api, config_path, prune=args.prune)
        elif args.command == "monitors":
            sync_monitors(
                session,
                config_path=args.config,
                project_yaml_path=args.project_yaml,
                prune=args.prune,
            )
    except BudgetExceeded as exc:
        print(f"\n❌ Aborted: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        # Via the session, not the original api: a reconnect inside _retry_call
        # swaps session.api, and disconnecting the stale object would leak the
        # live one (R3).
        session.disconnect()


if __name__ == "__main__":
    main()
