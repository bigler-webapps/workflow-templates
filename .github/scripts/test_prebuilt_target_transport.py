"""Controller-only tests for the fixed SSH transport and target-stream interop."""

from __future__ import annotations

import importlib
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import sys
import socket
import subprocess
import threading
import time
import unittest
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import prebuilt_static_target as st
import prebuilt_target as pt
import prebuilt_target_entry as entry
import prebuilt_target_transport as transport
import release_manifest as rm

image_fixtures = importlib.import_module("test_prebuilt_target")
static_fixtures = importlib.import_module("test_prebuilt_static_target")


class PipeProcess:
    """Process-like lifecycle backed by real local pipes and one endpoint thread."""

    def __init__(self, endpoint):
        child_stdin, parent_stdin = os.pipe()
        parent_stdout, child_stdout = os.pipe()
        self.stdin = os.fdopen(parent_stdin, "wb", buffering=0)
        self.stdout = os.fdopen(parent_stdout, "rb", buffering=0)
        self._child_stdin = child_stdin
        self._child_stdout = child_stdout
        self.returncode = None
        self.terminated = 0
        self.killed = 0
        self.waits = []
        self.thread = threading.Thread(target=self._run, args=(endpoint,), daemon=False)
        self.thread.start()

    def _run(self, endpoint):
        try:
            with os.fdopen(self._child_stdin, "rb", buffering=0) as source:
                with os.fdopen(self._child_stdout, "wb", buffering=0) as sink:
                    endpoint(source, sink)
            self.returncode = 0
        except BaseException:
            self.returncode = 23
        finally:
            self._child_stdin = None
            self._child_stdout = None

    def poll(self):
        return self.returncode if not self.thread.is_alive() else None

    def wait(self, timeout=None):
        self.waits.append(timeout)
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise subprocess.TimeoutExpired("synthetic-owned-child", timeout)
        return self.returncode

    def terminate(self):
        self.terminated += 1
        self.returncode = 23
        self.close_endpoints()

    def kill(self):
        self.killed += 1
        self.terminate()

    def close_endpoints(self):
        # The endpoint thread owns its raw descriptors. Closing them from this
        # thread could close a reused descriptor; parent pipes supply EOF instead.
        if self.thread.is_alive():
            return
        for fd_name in ("_child_stdin", "_child_stdout"):
            fd = getattr(self, fd_name, None)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, fd_name, None)


