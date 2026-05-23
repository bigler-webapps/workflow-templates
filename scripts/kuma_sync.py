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
from pathlib import Path

import yaml

try:
    from uptime_kuma_api import UptimeKumaApi
except ImportError:
    print("❌ uptime-kuma-api not installed. Run: pip install uptime-kuma-api", file=sys.stderr)
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

    api = UptimeKumaApi(url)
    api.login(user, password)
    return api


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


def sync_monitors(api, config_path, prune=False):
    with open(config_path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    specs = data.get("monitors", [])
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
    sm.add_argument("--config", required=True, help="Path to monitor.yml")
    sm.add_argument("--prune", action="store_true",
                    help="Delete Kuma monitors absent from the config")

    args = parser.parse_args()
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"❌ Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    api = _login()
    try:
        if args.command == "notifications":
            sync_notifications(api, config_path, prune=args.prune)
        elif args.command == "monitors":
            sync_monitors(api, config_path, prune=args.prune)
    finally:
        api.disconnect()


if __name__ == "__main__":
    main()
