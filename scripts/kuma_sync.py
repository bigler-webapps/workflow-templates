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
        specs.append({**base, "name": f"{name_prefix}-frontend", "url": f"https://{primary}"})
        specs.append({**base, "name": f"{name_prefix}-healthz",  "url": f"https://{primary}{healthz_path}"})

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

    existing = {m["name"]: m for m in api.get_monitors()}
    declared_names = set()

    for raw_spec in specs:
        spec = _expand_env(raw_spec)
        name = spec["name"]
        declared_names.add(name)

        kwargs = {
            "name": name,
            "type": spec.get("type", "http"),
            "url": spec.get("url"),
            "interval": int(spec.get("interval", 60)),
            "maxretries": int(spec.get("max_retries", 3)),
            "retryInterval": int(spec.get("retry_interval", 60)),
        }
        # Optional fields — only set if explicitly declared in the YAML.
        # Backward-compatible: existing configs without these keys behave as before.
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

        # Drop None to let Kuma defaults apply
        kwargs = {k: v for k, v in kwargs.items() if v is not None}

        if name in existing:
            api.edit_monitor(existing[name]["id"], **kwargs)
            print(f"✏️  updated monitor: {name}")
        else:
            api.add_monitor(**kwargs)
            print(f"➕ created monitor: {name}")

    if prune:
        for name, entry in existing.items():
            if name not in declared_names:
                api.delete_monitor(entry["id"])
                print(f"🗑️  deleted stale monitor: {name}")


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

    args = parser.parse_args()

    api = _login()
    try:
        if args.command == "notifications":
            config_path = Path(args.config)
            if not config_path.exists():
                print(f"❌ Config file not found: {config_path}", file=sys.stderr)
                sys.exit(1)
            sync_notifications(api, config_path, prune=args.prune)
        elif args.command == "monitors":
            sync_monitors(
                api,
                config_path=args.config,
                project_yaml_path=args.project_yaml,
                prune=args.prune,
            )
    finally:
        api.disconnect()


if __name__ == "__main__":
    main()