class TransportFixture(unittest.TestCase):
    def setUp(self):
        for target in ("subprocess.Popen", "socket.socket", "socket.create_connection"):
            patch = mock.patch(target, side_effect=AssertionError("real process/network forbidden"))
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.dict(transport.deploy.__kwdefaults__, {
            "process_factory": lambda *a, **k: self.fail("real process factory forbidden")})
        patch.start()
        self.addCleanup(patch.stop)
        self.image = image_fixtures.TargetFixture(methodName="setUp")
        self.image.setUp()
        self.addCleanup(self.image.doCleanups)
        self.addCleanup(self.image.tearDown)
        self.root = self.image.root
        self.ssh_dir = self.root / "ssh-inputs"
        self.ssh_dir.mkdir(mode=0o700)
        self.identity_file = self.ssh_dir / "identity-input.txt"
        self.known_hosts_file = self.ssh_dir / "known-hosts-input.txt"
        self.identity_file.write_text("nonsecret test identity placeholder\n", encoding="utf-8")
        self.known_hosts_file.write_text("nonsecret test host placeholder\n", encoding="utf-8")
        if os.name != "nt":
            self.ssh_dir.chmod(0o700)
            self.identity_file.chmod(0o600)
            self.known_hosts_file.chmod(0o644)
        self.processes = []
        self.factory_calls = []
        self.executor = image_fixtures.FakeExecutor()
        self.endpoint_invocation = entry.Invocation(
            mode="image", expected_identity=image_fixtures.IDENTITY, target=self.image.target,
            correlation="deploy-" + "c" * 40, image_namespace=image_fixtures.NAMESPACE,
            resolve_pushed_image=self.image.resolver, executor=self.executor, enabled=True,
        )

    def image_config(self, **changes):
        fields = dict(
            host="target.example", port=2222, user="deploy-user",
            identity_file=str(self.identity_file), known_hosts_file=str(self.known_hosts_file),
            expected_identity=image_fixtures.IDENTITY, target=self.image.target,
            correlation="deploy-" + "c" * 40,
            output_kind="image", image_namespace=image_fixtures.NAMESPACE,
            resolve_pushed_image=self.image.resolver, enabled=True,
        )
        fields.update(changes)
        return transport.TransportConfig(**fields)

    def fake_factory(self, endpoint):
        def factory(argv, **kwargs):
            self.factory_calls.append((argv, kwargs))
            proc = PipeProcess(endpoint)
            self.processes.append(proc)
            return proc
        return factory

    def invoke(self, config=None, manifest=None, bundle=b"", endpoint=None):
        endpoint = endpoint or (lambda source, sink: entry.run_target_stream(
            self.endpoint_invocation, source, sink))
        return transport.deploy(
            config or self.image_config(), manifest if manifest is not None else self.image.raw,
            bundle, process_factory=self.fake_factory(endpoint), timeout_seconds=5,
        )

    def tearDown(self):
        for proc in self.processes:
            proc.close_endpoints()
            proc.thread.join(1)
            self.assertFalse(proc.thread.is_alive(), "fake endpoint must finish")
            self.assertTrue(proc.stdin is None or proc.stdin.closed)
            self.assertTrue(proc.stdout is None or proc.stdout.closed)

    def actual_endpoint(self, transform):
        def endpoint(source, sink):
            receipt = io.BytesIO()
            entry.run_target_stream(self.endpoint_invocation, source, receipt)
            sink.write(transform(receipt.getvalue()))
        return endpoint

    def positive(self):
        before = len(self.executor.calls)
        result = self.invoke()
        self.assertEqual(result.status, "succeeded")
        self.assertTrue(any("up" in args and "--no-build" in args
                            for args, *_ in self.executor.calls[before:]))
        return result


