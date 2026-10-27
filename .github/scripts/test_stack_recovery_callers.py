"""Executable tests for the fixed deploy and maintenance recovery callers."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / ".github/scripts"
sys.path.insert(0, str(SCRIPTS))

import guarded_deploy
import guarded_maintenance


class FakeGuard:
    fd = 73


class FakeRecovery:
    def __init__(self):
        self.events = []

    @contextlib.contextmanager
    def writer_guard(self, project):
        self.events.append(("enter", project))
        try:
            yield FakeGuard()
        finally:
            self.events.append(("exit", project))

    def gated_compose_override(self, project, containers):
        self.events.append(("gate", project))
        assert containers[0]["Config"]["Labels"]["com.docker.compose.project"] == project
        return {"services": {"backend": {"entrypoint": ["gated-existing-command"]}}}

    def maintenance_guard(self, operation):
        return self.writer_guard(None)


class DeployCallerTests(unittest.TestCase):
    def setUp(self):
        self.project = tempfile.TemporaryDirectory(prefix="guarded-deploy-")
        self.previous_cwd = os.getcwd()
        self.addCleanup(self._restore_cwd_and_cleanup)
        self.recovery = FakeRecovery()
        self.env = {
            "PROJECT_NAME": "demo-app",
            "TARGET_ENV": "staging",
            "REMOTE_PATH": self.project.name,
            "IMAGE_TAG": "a" * 40,
            "USE_BUILD_FALLBACK": "false",
            "SKIP_MIGRATE": "true",
            "GITHUB_TOKEN": "synthetic-token",
            "GITHUB_ACTOR": "synthetic-actor",
            "PERSISTENT_DIRS": "media/static",
        }

    def _restore_cwd_and_cleanup(self):
        os.chdir(self.previous_cwd)
        self.project.cleanup()

    def _docker_stub(self, *, restarting=False):
        calls = []
        compose_ps = 0

        def run(argv, **kwargs):
            nonlocal compose_ps
            argv = list(argv)
            active = self.recovery.events and self.recovery.events[-1][0] != "exit"
            calls.append((argv, kwargs, active))
            if argv == ["docker", "info"]:
                return subprocess.CompletedProcess(argv, 0)
            if argv[:2] == ["docker", "compose"] and argv[-3:] == ["ps", "-a", "-q"]:
                return subprocess.CompletedProcess(argv, 0, stdout="created-app created-db\n")
            if argv[:2] == ["docker", "compose"] and argv[-2:] == ["ps", "-q"]:
                compose_ps += 1
                ids = "old-app old-db\n" if compose_ps == 1 else "new-app new-db\n"
                return subprocess.CompletedProcess(argv, 0, stdout=ids)
            if argv[:2] == ["docker", "inspect"] and "{{.Config.Image}}" in argv:
                ids = argv[4:]
                if ids == ["old-app", "old-db"]:
                    rendered = "ghcr.io/example/demo:old\npostgres:18\n"
                elif ids:
                    rendered = "ghcr.io/example/demo:" + self.env["IMAGE_TAG"] + "\npostgres:18\n"
                else:
                    rendered = ""
                return subprocess.CompletedProcess(argv, 0, stdout=rendered)
            if argv[:2] == ["docker", "inspect"] and "--format" not in argv:
                return subprocess.CompletedProcess(argv, 0, stdout=json.dumps([{
                    "Config": {"Labels": {"com.docker.compose.project": "demo-app_staging"}},
                }]))
            if argv[:2] == ["docker", "inspect"] and "{{.Name}} {{.State.Restarting}}" in argv:
                row = "/demo-backend true\n" if restarting else "/demo-backend false\n"
                return subprocess.CompletedProcess(argv, 0, stdout=row)
            if argv[:3] == ["docker", "ps", "-aq"]:
                return subprocess.CompletedProcess(argv, 0, stdout="new-app new-db\n")
            if argv[:3] == ["docker", "image", "rm"]:
                return subprocess.CompletedProcess(argv, 0)
            return subprocess.CompletedProcess(argv, 0, stdout="")

        return calls, run

    def test_deploy_holds_host_guard_through_migration_health_and_image_cleanup(self):
        calls, docker_run = self._docker_stub()
        self.env["SKIP_MIGRATE"] = "false"
        with mock.patch.object(guarded_deploy.subprocess, "run", side_effect=docker_run), \
             mock.patch.object(guarded_deploy.time, "sleep"):
            guarded_deploy.deploy(self.env, self.recovery)

        self.assertEqual(self.recovery.events, [("enter", "demo-app_staging"), ("gate", "demo-app_staging"), ("exit", "demo-app_staging")])
        modifying = [
            (argv, kwargs, active) for argv, kwargs, active in calls
            if argv[:2] == ["docker", "image"] or "pull" in argv or "up" in argv
            or "migrate" in argv or "image" in argv and "rm" in argv
        ]
        self.assertTrue(modifying)
        self.assertTrue(all(active for _, _, active in modifying))
        self.assertTrue(all(kwargs.get("pass_fds") == (FakeGuard.fd,) for _, kwargs, _ in modifying))
        self.assertTrue(all("GITHUB_TOKEN" not in kwargs.get("env", {}) for _, kwargs, _ in calls))
        self.assertTrue(all("HRAM_ENGINE_READ_TOKEN" not in kwargs.get("env", {}) for _, kwargs, _ in calls))
        self.assertLess(
            next(i for i, (argv, _, _) in enumerate(calls) if "migrate" in argv),
            next(i for i, (argv, _, _) in enumerate(calls) if argv[:3] == ["docker", "image", "rm"]),
        )
        self.assertTrue((Path(self.project.name) / "media/static").is_dir())

    def test_restart_loop_fails_before_old_image_cleanup(self):
        calls, docker_run = self._docker_stub(restarting=True)
        with mock.patch.object(guarded_deploy.subprocess, "run", side_effect=docker_run), \
             mock.patch.object(guarded_deploy.time, "sleep"):
            with self.assertRaises(guarded_deploy.DeployError):
                guarded_deploy.deploy(self.env, self.recovery)
        self.assertFalse(any(argv[:3] == ["docker", "image", "rm"] for argv, _, _ in calls))
        self.assertEqual(self.recovery.events[-1], ("exit", "demo-app_staging"))

    def test_unavailable_docker_refuses_before_mutation_or_privilege_fallback(self):
        with mock.patch.object(guarded_deploy.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["docker", "info"])) as run:
            with self.assertRaises(guarded_deploy.DeployError):
                guarded_deploy.deploy(self.env, self.recovery)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0], ["docker", "info"])
        self.assertEqual(self.recovery.events[-1], ("exit", "demo-app_staging"))

    def test_build_token_is_limited_to_the_fixed_fallback_build(self):
        calls, docker_run = self._docker_stub()
        self.env["USE_BUILD_FALLBACK"] = "true"
        self.env["HRAM_ENGINE_READ_TOKEN"] = "synthetic-build-token"
        with mock.patch.object(guarded_deploy.subprocess, "run", side_effect=docker_run):
            guarded_deploy.deploy(self.env, self.recovery)
        build_calls = [(argv, kwargs) for argv, kwargs, _ in calls if argv[-1] == "build"]
        self.assertEqual(len(build_calls), 1)
        build_env = build_calls[0][1]["env"]
        self.assertEqual(build_env.get("HRAM_ENGINE_READ_TOKEN"), "synthetic-build-token")
        self.assertEqual(build_env.get("DOCKER_BUILDKIT"), "1")
        self.assertTrue(all("HRAM_ENGINE_READ_TOKEN" not in kwargs.get("env", {}) for argv, kwargs, _ in calls if argv[-1] != "build"))
        self.assertTrue(all("GITHUB_TOKEN" not in kwargs.get("env", {}) for _, kwargs, _ in calls))

    def test_missing_recovery_contract_fails_before_any_docker_operation(self):
        no_contract = types.ModuleType("recovery")
        @contextlib.contextmanager
        def refuse(project):
            raise OSError("admission refused")
            yield

        no_contract.writer_guard = refuse
        with mock.patch.object(guarded_deploy.subprocess, "run") as run:
            with self.assertRaises(OSError):
                guarded_deploy.deploy(self.env, no_contract)
            run.assert_not_called()


class MaintenanceCallerTests(unittest.TestCase):
    def test_apt_commands_keep_the_all_project_guard_for_the_whole_lifecycle(self):
        recovery = FakeRecovery()
        calls = []
        apt_list_calls = 0

        def run(argv, **kwargs):
            nonlocal apt_list_calls
            active = recovery.events and recovery.events[-1][0] != "exit"
            calls.append((list(argv), kwargs, active))
            if "list" in argv:
                apt_list_calls += 1
                output = "Listing...\npackage/stable 2 amd64 [upgradable from: 1]\n" if apt_list_calls == 1 else "Listing...\n"
                return subprocess.CompletedProcess(argv, 0, stdout=output)
            return subprocess.CompletedProcess(argv, 0, stdout="")

        with mock.patch.object(guarded_maintenance, "_load_recovery", return_value=recovery), \
             mock.patch.object(guarded_maintenance.subprocess, "run", side_effect=run):
            guarded_maintenance.run("apt", contract_required=True)

        self.assertEqual(recovery.events, [("enter", None), ("exit", None)])
        self.assertTrue(calls)
        self.assertTrue(all(active for _, _, active in calls))
        self.assertTrue(all(kwargs.get("pass_fds") == (FakeGuard.fd,) for _, kwargs, _ in calls))
        self.assertEqual(calls[0][0][-3:], ["apt-get", "update", "-y"])

    def test_missing_required_contract_fails_closed(self):
        with mock.patch.object(guarded_maintenance, "_load_recovery", return_value=None), \
             mock.patch.object(guarded_maintenance, "_run") as run:
            with self.assertRaises(guarded_maintenance.MaintenanceError):
                guarded_maintenance.run("apt", contract_required=True)
            run.assert_not_called()

    def test_optional_inventory_without_contract_is_the_only_unlocked_maintenance_path(self):
        calls = []

        def run(argv, **kwargs):
            calls.append((list(argv), kwargs))
            return subprocess.CompletedProcess(argv, 0, stdout="Listing...\n")

        with mock.patch.object(guarded_maintenance, "_load_recovery", return_value=None), \
             mock.patch.object(guarded_maintenance, "_run", side_effect=run):
            guarded_maintenance.run("apt", contract_required=False)
        self.assertTrue(calls)
        self.assertTrue(all("pass_fds" not in kwargs for _, kwargs in calls))

    def test_reboot_request_is_fixed_and_retains_guard_until_shutdown(self):
        recovery = FakeRecovery()
        status_dir = Path(tempfile.mkdtemp(prefix="guarded-reboot-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(status_dir, ignore_errors=True))
        calls = []

        def run(argv, **kwargs):
            calls.append((list(argv), kwargs, recovery.events[-1][0] != "exit"))
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(guarded_maintenance, "_load_recovery", return_value=recovery), \
             mock.patch.object(guarded_maintenance.subprocess, "run", side_effect=run), \
             mock.patch.object(guarded_maintenance.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                guarded_maintenance.run("reboot", contract_required=True, status_dir=status_dir)

        self.assertEqual(calls[0][0][-2:], ["systemctl", "reboot"])
        self.assertEqual(calls[0][1].get("pass_fds"), (FakeGuard.fd,))
        self.assertTrue(calls[0][2])
        self.assertTrue((status_dir / "guard-acquired").exists())
        self.assertTrue((status_dir / "reboot-requested").exists())
        self.assertFalse((status_dir / "reboot-failed").exists())


class ActionAdoptionTests(unittest.TestCase):
    def test_restore_callers_ship_module_and_use_transaction_owned_finalization(self):
        restore = (ROOT / ".github/actions/restore/action.yml").read_text(encoding="utf-8")
        cross = (ROOT / ".github/actions/restore-dest-import/action.yml").read_text(encoding="utf-8")
        self.assertIn('stack_recovery.py missing; recovery contract is required', restore)
        self.assertIn('IDENTITY_HASH="$(printf', restore)
        self.assertIn('"$APP_NAME" "$SSH_HOST" "$TARGET_ENV" "$RECOVERY_SOURCE" | sha256sum', restore)
        self.assertIn('--operation-id "$RECOVERY_OPERATION_ID"', restore)
        self.assertIn('--recovery-source "$RECOVERY_SOURCE"', restore)
        self.assertIn('"${OPS_SCRIPTS_PATH}/stack_recovery.py"', cross)
        self.assertIn('IDENTITY_HASH="$(printf', cross)
        self.assertIn('"$DEST_TARGET" "$DEST_SUBTARGET" "$RECOVERY_SOURCE" | sha256sum', cross)
        self.assertIn('--operation-id "$RECOVERY_OPERATION_ID"', cross)
        self.assertIn('--recovery-source "$RECOVERY_SOURCE"', cross)
        self.assertNotIn('docker compose up -d', cross)
        self.assertIn('required: true', cross.split('recovery_source:', 1)[1].split('ssh_host:', 1)[0])

    def test_maintenance_action_requires_contract_by_default_and_verifies_reboot(self):
        action = (ROOT / ".github/actions/maintenance/action.yml").read_text(encoding="utf-8")
        self.assertIn("recovery_contract_required:", action)
        self.assertIn("default: 'true'", action)
        self.assertIn("--contract-required", action)
        self.assertIn("reboot-requested", action)
        self.assertIn("boot_id_before=$BOOT_ID_BEFORE", action)
        self.assertIn('"$BOOT_ID_AFTER" != "$BOOT_ID_BEFORE"', action)
        trigger = action.split("Trigger reboot", 1)[1].split("Wait for host", 1)[0]
        self.assertNotIn("|| true", trigger)


if __name__ == "__main__":
    unittest.main()
