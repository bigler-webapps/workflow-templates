"""Run the maintenance action's fixed apt/reboot operations under recovery admission."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


RECOVERY_MODULE_DIR = "/usr/local/lib/webapp-recovery"


class MaintenanceError(RuntimeError):
    pass


def _load_recovery():
    if RECOVERY_MODULE_DIR not in sys.path:
        sys.path.insert(0, RECOVERY_MODULE_DIR)
    try:
        import stack_recovery
    except ImportError as exc:
        return None
    return stack_recovery


def _argv(command: list[str]) -> list[str]:
    prefix = [] if getattr(os, "geteuid", lambda: 1000)() == 0 else ["sudo", "-n"]
    return [*prefix, *command]


def _run(command: list[str], guard=None, **kwargs) -> subprocess.CompletedProcess:
    argv = _argv(command)
    if guard is None:
        return subprocess.run(argv, **kwargs)
    return subprocess.run(argv, pass_fds=(guard.fd,), **kwargs)


def _run_apt(guard) -> None:
    print("Refreshing apt index.", flush=True)
    _run(["apt-get", "update", "-y"], guard=guard, check=True)

    before = _run(["apt", "list", "--upgradable"], guard=guard,
                  check=True, capture_output=True, text=True).stdout
    before_count = sum(bool(line.strip()) for line in before.splitlines()[1:])
    print(f"Upgradable packages before upgrade: {before_count}", flush=True)

    if before_count:
        options = ["-y", "-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold"]
        print("Installing upgrades.", flush=True)
        _run(["apt-get", *options, "upgrade"], guard=guard, check=True)
        _run(["apt-get", *options, "autoremove"], guard=guard, check=True)
        _run(["apt-get", "-y", "autoclean"], guard=guard, check=True)
    else:
        print("No package upgrades required.", flush=True)

    after = _run(["apt", "list", "--upgradable"], guard=guard,
                 check=True, capture_output=True, text=True).stdout
    after_count = sum(bool(line.strip()) for line in after.splitlines()[1:])
    print(f"Upgradable packages after upgrade: {after_count}", flush=True)


def _write_status(status_dir: Path | None, marker: str) -> None:
    if status_dir is None:
        return
    status_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker_path = status_dir / marker
    marker_path.write_text("\n", encoding="utf-8")


def _run_reboot(guard, status_dir: Path | None) -> None:
    first = _run(["systemctl", "reboot"], guard=guard, check=False,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if first.returncode != 0:
        second = _run(["reboot"], guard=guard, check=False,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if second.returncode != 0:
            raise MaintenanceError("guarded reboot request failed")
    _write_status(status_dir, "reboot-requested")

    # systemctl may accept the request and return before shutdown starts. Keep
    # the all-project guard alive until the host begins shutting down; a
    # detached process is terminated by shutdown while still holding its fd.
    for _ in range(120):
        time.sleep(1)
    raise MaintenanceError("host did not begin shutdown after the guarded reboot request")


def run(operation: str, contract_required: bool, status_dir: Path | None = None) -> None:
    recovery = _load_recovery()
    try:
        if recovery is None:
            if contract_required:
                raise MaintenanceError("required destination recovery contract is unavailable")
            print("Recovery contract is not required for this inventory target.", flush=True)
            if operation == "verify-reboot":
                return
            if operation == "apt":
                _run_apt(None)
            else:
                _run_reboot(None, status_dir)
            return

        if operation == "verify-reboot":
            recovery.reconcile_completed_maintenance_reboot()
            if recovery.read_host_reservation():
                raise MaintenanceError("host maintenance reservation remains unresolved")
            return
        with recovery.maintenance_guard(operation) as guard:
            _write_status(status_dir, "guard-acquired")
            if operation == "apt":
                _run_apt(guard)
            else:
                _run_reboot(guard, status_dir)
    except Exception:
        _write_status(status_dir, "reboot-failed")
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("apt", "reboot", "verify-reboot"))
    parser.add_argument("--contract-required", choices=("true", "false"), required=True)
    parser.add_argument("--status-dir", type=Path)
    args = parser.parse_args()
    try:
        run(args.operation, args.contract_required == "true", args.status_dir)
    except (MaintenanceError, OSError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