class PrebuiltTargetTransportTests(TransportFixture):
    def test_image_uses_actual_target_sdk_over_fixed_binary_pipe(self):
        result = self.invoke()
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.image, image_fixtures.IMAGE)
        self.assertEqual(result.services, ("db", "web", "worker"))
        self.assertTrue(result.migrated)
        self.assertTrue(self.image.resolver_calls)
        commands = [call[0] for call in self.executor.calls]
        self.assertTrue(any("up" in command and "--no-build" in command for command in commands))
        argv, options = self.factory_calls[0]
        self.assertEqual(argv[0:3], ("/usr/bin/ssh", "-F", "/dev/null"))
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertIn("IdentitiesOnly=yes", argv)
        self.assertIn("IdentityAgent=none", argv)
        self.assertIn("ProxyCommand=none", argv)
        self.assertIn("ClearAllForwardings=yes", argv)
        self.assertEqual(argv[-3:], (transport.REMOTE_LAUNCHER, "--correlation",
                                     "deploy-" + "c" * 40))
        self.assertFalse(options["shell"])
        self.assertIs(options["stderr"], transport.subprocess.DEVNULL)
        self.assertEqual(options["env"]["PATH"], "/usr/bin:/bin")
        self.assertEqual(options["env"]["SSH_AUTH_SOCK"], "")
        self.assertTrue(options["close_fds"])
        self.assertFalse(self.processes[0].thread.is_alive())

    def test_static_uses_actual_static_sdk_and_has_no_registry_authority(self):
        fixture = static_fixtures.StaticTargetTests(methodName="setUp")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)
        static_executor = static_fixtures.FakeExecutor(
            preserved=((str(fixture.app / "Caddyfile"), "/etc/caddy/Caddyfile"),))
        invocation = entry.Invocation(
            mode="static", expected_identity=static_fixtures.IDENTITY, target=fixture.spec,
            correlation="deploy-" + "e" * 40, selected_artifact=fixture.selection,
            executor=static_executor, enabled=True,
        )
        config = transport.TransportConfig(
            host="target.example", port=22, user="deploy-user",
            identity_file=str(self.identity_file), known_hosts_file=str(self.known_hosts_file),
            expected_identity=static_fixtures.IDENTITY, target=fixture.spec,
            correlation="deploy-" + "e" * 40,
            output_kind="static", selected_artifact=fixture.selection, enabled=True,
        )
        calls_before = len(self.factory_calls)
        for manifest, bundle, selected in (
                (fixture.manifest, fixture.archive+b"changed", fixture.selection),
                (fixture.manifest, fixture.archive, replace(fixture.selection, artifact_id=99999))):
            with self.assertRaises(transport.TransportError):
                transport.deploy(replace(config, selected_artifact=selected), manifest, bundle,
                    process_factory=self.fake_factory(lambda s, o: self.fail("invalid static request reached SSH")),
                    timeout_seconds=5)
            self.assertEqual(len(self.factory_calls), calls_before)
        result = transport.deploy(
            config, fixture.manifest, fixture.archive,
            process_factory=self.fake_factory(lambda source, sink: entry.run_target_stream(invocation, source, sink)),
            timeout_seconds=5,
        )
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.artifact_sha256, rm.verify_static_bundle(
            fixture.archive, manifest=rm.validate_manifest_structure(
                fixture.manifest, expected=static_fixtures.IDENTITY,
                expected_static_artifact=fixture.selection))[1])
        self.assertEqual(result.artifact_id, fixture.selection.artifact_id)
        roots = list(fixture.assets_parent.iterdir())
        self.assertEqual(len(roots), 1)
        self.assertEqual((roots[0] / "report" / "index.html").read_bytes(), b"immutable site")
        commands = [call[0] for call in static_executor.calls]
        self.assertTrue(any("up" in command and "--no-build" in command for command in commands))
        self.assertFalse(any("build" in command for command in commands))
        self.assertIsNone(config.resolve_pushed_image)
        self.assertIsNone(config.image_namespace)

    def test_invalid_manifest_refuses_before_process_and_valid_positive_executes(self):
        config = self.image_config()
        corrupt = self.image.raw.replace(image_fixtures.DIGEST.encode(),
                                         ("sha256:" + "b" * 64).encode())
        with self.assertRaises(transport.TransportError) as caught:
            self.invoke(config, manifest=corrupt)
        self.assertEqual(caught.exception.code, "release_payload_invalid")
        self.assertEqual(self.factory_calls, [])
        result = self.invoke(config)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(len(self.factory_calls), 1)

    def test_inactive_and_malformed_route_refuse_before_factory(self):
        for config in (self.image_config(enabled=False), self.image_config(host="target;touch"),
                       self.image_config(port=True), self.image_config(user="deploy user"),
                       self.image_config(correlation="deploy-" + "A" * 40)):
            with self.subTest(config=config):
                with self.assertRaises(transport.TransportError):
                    self.invoke(config)
        self.assertEqual(self.factory_calls, [])
        self.positive()

    def test_provider_paths_are_metadata_checked_without_opening_contents(self):
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if path in (self.identity_file, self.known_hosts_file):
                raise AssertionError("provider file content must not be opened")
            return original_open(path, *args, **kwargs)

        self.identity_file.chmod(0o644)
        with mock.patch.object(Path, "open", guarded_open):
            with self.assertRaises(transport.TransportError) as caught:
                self.invoke()
        self.assertEqual(caught.exception.code, "provider_file_invalid")
        self.assertEqual(self.factory_calls, [])
        self.identity_file.chmod(0o600)
        with mock.patch.object(Path, "open", guarded_open):
            self.positive()

    def test_provider_paths_reject_dollar_expansion_before_metadata_or_process(self):
        for field in ("identity_file", "known_hosts_file"):
            for token in ("${HOME}", "${UNSET}", "$"):
                with self.subTest(field=field, token=token):
                    directory = self.root / f"literal-{field}-{token}"
                    directory.mkdir(mode=0o700)
                    candidate = directory / f"{field}-input.txt"
                    candidate.write_text("nonsecret metadata placeholder\n", encoding="utf-8")
                    if os.name != "nt":
                        directory.chmod(0o700)
                        candidate.chmod(0o600 if field == "identity_file" else 0o644)
                    self.assertTrue(candidate.is_file())
                    config = self.image_config(**{field: str(candidate)})

                    lstat_calls = []
                    original_lstat = Path.lstat

                    def observe_lstat(path, *args, **kwargs):
                        lstat_calls.append(path)
                        return original_lstat(path, *args, **kwargs)

                    before_factory = len(self.factory_calls)
                    before_processes = len(self.processes)
                    with mock.patch.object(Path, "lstat", observe_lstat):
                        with self.assertRaises(transport.TransportError) as caught:
                            self.invoke(config=config)
                    self.assertEqual(caught.exception.code, "provider_path_invalid")
                    self.assertNotIn(str(candidate), str(caught.exception))
                    self.assertEqual(lstat_calls, [])
                    self.assertEqual(len(self.factory_calls), before_factory)
                    self.assertEqual(len(self.processes), before_processes)
            self.positive()

    def test_malformed_receipt_and_success_followed_by_nonzero_exit_are_not_success(self):
        malformed = self.actual_endpoint(lambda _: b'{"status":"succeeded"}\n')
        with self.assertRaises(transport.TransportError) as caught:
            self.invoke(endpoint=malformed)
        self.assertTrue(caught.exception.ambiguous)
        self.assertEqual(caught.exception.code, "receipt_invalid")
        self.positive()

    def test_extra_stdout_after_real_receipt_is_rejected(self):
        def noisy(source, sink):
            entry.run_target_stream(self.endpoint_invocation, source, sink)
            sink.write(b"extra\n")
            sink.flush()

        before = len(self.executor.calls)
        with self.assertRaises(transport.TransportError) as caught:
            self.invoke(endpoint=noisy)
        self.assertTrue(caught.exception.ambiguous)
        self.assertTrue(any("up" in args for args, *_ in self.executor.calls[before:]))
        self.positive()

    def test_success_receipt_followed_by_nonzero_process_exit_is_ambiguous(self):
        def success_then_failure(source, sink):
            entry.run_target_stream(self.endpoint_invocation, source, sink)
            raise RuntimeError("synthetic private endpoint failure")

        with self.assertRaises(transport.TransportError) as caught:
            self.invoke(endpoint=success_then_failure)
        self.assertEqual(caught.exception.code, "ssh_failed")
        self.assertTrue(caught.exception.ambiguous)
        self.positive()

    def test_strict_route_config_and_deadline_groups_have_local_positive(self):
        class ForgedConfig(transport.TransportConfig):
            pass
        cases = [self.image_config(enabled=False), self.image_config(host="fe80::1%eth0"),
                 self.image_config(host="999.1.1.1"), self.image_config(host="host\nname"),
                 self.image_config(host="-oProxyCommand=bad"), self.image_config(host="UPPER.example"),
                 self.image_config(port=True), self.image_config(port=65536),
                 self.image_config(user="bad user"), self.image_config(correlation="deploy-" + "A"*40),
                 self.image_config(target=replace(self.image.target, project_name="bad;project")),
                 self.image_config(target=replace(self.image.target, environment="production")),
                 self.image_config(expected_identity=replace(image_fixtures.IDENTITY, ref="refs/heads/main")),
                 self.image_config(output_kind="static"), self.image_config(selected_artifact=object()),
                 object()]
        for config in cases:
            with self.subTest(case=type(config).__name__):
                before = len(self.factory_calls)
                with self.assertRaises(transport.TransportError):
                    self.invoke(config=config)
                self.assertEqual(len(self.factory_calls), before)
                self.positive()
        for budget in (True, 0, -1, float("nan"), float("inf"), 1801, 10**1000):
            with self.subTest(budget_type=type(budget).__name__):
                before = len(self.factory_calls)
                with self.assertRaises(transport.TransportError) as caught:
                    transport.deploy(self.image_config(), self.image.raw, timeout_seconds=budget,
                                     process_factory=self.fake_factory(lambda s, o: None))
                self.assertEqual(caught.exception.code, "deadline_invalid")
                self.assertEqual(len(self.factory_calls), before)
                self.positive()

    def test_provider_modes_links_and_changed_identity_refuse_before_process(self):
        for path, unsafe_mode, safe_mode in ((self.identity_file, 0o644, 0o600),
                                             (self.known_hosts_file, 0o666, 0o644),
                                             (self.ssh_dir, 0o755, 0o700)):
            path.chmod(unsafe_mode)
            before = len(self.factory_calls)
            with self.assertRaises(transport.TransportError):
                self.invoke()
            self.assertEqual(len(self.factory_calls), before)
            path.chmod(safe_mode)
            self.positive()
        alias = self.ssh_dir / "identity-alias.txt"
        alias.symlink_to(self.identity_file)
        try:
            before = len(self.factory_calls)
            with self.assertRaises(transport.TransportError):
                self.invoke(config=self.image_config(identity_file=str(alias)))
            self.assertEqual(len(self.factory_calls), before)
        finally:
            alias.unlink()
        self.positive()
        for provider in (self.identity_file, self.known_hosts_file):
            linked = self.ssh_dir / "linked-input.txt"
            os.link(provider, linked)
            try:
                with self.assertRaises(transport.TransportError):
                    self.invoke()
            finally:
                linked.unlink()
            self.positive()
        original = transport._same_snapshots
        def changed(config, before):
            self.identity_file.chmod(0o640)
            original(config, before)
        count = len(self.factory_calls)
        with mock.patch.object(transport, "_same_snapshots", changed):
            with self.assertRaises(transport.TransportError):
                self.invoke()
        self.assertEqual(len(self.factory_calls), count)
        self.identity_file.chmod(0o600)
        self.positive()
        for path in (str(self.identity_file) + "%h", str(self.identity_file) + " bad",
                     str(self.identity_file.parent / ".." / self.identity_file.name)):
            before = len(self.factory_calls)
            with self.assertRaises(transport.TransportError):
                self.invoke(config=self.image_config(identity_file=path))
            self.assertEqual(len(self.factory_calls), before)
            self.positive()

    def test_manifest_identity_and_size_refuse_before_ssh_with_local_positive(self):
        # Real source schema is used, rather than a receipt-like success DTO.
        variants = [b"", b"x" * (rm.MAX_MANIFEST_BYTES + 1), b"{", self.image.raw + b"\nextra"]
        for field, value in (("source_repository", "foreign/repo"), ("source_sha", "f"*40),
                             ("run_id", 99999), ("run_attempt", 99), ("source_ref", "refs/heads/main")):
            item = json.loads(self.image.raw)
            item[field] = value
            variants.append(json.dumps(item).encode())
        for raw in variants:
            with self.subTest(length=len(raw)):
                before = len(self.factory_calls)
                with self.assertRaises(transport.TransportError):
                    self.invoke(manifest=raw)
                self.assertEqual(len(self.factory_calls), before)
                self.positive()

    def test_receipt_identity_and_semantic_fields_are_bound_with_local_positive(self):
        cases = {"correlation":"deploy-"+"f"*40, "source_repository":"foreign/repo",
                 "source_sha":"f"*40, "run_id":True, "attempt":999,
                 "environment":"production", "project":"foreign", "output_kind":"static",
                 "version":True, "health":"unhealthy", "migrated":False,
                 "image":image_fixtures.NAMESPACE+"@sha256:"+"e"*64,
                 "services":["foreign"], "status":"failed", "effect":"none",
                 "unknown":"HOSTILE-SECRET-MARKER"}
        for key, value in cases.items():
            def mutate(raw, key=key, value=value):
                item = json.loads(raw)
                item[key] = value
                return entry._encode_receipt(item)
            with self.subTest(field=key):
                with self.assertRaises(transport.TransportError) as caught:
                    self.invoke(endpoint=self.actual_endpoint(mutate))
                self.assertEqual(caught.exception.code, "receipt_invalid")
                self.assertTrue(caught.exception.ambiguous)
                self.assertNotIn("HOSTILE-SECRET-MARKER", str(caught.exception))
                self.positive()

    def test_receipt_grammar_caps_and_redaction_reach_the_actual_producer(self):
        transforms = [lambda raw: raw+b"extra", lambda raw: raw+b"\n", lambda raw: raw[:-1],
                      lambda raw: b" "+raw, lambda raw: raw.replace(b'"version":1',b'"version":NaN'),
                      lambda raw: raw.replace(b'"version":1',b'"version":1,"version":1'),
                      lambda raw: b"["*1000+b"0"+b"]"*1000+b"\n",
                      lambda raw: b"HOSTILE-SECRET-MARKER"*205]
        for transform in transforms:
            with self.subTest(transform=transforms.index(transform)):
                before = len(self.executor.calls)
                with self.assertRaises(transport.TransportError) as caught:
                    self.invoke(endpoint=self.actual_endpoint(transform))
                self.assertGreater(len(self.executor.calls), before)
                self.assertTrue(caught.exception.ambiguous)
                self.assertNotIn("HOSTILE-SECRET-MARKER", repr(caught.exception))
                self.positive()

    def test_partial_pipe_writes_and_reads_still_execute_exact_frame(self):
        write, read = os.write, os.read
        with mock.patch.object(os, "write", side_effect=lambda fd, data: write(fd, data[:7])), \
             mock.patch.object(os, "read", side_effect=lambda fd, n: read(fd, min(n, 3))):
            self.positive()

    def test_no_provider_content_open_or_extra_registry_read_after_handoff(self):
        original_open = Path.open
        def guarded(path, *a, **k):
            if path in (self.identity_file, self.known_hosts_file):
                self.fail("provider contents must not be opened")
            return original_open(path, *a, **k)
        before = len(self.image.resolver_calls)
        with mock.patch.object(Path, "open", guarded):
            self.positive()
        # Exactly one controller validation and one actual target SDK validation.
        self.assertEqual(len(self.image.resolver_calls)-before, 2)

    def test_fixed_argv_and_full_frame_are_observed_not_reconstructed_by_a_stub(self):
        frames = []
        def observe(source, sink):
            frame = source.read()
            frames.append(frame)
            entry.run_target_stream(self.endpoint_invocation, io.BytesIO(frame), sink)
        self.invoke(endpoint=observe)
        expected = (entry._HEADER.pack(b"PTARGET1", 1, 1, len(self.image.raw), 0)
                    + self.image.raw)
        self.assertEqual(frames, [expected])
        argv, options = self.factory_calls[-1]
        self.assertEqual(argv, (
            "/usr/bin/ssh", "-F", "/dev/null", "-T", "-p", "2222", "-i", str(self.identity_file),
            "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
            "-o", "CertificateFile=none", "-o", "PKCS11Provider=none",
            "-o", "SecurityKeyProvider=none", "-o", "AddKeysToAgent=no",
            "-o", "ConnectionAttempts=1", "-o", "EscapeChar=none",
            "-o", "BatchMode=yes", "-o", "NumberOfPasswordPrompts=0",
            "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
            "-o", "GSSAPIAuthentication=no", "-o", "HostbasedAuthentication=no",
            "-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no",
            "-o", "VerifyHostKeyDNS=no", "-o", "UserKnownHostsFile="+str(self.known_hosts_file),
            "-o", "GlobalKnownHostsFile=/dev/null", "-o", "CanonicalizeHostname=no",
            "-o", "ClearAllForwardings=yes", "-o", "ForwardAgent=no", "-o", "ForwardX11=no",
            "-o", "PermitLocalCommand=no", "-o", "ProxyCommand=none", "-o", "ProxyJump=none",
            "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "ControlPersist=no",
            "-o", "RequestTTY=no", "-l", "deploy-user", "target.example",
            "/usr/local/libexec/webapp-prebuilt-target-v1", "--correlation", "deploy-"+"c"*40))
        self.assertEqual(set(options["env"]), {"PATH", "LANG", "LC_ALL", "HOME", "SSH_AUTH_SOCK"})
        self.assertEqual(options["stdin"], subprocess.PIPE)
        self.assertEqual(options["stdout"], subprocess.PIPE)
        self.assertTrue(all(p.waits for p in self.processes))

    def test_success_line_then_late_exit_is_ambiguous_and_child_reaped(self):
        def late(source, sink):
            entry.run_target_stream(self.endpoint_invocation, source, sink)
            time.sleep(0.12)
        with self.assertRaises(transport.TransportError) as caught:
            transport.deploy(self.image_config(), self.image.raw, timeout_seconds=0.06,
                             process_factory=self.fake_factory(late))
        self.assertTrue(caught.exception.ambiguous)
        self.assertEqual(caught.exception.code, "deadline_exceeded")
        self.assertEqual(self.processes[-1].terminated, 1)
        self.assertFalse(self.processes[-1].thread.is_alive())
        self.positive()

    def test_backwards_clock_after_handoff_never_returns_receipt(self):
        baseline = time.monotonic()
        calls = 0
        def clock():
            nonlocal calls
            calls += 1
            return baseline - 1 if self.factory_calls and calls > 6 else baseline
        with self.assertRaises(transport.TransportError) as caught:
            transport.deploy(self.image_config(), self.image.raw, clock=clock,
                             process_factory=self.fake_factory(lambda s, o: entry.run_target_stream(
                                 self.endpoint_invocation, s, o)), timeout_seconds=5)
        self.assertEqual(caught.exception.code, "clock_invalid")
        self.assertTrue(caught.exception.ambiguous)
        self.positive()

    def test_pre_handoff_clock_failure_does_not_start_child(self):
        for value in (float("nan"), True, 10**1000):
            before = len(self.factory_calls)
            with self.assertRaises(transport.TransportError) as caught:
                transport.deploy(self.image_config(), self.image.raw, clock=lambda: value,
                                 process_factory=self.fake_factory(lambda s, o: None))
            self.assertEqual(caught.exception.code, "clock_invalid")
            self.assertEqual(len(self.factory_calls), before)
            self.positive()

    def test_successful_exit_with_wait_failure_is_never_success(self):
        base_factory = self.fake_factory(lambda s, o: entry.run_target_stream(self.endpoint_invocation, s, o))
        def factory(*a, **k):
            proc = base_factory(*a, **k)
            proc.wait = mock.Mock(side_effect=RuntimeError("HOSTILE-SECRET-MARKER"))
            return proc
        with self.assertRaises(transport.TransportError) as caught:
            transport.deploy(self.image_config(), self.image.raw, process_factory=factory, timeout_seconds=5)
        self.assertTrue(caught.exception.ambiguous)
        self.assertEqual(caught.exception.code, "ssh_cleanup_failed")
        self.assertNotIn("HOSTILE-SECRET-MARKER", str(caught.exception))
        self.positive()

    def test_broken_write_oversize_and_output_before_full_input_do_not_fake_success(self):
        real_write = os.write
        for result in (0, -1, "not-an-int", True):
            with mock.patch.object(os, "write", return_value=result):
                with self.assertRaises(transport.TransportError) as caught:
                    self.invoke()
            self.assertTrue(caught.exception.ambiguous)
            self.positive()
        def early(source, sink):
            sink.write(b"HOSTILE-SECRET-MARKER"*250)
            source.read()
        with self.assertRaises(transport.TransportError) as caught:
            self.invoke(endpoint=early)
        self.assertTrue(caught.exception.ambiguous)
        self.assertNotIn("HOSTILE-SECRET-MARKER", str(caught.exception))
        self.positive()

    def test_actual_target_pre_effect_refusal_is_data_and_unknown_effect_is_ambiguous(self):
        def corrupt_header(source, sink):
            raw = source.read()
            entry.run_target_stream(self.endpoint_invocation, io.BytesIO(b"X"+raw[1:]), sink)
        before = len(self.executor.calls)
        result = self.invoke(endpoint=corrupt_header)
        self.assertEqual((result.status, result.effect, result.error_code), ("refused", "none", "target_refused"))
        self.assertEqual(len(self.executor.calls), before)
        self.positive()
        self.executor.fail_at = lambda args: "up" in args
        with self.assertRaises(transport.TransportError) as caught:
            self.invoke()
        self.assertTrue(caught.exception.ambiguous)
        self.assertTrue(any("up" in args for args, *_ in self.executor.calls))
        self.executor.fail_at = None
        self.positive()

    def test_untrusted_directory_chain_refuses_before_ssh_with_local_positive(self):
        container = self.root / "provider-parent"
        container.mkdir(mode=0o700)
        private = container / "private"
        private.mkdir(mode=0o700)
        identity = private / "identity-input.txt"
        known = private / "known-hosts-input.txt"
        identity.write_text("nonsecret metadata placeholder", encoding="utf-8")
        known.write_text("nonsecret metadata placeholder", encoding="utf-8")
        identity.chmod(0o600)
        known.chmod(0o644)
        config = self.image_config(identity_file=str(identity), known_hosts_file=str(known))
        for mode in (0o777, 0o770, 0o702):
            container.chmod(mode)
            before = len(self.factory_calls)
            with self.assertRaises(transport.TransportError) as caught:
                self.invoke(config=config)
            self.assertEqual(caught.exception.code, "provider_path_invalid")
            self.assertEqual(len(self.factory_calls), before)
            container.chmod(0o700)
            self.assertEqual(self.invoke(config=config).status, "succeeded")
        alias = self.root / "provider-alias"
        alias.symlink_to(container, target_is_directory=True)
        with self.assertRaises(transport.TransportError):
            self.invoke(config=self.image_config(identity_file=str(alias/"private"/identity.name)))
        self.assertEqual(self.invoke(config=config).status, "succeeded")
        original = transport._same_snapshots
        def change_ancestor(c, before):
            container.chmod(0o777)
            original(c, before)
        before = len(self.factory_calls)
        with mock.patch.object(transport, "_same_snapshots", change_ancestor):
            with self.assertRaises(transport.TransportError):
                self.invoke(config=config)
        self.assertEqual(len(self.factory_calls), before)
        container.chmod(0o700)
        self.assertEqual(self.invoke(config=config).status, "succeeded")

    def test_contradictory_unknown_effect_code_cannot_be_a_no_effect_refusal(self):
        def contradict(raw):
            value = json.loads(raw)
            for key in ("health", "image", "services", "migrated"):
                value.pop(key)
            value.update(status="failed", effect="none", error_code="deployment_failed",
                         recovery_required=False)
            return entry._encode_receipt(value)
        before = len(self.executor.calls)
        with self.assertRaises(transport.TransportError) as caught:
            self.invoke(endpoint=self.actual_endpoint(contradict))
        self.assertEqual(caught.exception.code, "receipt_invalid")
        self.assertTrue(caught.exception.ambiguous)
        self.assertTrue(any("up" in args for args, *_ in self.executor.calls[before:]))
        self.positive()


if __name__ == "__main__":
    unittest.main()
