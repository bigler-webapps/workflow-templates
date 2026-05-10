#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

import yaml


def fail(message: str) -> None:
    print(f"❌ {message}", file=sys.stderr)
    sys.exit(1)


def load_inventory(path: Path) -> dict:
    if not path.exists():
        fail(f"Inventory file not found: {path}")

    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        fail(f"Failed to parse inventory file: {exc}")

    targets = data.get("targets")
    if not isinstance(targets, dict) or not targets:
        fail("Inventory must contain a non-empty 'targets' mapping")

    return targets


def normalize_target(name: str, raw_target: dict) -> dict:
    target = dict(raw_target or {})
    target["target"] = name
    target.setdefault("github_environment", name)
    target.setdefault("deploy_user", "deploy")
    target.setdefault("roles", [])
    target.setdefault("sync_staging_apps", [])
    target.setdefault("expected_container_tokens", [])
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve inventory targets for GitHub workflow matrices.")
    parser.add_argument("--inventory", default="inventory/inventory.yaml", help="Path to the inventory YAML file.")
    parser.add_argument("--target", help="Single target to resolve.")
    parser.add_argument("--role", help="Resolve all targets carrying this role.")
    parser.add_argument("--require-role", help="Require that the selected target(s) contain this role.")
    args = parser.parse_args()

    if not args.target and not args.role:
        fail("Provide either --target or --role")

    raw_targets = load_inventory(Path(args.inventory))
    normalized_targets = {
        name: normalize_target(name, raw_target)
        for name, raw_target in raw_targets.items()
    }

    if args.target:
        target = normalized_targets.get(args.target)
        if target is None:
            fail(f"Unknown target '{args.target}'")
        selected = [target]
    else:
        selected = [
            target
            for target in normalized_targets.values()
            if args.role in target.get("roles", [])
        ]
        if not selected:
            fail(f"No targets found for role '{args.role}'")

    if args.require_role:
        missing_role = [
            target["target"]
            for target in selected
            if args.require_role not in target.get("roles", [])
        ]
        if missing_role:
            fail(
                f"Target(s) missing required role '{args.require_role}': "
                + ", ".join(missing_role)
            )

    print(json.dumps({"include": selected}, separators=(",", ":")))


if __name__ == "__main__":
    main()
