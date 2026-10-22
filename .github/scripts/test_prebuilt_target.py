"""Offline behavioral tests for the target-only immutable image executor."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import io
import builtins
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import prebuilt_target as pt
import release_manifest as rm


IDENTITY = rm.ReleaseIdentity(
    repository="bigler-webapps/sample-app", sha="0123456789abcdef0123456789abcdef01234567",
    ref="refs/heads/develop", workflow=".github/workflows/release.yml", run_id=123456, attempt=2,
)
NAMESPACE = "ghcr.io/bigler-webapps/sample-app-backend"
DIGEST = "sha256:" + "a" * 64
IMAGE = NAMESPACE + "@" + DIGEST
SIDECAR = "ghcr.io/library/postgres@sha256:" + "b" * 64
TAG = rm.release_image_tag(IDENTITY)


def registry_read(namespace, tag):
    return {"repository": namespace, "tag": tag, "digest": DIGEST}


class FakeExecutor:
    def __init__(self, *, config=None, ps=None, inspect=None, fail_at=None, hook=None):
        self.calls = []
        self.fail_at = fail_at
        self.hook = hook
        self.config = config if config is not None else {
            "services": {
                "web": {"image": IMAGE}, "worker": {"image": IMAGE}, "db": {"image": SIDECAR},
            },
        }
        self.ps = ps
        self.inspect = inspect

    def run(self, argv, *, cwd, env, timeout, capture=False):
        self.calls.append((argv, cwd, dict(env), timeout, capture))
        if self.hook:
            self.hook(argv)
        if self.fail_at and self.fail_at(argv):
            return pt._CommandResult(17, b"synthetic-secret-child-output")
        joined = " ".join(argv)
        if "config --format json" in joined:
            data = json.dumps(self.config, separators=(",", ":")).encode()
            return pt._CommandResult(0, data)
        if "ps --all --format json" in joined:
            rows = self.ps if self.ps is not None else [
                {"ID": "a" * 64, "Name": "sample-web-1", "Project": "sample", "Service": "web", "State": "running", "Health": "healthy", "Command": "synthetic-private-command", "ExitCode": 0},
                {"ID": "c" * 64, "Name": "sample-worker-1", "Project": "sample", "Service": "worker", "State": "running", "Health": "", "Command": "synthetic-private-command", "ExitCode": 0},
                {"ID": "d" * 64, "Name": "sample-db-1", "Project": "sample", "Service": "db", "State": "running", "Health": "healthy", "Command": "synthetic-private-command", "ExitCode": 0},
            ]
            if isinstance(rows, bytes):
                data = rows
            elif isinstance(rows, str):
                data = rows.encode()
            elif isinstance(rows, list):
                data = b"".join(json.dumps(row, separators=(",", ":")).encode() + b"\n" for row in rows)
            else:
                data = json.dumps(rows, separators=(",", ":")).encode()
            return pt._CommandResult(0, data)
        if "inspect" in argv:
            service = "web" if argv[-1] == "a" * 64 else "worker" if argv[-1] == "c" * 64 else "db"
            values = self.inspect(service) if callable(self.inspect) else self.inspect
            values = values or (IMAGE if service in ("web", "worker") else SIDECAR,
                                "running", False, "healthy" if service != "worker" else None,
                                "sample", service)
            return pt._CommandResult(0, "".join(json.dumps(value) + "\n" for value in values).encode())
        return pt._CommandResult(0, b"")


class TargetFixture(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.object(pt.subprocess, "Popen", side_effect=AssertionError("real subprocess forbidden")).start()
        patch("socket.socket", side_effect=AssertionError("real socket forbidden")).start()
        # The managed Windows runner denies AppData temp ACL changes; keep the
        # disposable fixture under this writable checkout instead.
        self.temp = tempfile.TemporaryDirectory(dir=SCRIPT_DIR.parents[1] if os.name == "nt" else None)
        self.root = Path(self.temp.name)
        self.app = self.root / "application"
        self.job = self.root / "private-job"
        self.docker = self.root / "docker-config"
        self.tmp = self.root / "private-tmp"
        for path in (self.app, self.job, self.docker, self.tmp):
            path.mkdir(mode=0o700)
            if os.name != "nt":
                path.chmod(0o700)
        (self.app / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
        self.control_hash = hashlib.sha256((self.app / "compose.yaml").read_bytes()).hexdigest()
        (self.job / "runtime-input.txt").write_text("synthetic=never-read\n", encoding="utf-8")
        (self.docker / "config.json").write_text('{"auths":{"ghcr.io":{}}}', encoding="utf-8")
        if os.name != "nt":
            (self.job / "runtime-input.txt").chmod(0o600)
            (self.app / "compose.yaml").chmod(0o644)
            (self.docker / "config.json").chmod(0o600)
        self.target = pt.TargetSpec(
            project_name="sample", environment="staging", application_dir=str(self.app),
            compose=(pt.ComposeControl("compose.yaml", self.control_hash),),
            image_services=("web", "worker"), sidecar_images=(("db", SIDECAR),),
            runtime_env_path=str(self.job / "runtime-input.txt"), docker_config_dir=str(self.docker),
            temporary_parent=str(self.tmp), migrate_service="web",
        )
        self.raw = rm.produce_image_manifest(
            IDENTITY, expected_namespace=NAMESPACE, pushed_tag=TAG, resolve_pushed_image=registry_read,
        )
        self.resolver_calls = []

        def resolver(namespace, tag):
            self.resolver_calls.append((namespace, tag))
            return registry_read(namespace, tag)
        self.resolver = resolver

    def tearDown(self):
        self.temp.cleanup()

    def execute(self, **kwargs):
        return pt.execute_image_deployment(
            kwargs.pop("raw_manifest", self.raw), expected_identity=kwargs.pop("expected_identity", IDENTITY),
            expected_image_namespace=kwargs.pop("expected_image_namespace", NAMESPACE),
            resolve_pushed_image=kwargs.pop("resolve_pushed_image", self.resolver),
            target=kwargs.pop("target", self.target), **kwargs,
        )

    def assert_private_temp_empty(self):
        self.assertEqual(list(self.tmp.iterdir()), [])


class PrebuiltTargetTests(TargetFixture):
    def test_success_uses_validated_digest_and_observed_services(self):
        executor = FakeExecutor()
        result = self.execute(executor=executor)
        self.assertEqual(result.image, IMAGE)
        self.assertEqual(result.services, ("db", "web", "worker"))
        self.assertEqual((result.source_repository, result.source_sha, result.run_id, result.attempt),
                         (IDENTITY.repository, IDENTITY.sha, IDENTITY.run_id, IDENTITY.attempt))
        self.assertTrue(result.migrated)
        self.assertEqual(self.resolver_calls, [(NAMESPACE, TAG)])
        self.assert_private_temp_empty()
        commands = [call[0] for call in executor.calls]
        up = next(command for command in commands if "up" in command)
        self.assertEqual(up[up.index("up"):],
                         ("up", "--detach", "--no-build", "--pull", "never", "--force-recreate", "--remove-orphans"))
        phases = ["inspect" if "inspect" in c else next(p for p in ("config", "pull", "up", "exec", "ps") if p in c) for c in commands]
        self.assertEqual(phases, ["config", "pull", "up", "exec", "ps", "inspect", "inspect", "inspect"])
        self.assertTrue(all(c[:3] == ("docker", "--host", "unix:///var/run/docker.sock") for c in commands))
        self.assertTrue(any("migrate" in command and "--noinput" in command for command in commands))
        self.assertTrue(all("build" not in command for command in commands if command is not up))
        for _, _, child_env, _, _ in executor.calls:
            self.assertEqual(set(child_env), {"PATH", "HOME", "LANG", "LC_ALL", "DOCKER_CONFIG"})
            self.assertEqual(child_env["DOCKER_CONFIG"], str(self.docker))

    def test_varied_release_identity_digest_and_service_are_not_constants(self):
        identity = replace(IDENTITY, repository="other/service", sha="f"*40, run_id=76543, attempt=7)
        namespace = "ghcr.io/other/service-backend"
        digest = "sha256:"+"c"*64
        image = namespace+"@"+digest
        tag = rm.release_image_tag(identity)
        def resolver(ns, release_tag):
            self.assertEqual((ns, release_tag), (namespace, tag))
            return {"repository": ns, "tag": release_tag, "digest": digest}
        raw = rm.produce_image_manifest(identity, expected_namespace=namespace,
            pushed_tag=tag, resolve_pushed_image=resolver)
        target = replace(self.target, image_services=("api",), sidecar_images=(), migrate_service=None)
        executor = FakeExecutor(config={"services": {"api": {"image": image}}},
            ps=[{"ID": "e"*64, "Project": "sample", "Service": "api", "State": "running", "Health": ""}],
            inspect=(image, "running", False, None, "sample", "api"))
        result = self.execute(raw_manifest=raw, expected_identity=identity,
            expected_image_namespace=namespace, resolve_pushed_image=resolver, target=target, executor=executor)
        self.assertEqual((result.image, result.source_repository, result.source_sha, result.run_id,
                          result.attempt, result.services, result.migrated),
            (image, "other/service", "f"*40, 76543, 7, ("api",), False))
        self.assertFalse(any("exec" in call[0] for call in executor.calls))
        self.assert_private_temp_empty()

    def test_runtime_contents_are_never_opened_and_inspect_is_narrow(self):
        original = os.open
        builtin_open, io_open = builtins.open, io.open
        runtime = Path(self.target.runtime_env_path)
        def guarded(path, *args, **kwargs):
            self.assertNotEqual(Path(path), runtime, "runtime input must remain metadata-only")
            return original(path, *args, **kwargs)
        def guard_reader(function):
            def read(path, *args, **kwargs):
                if not isinstance(path, int): self.assertNotEqual(Path(path), runtime)
                return function(path, *args, **kwargs)
            return read
        executor = FakeExecutor()
        with patch.object(pt.os, "open", side_effect=guarded), \
             patch.object(builtins, "open", side_effect=guard_reader(builtin_open)), \
             patch.object(io, "open", side_effect=guard_reader(io_open)):
            self.execute(executor=executor)
        templates = [call[0][call[0].index("--format")+1] for call in executor.calls if "inspect" in call[0]]
        self.assertEqual(len(templates), 3)
        self.assertTrue(all("Config.Env" not in t and "if .State.Health" in t for t in templates))
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.execute(executor=FakeExecutor())
            with self.assertRaises(pt.PrebuiltTargetError) as caught:
                self.execute(executor=FakeExecutor(fail_at=lambda a: "ps" in a))
        self.assertEqual((output.getvalue(), errors.getvalue()), ("", ""))
        self.assertEqual(str(caught.exception), "command_failed")

    def test_invalid_clock_and_deadline_values_do_not_reach_registry_or_commands(self):
        for value in (True, float("nan"), float("inf"), 10**400):
            executor = FakeExecutor()
            with self.assertRaises(pt.PrebuiltTargetError):
                self.execute(clock=lambda: value, executor=executor)
            with self.assertRaises(pt.PrebuiltTargetError):
                self.execute(clock=lambda: 100.0, deadline=value, executor=executor)
            self.assertEqual(executor.calls, [])
        self.assertEqual(self.resolver_calls, [])

    def test_ps_project_duplicate_id_and_unhealthy_state_reject_after_up(self):
        base = [
            {"ID": "a"*64, "Project": "sample", "Service": "web", "State": "running", "Health": "healthy"},
            {"ID": "c"*64, "Project": "sample", "Service": "worker", "State": "running", "Health": ""},
            {"ID": "d"*64, "Project": "sample", "Service": "db", "State": "running", "Health": "healthy"},
        ]
        for changes in ({"Project": "other"}, {"Project": None}, {"ID": "c"*64},
                        {"Health": "unhealthy"}, {"ExitCode": True}, {"Service": []}):
            executor = FakeExecutor(ps=[dict(base[0], **changes), *base[1:]])
            with self.assertRaises(pt.PrebuiltTargetError): self.execute(executor=executor)
            self.assertTrue(any("up" in call[0] for call in executor.calls))
            self.assertFalse(any("inspect" in call[0] for call in executor.calls))
            self.assert_private_temp_empty()

    @unittest.skipIf(os.name == "nt", "target POSIX link semantics")
    def test_control_symlink_hardlink_and_writable_parent_reject_before_registry(self):
        control = self.app/"compose.yaml"
        saved = self.app/"saved.yaml"
        control.rename(saved); control.symlink_to(saved)
        executor = FakeExecutor()
        with self.assertRaises(pt.PrebuiltTargetError): self.execute(executor=executor)
        self.assertEqual(executor.calls, [])
        control.unlink(); os.link(saved, control)
        with self.assertRaises(pt.PrebuiltTargetError): self.execute(executor=executor)
        self.assertEqual(executor.calls, [])
        control.unlink(); saved.rename(control)
        self.app.chmod(0o777)
        with self.assertRaises(pt.PrebuiltTargetError): self.execute(executor=executor)
        self.assertEqual(executor.calls, [])
        self.app.chmod(0o700)
        self.assertEqual(self.resolver_calls, [])

    def test_runtime_control_alias_rejects_before_open_or_callback(self):
        control = self.app/"compose.yaml"
        if os.name != "nt": control.chmod(0o600)
        executor = FakeExecutor()
        with patch.object(pt, "_read_control", side_effect=AssertionError("must not open runtime alias")):
            with self.assertRaisesRegex(pt.PrebuiltTargetError, "runtime_scope_invalid"):
                self.execute(target=replace(self.target, runtime_env_path=str(control)), executor=executor)
        self.assertEqual((self.resolver_calls, executor.calls), ([], []))

    def test_private_runtime_config_and_temporary_paths_stay_outside_application_tree(self):
        cases = [replace(self.target, runtime_env_path=str(self.app/"private-runtime.txt")),
                 replace(self.target, docker_config_dir=str(self.app/"private-docker")),
                 replace(self.target, temporary_parent=str(self.app/"private-tmp"))]
        for target in cases:
            executor = FakeExecutor()
            with patch.object(pt, "_private_inputs", side_effect=AssertionError("reject before filesystem")):
                with self.assertRaisesRegex(pt.PrebuiltTargetError, "runtime_scope_invalid"):
                    self.execute(target=target, executor=executor)
            self.assertEqual(executor.calls, [])
        self.assertEqual(self.resolver_calls, [])

    def test_complete_legacy_compose_ps_array_is_supported(self):
        rows = [{"ID": "a"*64, "Project": "sample", "Service": "web", "State": "running", "Health": "healthy"},
                {"ID": "c"*64, "Project": "sample", "Service": "worker", "State": "running", "Health": ""},
                {"ID": "d"*64, "Project": "sample", "Service": "db", "State": "running", "Health": "healthy"}]
        executor = FakeExecutor(ps=json.dumps(rows).encode())
        result = self.execute(executor=executor)
        self.assertEqual((result.image,result.services), (IMAGE,("db","web","worker")))
        self.assertEqual(sum("inspect" in call[0] for call in executor.calls), 3)
        self.assert_private_temp_empty()

    def test_configured_health_requires_observed_healthy_state(self):
        for health, disabled, succeeds in ((None, False, False), ("healthy", False, True), (None, True, True)):
            executor = FakeExecutor()
            executor.config["services"]["web"]["healthcheck"] = {"test": ["CMD", "true"], "disable": disabled}
            executor.inspect = lambda service: (IMAGE if service != "db" else SIDECAR,
                "running", False, health if service == "web" else None, "sample", service)
            if succeeds:
                self.assertEqual(self.execute(executor=executor).image, IMAGE)
            else:
                with self.assertRaisesRegex(pt.PrebuiltTargetError, "container_unhealthy"):
                    self.execute(executor=executor)
            self.assertTrue(any("inspect" in call[0] for call in executor.calls))
            self.assert_private_temp_empty()

    def test_override_is_real_private_yaml_with_build_reset_then_removed(self):
        observed = []

        def inspect_command(argv):
            if "config" in argv:
                override = Path(argv[argv.index("--file", argv.index("--file") + 1) + 1])
                observed.append((override, override.stat().st_mode & 0o777, override.read_text(encoding="utf-8")))
        self.execute(executor=FakeExecutor(hook=inspect_command))
        path, mode, payload = observed[0]
        if os.name != "nt":
            self.assertEqual(mode, 0o600)
        self.assertEqual(payload, 'services:\n' + ''.join(
            f'  {s}:\n    image: "{IMAGE}"\n    build: !reset null\n'
            f'    env_file: [{json.dumps(self.target.runtime_env_path)}]\n' for s in ('web', 'worker')))
        self.assertFalse(path.exists())

    def test_initial_temp_identity_failure_cleans_empty_owned_directory(self):
        identity = pt._identity
        def broken(path, **kwargs):
            if Path(path).parent == self.tmp:
                raise pt.PrebuiltTargetError("synthetic_identity_failure")
            return identity(path, **kwargs)
        executor = FakeExecutor()
        with patch.object(pt, "_identity", side_effect=broken):
            with self.assertRaisesRegex(pt.PrebuiltTargetError, "synthetic_identity_failure"):
                self.execute(executor=executor)
        self.assertEqual(executor.calls, [])
        self.assertEqual(self.resolver_calls, [(NAMESPACE,TAG)])
        self.assert_private_temp_empty()

    def test_default_executor_is_selected_with_fixed_local_endpoint(self):
        executor = FakeExecutor()
        with patch.object(pt, "_SubprocessExecutor", return_value=executor) as factory:
            result = self.execute()
        factory.assert_called_once_with()
        self.assertEqual(result.image, IMAGE)
        self.assertTrue(executor.calls)
        self.assertTrue(all(c[0][:3] == ("docker", "--host", "unix:///var/run/docker.sock") for c in executor.calls))
        self.assertTrue(all("DOCKER_HOST" not in c[2] and "DOCKER_CONTEXT" not in c[2] for c in executor.calls))
        self.assert_private_temp_empty()

    def test_docker_profile_rejects_helpers_plugins_contexts_and_unscoped_credentials(self):
        profile = self.docker/"config.json"
        for value in ({}, {"auths":{}}, {"auths":{"other.example":{}}},
                      {"auths":{"ghcr.io":{}}, "credsStore":"synthetic-helper"},
                      {"auths":{"ghcr.io":{}}, "credHelpers":{"ghcr.io":"synthetic-helper"}},
                      {"auths":{"ghcr.io":{}}, "currentContext":"synthetic-remote"},
                      {"auths":{"ghcr.io":{}}, "cliPluginsExtraDirs":["/tmp/plugins"]},
                      {"auths":{"ghcr.io":{"auth":"not-base64"}}}):
            profile.write_text(json.dumps(value),encoding="utf-8")
            executor = FakeExecutor()
            with self.assertRaisesRegex(pt.PrebuiltTargetError,"docker_profile_invalid"):
                self.execute(executor=executor)
            self.assertEqual(executor.calls,[])
        profile.write_text('{"auths":{"ghcr.io":{}}}',encoding="utf-8")
        (self.docker/"cli-plugins").mkdir()
        with self.assertRaisesRegex(pt.PrebuiltTargetError,"docker_profile_invalid"):
            self.execute(executor=FakeExecutor())
        self.assertEqual(self.resolver_calls,[])

    def test_compose_external_references_and_unsafe_yaml_reject_before_credentials(self):
        cases = ["include: child.yaml\nservices: {}\n", "include:\n  - path: child.yaml\nservices: {}\n",
                 "services:\n  web:\n    extends: {file: child.yaml, service: base}\n",
                 "services:\n  web:\n    env_file: child.txt\n", "services:\n  web:\n    label_file: child.txt\n",
                 "configs:\n  app: {file: child.txt}\nservices: {}\n", "secrets:\n  key: {file: child.txt}\nservices: {}\n",
                 "services: {}\nservices: {}\n", "services: &base {}\nx: *base\n", "services: !!python/object:fixture {}\n"]
        control = self.app/"compose.yaml"
        for content in cases:
            control.write_text(content,encoding="utf-8")
            target = replace(self.target,compose=(pt.ComposeControl("compose.yaml",hashlib.sha256(content.encode()).hexdigest()),))
            executor=FakeExecutor()
            with patch.object(pt,"_docker_profile",side_effect=AssertionError("no credentials before control validation")):
                with self.assertRaises(pt.PrebuiltTargetError):self.execute(target=target,executor=executor)
            self.assertEqual(executor.calls,[])
        self.assertEqual(self.resolver_calls,[])

    def test_pinned_yaml_parser_and_bounded_node_subset_have_positive_control(self):
        import yaml
        path = SCRIPT_DIR.parents[1]/"toolchains/compose-parser-requirements.txt"
        self.assertIn("PyYAML==6.0.3",path.read_text(encoding="utf-8").splitlines())
        self.assertEqual(yaml.__version__,"6.0.3")
        pt._validate_compose_control(b'services:\n  web:\n    environment:\n      NOTE: "include: data, not a directive"\n    volumes: ["app-data:/data"]\nvolumes:\n  app-data: {}\n')
        with patch.object(yaml,"__version__","0.0.0"):
            with self.assertRaisesRegex(pt.PrebuiltTargetError,"compose_parser_unavailable"):
                self.execute(executor=FakeExecutor())
        for content in (b"services: "+b"["*65+b"]"*65, b"services: {}\nx: ["+b"1,"*4096+b"]"):
            with self.assertRaises(pt.PrebuiltTargetError):pt._validate_compose_control(content)

    def test_scoped_synthetic_docker_auth_is_never_logged_or_returned(self):
        import base64
        sentinel = "synthetic-private-registry-token"
        value = base64.b64encode(("fixture-user:"+sentinel).encode()).decode()
        (self.docker/"config.json").write_text(json.dumps({"auths":{"ghcr.io":{"auth":value}}}),encoding="utf-8")
        output,errors = io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(output),contextlib.redirect_stderr(errors):
            result = self.execute(executor=FakeExecutor())
        self.assertEqual((output.getvalue(),errors.getvalue()),("",""))
        self.assertNotIn(sentinel,repr(result)); self.assertNotIn(value,repr(result))
        with patch.object(pt,"_docker_profile",side_effect=AssertionError("runtime alias must reject before read")):
            with self.assertRaisesRegex(pt.PrebuiltTargetError,"runtime_scope_invalid"):
                self.execute(target=replace(self.target,runtime_env_path=str(self.docker/"config.json")),executor=FakeExecutor())
        self.assert_private_temp_empty()

    def test_static_or_parsed_manifest_rejected_before_target_command(self):
        bundle = b"static target fixture"
        static = rm.produce_static_manifest(IDENTITY, artifact_id=998877, bundle=bundle, bundle_length=len(bundle))
        executor = FakeExecutor()
        with self.assertRaises(pt.PrebuiltTargetError):
            self.execute(raw_manifest=static, executor=executor)
        with self.assertRaises(pt.PrebuiltTargetError):
            self.execute(raw_manifest=json.loads(self.raw), executor=executor)
        self.assertEqual(executor.calls, [])
        self.assertEqual(self.resolver_calls, [])
        self.assert_private_temp_empty()

    def test_untrusted_target_shapes_reject_before_registry_callback(self):
        cases = [
            replace(self.target, project_name="sample;touch"),
            replace(self.target, environment="production"),
            replace(self.target, application_dir=str(self.app / ".." / "application")),
            replace(self.target, compose=(pt.ComposeControl("../compose.yaml", self.control_hash),)),
            replace(self.target, image_services=("web", "web")),
            replace(self.target, sidecar_images=(("web", SIDECAR),)),
            replace(self.target, sidecar_images=(("db", "postgres:latest"),)),
            replace(self.target, migrate_service="db"),
        ]
        for target in cases:
            with self.subTest(target=target):
                executor = FakeExecutor()
                with self.assertRaises(pt.PrebuiltTargetError):
                    self.execute(target=target, executor=executor)
                self.assertEqual(executor.calls, [])
        self.assertEqual(self.resolver_calls, [])
        self.assert_private_temp_empty()

    def test_invalid_identity_and_namespace_scalars_reject_before_profile_access(self):
        for identity, namespace in ((replace(IDENTITY, repository="bad\nrepo"),NAMESPACE),
                                    (replace(IDENTITY,sha="invalid"),NAMESPACE),
                                    (replace(IDENTITY,run_id=True),NAMESPACE),
                                    (replace(IDENTITY,run_id=10**100),NAMESPACE),
                                    (replace(IDENTITY,attempt=2**31),NAMESPACE),
                                    (replace(IDENTITY,repository="owner/"+"a"*256),NAMESPACE),
                                    (IDENTITY,"ghcr.io/bad\nrepo"), (IDENTITY,"other.example/app")):
            executor = FakeExecutor()
            with patch.object(pt,"_private_inputs",side_effect=AssertionError("must validate before credential access")):
                with self.assertRaises(pt.PrebuiltTargetError):
                    self.execute(expected_identity=identity,expected_image_namespace=namespace,executor=executor)
            self.assertEqual(executor.calls,[])
        self.assertEqual(self.resolver_calls,[])

    def test_malformed_or_cross_source_manifest_rejects_before_credential_profile(self):
        wrong=json.loads(self.raw);wrong["source_sha"]="f"*40
        static=rm.produce_static_manifest(IDENTITY,artifact_id=998877,bundle=b"fixture",bundle_length=7)
        for raw in (b"{}", b"not JSON", rm._encode_manifest(wrong),static):
            executor=FakeExecutor()
            with patch.object(pt,"_private_inputs",side_effect=AssertionError("no private credential access")):
                with self.assertRaises(pt.PrebuiltTargetError):self.execute(raw_manifest=raw,executor=executor)
            self.assertEqual(executor.calls,[])
        self.assertEqual(self.resolver_calls,[])

    def test_control_hash_and_private_runtime_permissions_reject_before_registry(self):
        executor = FakeExecutor()
        with self.assertRaises(pt.PrebuiltTargetError):
            self.execute(target=replace(self.target, compose=(pt.ComposeControl("compose.yaml", "f" * 64),)),
                         executor=executor)
        self.assertEqual(executor.calls, [])
        if os.name != "nt":
            (self.job / "runtime-input.txt").chmod(0o644)
            with self.assertRaises(pt.PrebuiltTargetError):
                self.execute(executor=executor)
            self.assertEqual(executor.calls, [])
        self.assertEqual(self.resolver_calls, [])
        self.assert_private_temp_empty()

    def test_noncanonical_sidecar_images_reject_before_registry_or_command(self):
        for name in ("ghcr.io/library/postgres/", "ghcr.io/library//postgres", "ghcr.io/library/postgres.",
                     "ghcr.io/library/../postgres", "ghcr..io/library/postgres", "-bad.io/library/postgres",
                     "ghcr.io:0/library/postgres", "ghcr.io:65536/library/postgres", "ghcr.io/"+"a"*256):
            executor = FakeExecutor()
            with self.assertRaisesRegex(pt.PrebuiltTargetError, "sidecar_image_invalid"):
                self.execute(target=replace(self.target, sidecar_images=(("db", name+"@sha256:"+"b"*64),)), executor=executor)
            self.assertEqual(executor.calls, [])
        self.assertEqual(self.resolver_calls, [])

    def test_wrong_identity_namespace_digest_and_static_never_execute(self):
        executor = FakeExecutor()
        for identity, namespace, resolver in (
            (replace(IDENTITY, sha="f" * 40), NAMESPACE, self.resolver),
            (IDENTITY, "ghcr.io/other/app-backend", self.resolver),
        ):
            with self.subTest(identity=identity, namespace=namespace):
                with self.assertRaises(pt.PrebuiltTargetError):
                    self.execute(expected_identity=identity, expected_image_namespace=namespace,
                                 resolve_pushed_image=resolver, executor=executor)
                self.assertEqual(self.resolver_calls, [])
        self.assertEqual(executor.calls, [])
        self.assert_private_temp_empty()

    def test_digest_mismatch_requires_registry_read_but_no_target_command(self):
        calls = []
        def resolver(namespace, tag):
            calls.append((namespace, tag))
            return {"repository": namespace, "tag": tag, "digest": "sha256:"+"c"*64}
        executor = FakeExecutor()
        with self.assertRaises(pt.PrebuiltTargetError):
            self.execute(resolve_pushed_image=resolver, executor=executor)
        self.assertEqual(calls, [(NAMESPACE,TAG)])
        self.assertEqual(executor.calls, [])
        self.assert_private_temp_empty()

    def test_rendered_build_unknown_service_and_wrong_image_reject_before_pull(self):
        cases = [
            {"services": {"web": {"image": IMAGE, "build": {"context": "."}},
                           "worker": {"image": IMAGE}, "db": {"image": SIDECAR}}},
            {"services": {"web": {"image": IMAGE}, "worker": {"image": IMAGE},
                           "db": {"image": SIDECAR}, "rogue": {"image": SIDECAR}}},
            {"services": {"web": {"image": NAMESPACE + ":latest"}, "worker": {"image": IMAGE},
                           "db": {"image": SIDECAR}}},
        ]
        for config in cases:
            executor = FakeExecutor(config=config)
            with self.assertRaises(pt.PrebuiltTargetError):
                self.execute(executor=executor)
            self.assertFalse(any("pull" in call[0] for call in executor.calls))
            self.assert_private_temp_empty()

    def test_ps_requires_complete_unique_running_service_set(self):
        base = [
            {"ID": "a" * 64, "Name": "sample-web-1", "Project": "sample", "Service": "web", "State": "running", "Health": "healthy"},
            {"ID": "c" * 64, "Name": "sample-worker-1", "Project": "sample", "Service": "worker", "State": "running", "Health": ""},
            {"ID": "d" * 64, "Name": "sample-db-1", "Project": "sample", "Service": "db", "State": "running", "Health": "healthy"},
        ]
        cases = [
            [], base[:-1], base + [base[0]],
            [dict(row, ID="not-a-container") if row["Service"] == "db" else row for row in base],
            [dict(row, State="exited") if row["Service"] == "worker" else row for row in base],
            [dict(row, State="restarting") if row["Service"] == "web" else row for row in base],
            b'{"ID":"' + b"a" * 64 + b'","Service":"web","State":"running","Health":"healthy"}\n{"ID":"' + b"c" * 64 + b'","Service":"worker","State":"running","Health":""}\n',
        ]
        for rows in cases:
            executor = FakeExecutor(ps=rows)
            with self.subTest(rows=type(rows).__name__, count=len(rows) if hasattr(rows, "__len__") else -1):
                with self.assertRaises(pt.PrebuiltTargetError):
                    self.execute(executor=executor)
            self.assert_private_temp_empty()

    def test_observed_container_image_project_service_and_health_are_authoritative(self):
        mutations = {
            "wrong-image": lambda s: ("ghcr.io/elsewhere/app@sha256:" + "c" * 64, "running", False, "healthy", "sample", s),
            "wrong-project": lambda s: (IMAGE if s != "db" else SIDECAR, "running", False, "healthy", "other", s),
            "wrong-service": lambda s: (IMAGE if s != "db" else SIDECAR, "running", False, "healthy", "sample", "rogue"),
            "restarting": lambda s: (IMAGE if s != "db" else SIDECAR, "running", True, "healthy", "sample", s),
            "unhealthy": lambda s: (IMAGE if s != "db" else SIDECAR, "running", False, "unhealthy", "sample", s),
            "not-running": lambda s: (IMAGE if s != "db" else SIDECAR, "exited", False, None, "sample", s),
        }
        for label, inspect in mutations.items():
            with self.subTest(label=label):
                with self.assertRaises(pt.PrebuiltTargetError):
                    self.execute(executor=FakeExecutor(inspect=inspect))
                self.assert_private_temp_empty()

    def test_failure_at_each_command_cleans_only_owned_override(self):
        predicates = [
            lambda a: "config" in a, lambda a: "pull" in a,
            lambda a: "up" in a, lambda a: "exec" in a,
            lambda a: "ps" in a, lambda a: "inspect" in a,
        ]
        for predicate in predicates:
            executor = FakeExecutor(fail_at=predicate)
            with self.assertRaises(pt.PrebuiltTargetError) as caught:
                self.execute(executor=executor)
            self.assertEqual(caught.exception.__cause__, None)
            self.assertNotIn("synthetic-secret", str(caught.exception))
            self.assert_private_temp_empty()

    def test_deadline_blocks_commands_and_no_result_is_returned(self):
        now = [10.0]

        def clock():
            return now[0]

        class Slow(FakeExecutor):
            def run(inner, *args, **kwargs):
                now[0] = 20.0
                return super().run(*args, **kwargs)

        executor = Slow()
        with self.assertRaises(pt.PrebuiltTargetError):
            self.execute(executor=executor, clock=clock, deadline=15.0)
        self.assertEqual(len(executor.calls), 1)
        self.assert_private_temp_empty()

    def test_registry_callback_checks_budget_after_parsing_and_after_read(self):
        now = [100.0]
        parse = rm._parse_manifest
        executor = FakeExecutor()
        def late_parse(raw):
            value = parse(raw)
            now[0] = 102.0
            return value
        with patch.object(rm, "_parse_manifest", side_effect=late_parse):
            with self.assertRaises(pt.PrebuiltTargetError):
                self.execute(executor=executor, clock=lambda: now[0], deadline=101)
        self.assertEqual((self.resolver_calls, executor.calls), ([], []))
        now[0] = 100.0
        calls = []
        def late_resolve(namespace, tag):
            calls.append((namespace, tag))
            now[0] = 102.0
            return registry_read(namespace, tag)
        with self.assertRaises(pt.PrebuiltTargetError):
            self.execute(executor=executor, clock=lambda: now[0], deadline=101, resolve_pushed_image=late_resolve)
        self.assertEqual(calls, [(NAMESPACE, TAG)])
        self.assertEqual(executor.calls, [])
        self.assert_private_temp_empty()

    def test_file_replacement_between_commands_fails_closed(self):
        replaced = [False]

        def hook(argv):
            if "config" in argv and not replaced[0]:
                replaced[0] = True
                control = self.app / "compose.yaml"
                control.unlink()
                control.write_text("services: {bad: true}\n", encoding="utf-8")

        executor = FakeExecutor(hook=hook)
        with self.assertRaises(pt.PrebuiltTargetError):
            self.execute(executor=executor)
        self.assertEqual(len(executor.calls), 1)
        self.assert_private_temp_empty()

    def test_ps_rejects_duplicate_keys_mixed_oversized_and_unsafe_metadata(self):
        cases = [
            b'{"ID":"' + b"a" * 64 + b'","ID":"' + b"a" * 64 + b'","Service":"web","State":"running","Health":"healthy"}\n',
            b'{"services":[]}\n[]\n',
            b"x" * (pt.MAX_METADATA_BYTES + 1),
            b'{"ID":"' + b"a" * 64 + b'","Service":"web","State":"running","Health":"healthy","Command":"secret"}\n',
        ]
        for raw in cases:
            executor = FakeExecutor(ps=raw)
            with self.assertRaises(pt.PrebuiltTargetError):
                self.execute(executor=executor)
            self.assert_private_temp_empty()


class SubprocessAdapterTests(unittest.TestCase):
    def test_stalled_pipe_reader_cannot_block_cleanup_or_spawn_more_readers(self):
        read_fd, write_fd = os.pipe()
        made, errors, done = [], [], threading.Event()
        class Process:
            def __init__(self, *_args, **kwargs):
                self.stdout = os.fdopen(read_fd, "rb", buffering=kwargs.get("bufsize", -1))
                self.returncode = None
            def poll(self): return None
            def kill(self): pass
            def wait(self, timeout=None):
                if timeout is None: raise AssertionError("unbounded wait")
                raise subprocess.TimeoutExpired("synthetic", timeout)
        def popen(*args, **kwargs):
            process = Process(*args, **kwargs); made.append(process); return process
        def run():
            try: pt._SubprocessExecutor().run(("docker", "inspect"), cwd="/tmp", env={}, timeout=.03, capture=True)
            except pt.PrebuiltTargetError as exc: errors.append(exc.code)
            finally: done.set()
        with patch.object(pt.subprocess, "Popen", side_effect=popen) as factory:
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            try:
                self.assertTrue(done.wait(.5), "closing a buffered pipe must not wait on its reader lock")
                self.assertEqual(errors, ["command_timeout"])
                self.assertTrue(made[0].stdout.closed)
                with self.assertRaisesRegex(pt.PrebuiltTargetError, "command_reader_busy"):
                    pt._SubprocessExecutor().run(("docker", "inspect"), cwd="/tmp", env={}, timeout=.1, capture=True)
                self.assertEqual(factory.call_count, 1)
            finally:
                os.close(write_fd)
                thread.join(1)
                self.assertTrue(pt._PROCESS_READER_GATE.acquire(timeout=1))
                pt._PROCESS_READER_GATE.release()

    def test_default_adapter_uses_no_shell_suppresses_stderr_and_bounds_capture(self):
        class FakeProcess:
            def __init__(self, argv, **kwargs):
                self.argv, self.kwargs = argv, kwargs
                self.stdout = io.BytesIO(b'{"safe":true}') if kwargs["stdout"] == subprocess.PIPE else None
                self.returncode = 0
                self.killed = False
                self.closed = False
            def poll(self): return self.returncode
            def wait(self, timeout=None): return self.returncode
            def kill(self): self.killed = True

        made = []

        def popen(argv, **kwargs):
            process = FakeProcess(argv, **kwargs)
            made.append(process)
            return process

        with patch.object(pt.subprocess, "Popen", popen):
            result = pt._SubprocessExecutor().run(
                ("docker", "compose", "config", "--format", "json"), cwd="/tmp/app",
                env={"PATH": "/usr/bin", "DOCKER_CONFIG": "/private/docker"}, timeout=2, capture=True,
            )
        self.assertEqual(result.stdout, b'{"safe":true}')
        self.assertFalse(made[0].kwargs["shell"])
        self.assertIs(made[0].kwargs["stderr"], subprocess.DEVNULL)
        self.assertIs(made[0].kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(made[0].kwargs["close_fds"])
        self.assertTrue(made[0].stdout.closed)

    def test_default_adapter_timeout_kills_and_waits(self):
        class FakeProcess:
            def __init__(self, *_args, **kwargs):
                self.kwargs = kwargs
                self.stdout = io.BytesIO(b"") if kwargs["stdout"] == subprocess.PIPE else None
                self.calls = 0
                self.returncode = None
                self.did_kill = False
                self.did_wait = False
            def poll(self): return self.returncode
            def wait(self, timeout=None):
                self.did_wait = True
                if self.returncode is None:
                    self.returncode = -9
                return self.returncode
            def kill(self): self.did_kill = True; self.returncode = -9

        processes = []
        def popen(*args, **kwargs):
            item = FakeProcess(*args, **kwargs); processes.append(item); return item
        with patch.object(pt.subprocess, "Popen", popen), patch.object(pt.time, "monotonic", side_effect=[0, 2]):
            with self.assertRaises(pt.PrebuiltTargetError):
                pt._SubprocessExecutor().run(("docker", "compose", "pull"), cwd="/tmp", env={"PATH": "/usr/bin"}, timeout=0.1)
        self.assertTrue(processes[0].did_kill)
        self.assertTrue(processes[0].did_wait)

    def test_adapter_overflow_timeout_and_failed_child_close_owned_pipes_with_bounded_wait(self):
        for mode in ("overflow", "timeout", "failure"):
            made = []
            class Process:
                def __init__(self, *_args, **_kwargs):
                    self.stdout = io.BytesIO(b"x"*(pt.MAX_METADATA_BYTES+1) if mode == "overflow" else b"private-output")
                    self.returncode = 7 if mode == "failure" else None
                    self.kills, self.waits = 0, []
                def poll(self): return self.returncode
                def kill(self): self.kills += 1
                def wait(self, timeout=None):
                    self.waits.append(timeout)
                    if self.returncode is None:
                        raise subprocess.TimeoutExpired("synthetic-child", timeout)
                    return self.returncode
            def popen(*args, **kwargs):
                child = Process(*args, **kwargs); made.append(child); return child
            with patch.object(pt.subprocess, "Popen", side_effect=popen):
                if mode == "failure":
                    result = pt._SubprocessExecutor().run(("docker", "inspect"), cwd="/tmp", env={}, timeout=1, capture=True)
                    self.assertEqual(result.returncode, 7)
                else:
                    with self.assertRaises(pt.PrebuiltTargetError) as caught:
                        pt._SubprocessExecutor().run(("docker", "inspect"), cwd="/tmp", env={},
                            timeout=.02 if mode == "timeout" else 1, capture=True)
                    self.assertEqual(str(caught.exception), "command_timeout" if mode == "timeout" else "command_metadata_too_large")
                    self.assertGreater(made[0].kills, 0)
                self.assertTrue(made[0].stdout.closed)
                self.assertTrue(made[0].waits)
                self.assertTrue(all(type(t) in (int,float) and 0 <= t <= 1 for t in made[0].waits))


if __name__ == "__main__":
    unittest.main(verbosity=2)
