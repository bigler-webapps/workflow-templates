"""Behavioral tests for the fixed stdin entry adapter and real target SDKs."""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import io
import json
import struct
import sys
import tarfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock
import pytest


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import prebuilt_static_target as st
import prebuilt_target as pt
import prebuilt_target_entry as entry
import release_manifest as rm

image_fixtures = importlib.import_module("test_prebuilt_target")
static_fixtures = importlib.import_module("test_prebuilt_static_target")


def image_frame(manifest: bytes, *, kind: int = 1, bundle: bytes = b"") -> bytes:
    # Independent encoder for the documented grammar, not an adapter helper.
    return struct.pack("!8sBBII", b"PTARGET1", 1, kind, len(manifest), len(bundle)) + manifest + bundle


def static_frame(manifest: bytes, bundle: bytes) -> bytes:
    return image_frame(manifest, kind=2, bundle=bundle)


class Capture:
    def __init__(self):
        self.data = bytearray()
        self.calls = 0

    def write(self, data: bytes) -> int:
        self.calls += 1
        self.data.extend(data)
        return len(data)

    def flush(self) -> None:
        return None

    def receipt(self) -> dict:
        return json.loads(bytes(self.data))


class GuardedReader(io.BytesIO):
    def __init__(self, data: bytes, *, partial: int | None = None):
        super().__init__(data)
        self.requests: list[int] = []
        self.partial = partial

    def read(self, size: int = -1) -> bytes:
        self.requests.append(size)
        if size < 0:
            raise AssertionError("unbounded read")
        if self.partial is not None:
            size = min(size, self.partial)
        return super().read(size)


class OverreadReader:
    def __init__(self, raw: bytes):
        self.raw = raw
        self.offset = 0
        self.requests = []

    def read(self, size: int = -1) -> bytes:
        self.requests.append(size)
        if size < 0:
            raise AssertionError("unbounded read")
        if self.offset == 0:
            result = self.raw[:size + 1]
            self.offset += len(result)
            return result
        return b""


class NonBytesReader:
    def __init__(self):
        self.requests = []

    def read(self, size: int = -1):
        self.requests.append(size)
        if size < 0:
            raise AssertionError("unbounded read")
        return "not bytes"


class RaisingReader:
    def __init__(self):
        self.requests = []

    def read(self, size: int = -1):
        self.requests.append(size)
        if size < 0:
            raise AssertionError("unbounded read")
        raise RuntimeError("synthetic_secret_reader_error")


class StepClock:
    def __init__(self, values):
        self.values = iter(values)
        self.last = None

    def __call__(self):
        self.last = next(self.values)
        return self.last


class MutableClock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value


def image_invocation(fixture, **changes) -> entry.Invocation:
    values = dict(
        mode="image", expected_identity=image_fixtures.IDENTITY, target=fixture.target,
        correlation="deploy-" + "c" * 40,
        image_namespace=image_fixtures.NAMESPACE,
        resolve_pushed_image=fixture.resolver,
        executor=None, enabled=True,
    )
    values.update(changes)
    return entry.Invocation(**values)


def static_invocation(fixture, **changes) -> entry.Invocation:
    values = dict(
        mode="static", expected_identity=static_fixtures.IDENTITY, target=fixture.spec,
        correlation="deploy-" + "d" * 40,
        selected_artifact=fixture.selection,
        executor=None, enabled=True,
    )
    values.update(changes)
    return entry.Invocation(**values)


class ImageTargetEntryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = image_fixtures.TargetFixture(methodName="setUp")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.addCleanup(self.fixture.doCleanups)

    def run_frame(self, raw: bytes, invocation=None, *, reader=None, sink=None):
        reader = reader if reader is not None else GuardedReader(raw)
        sink = sink if sink is not None else Capture()
        entry.run_target_stream(invocation or image_invocation(self.fixture), reader, sink)
        return reader, sink

    def test_golden_frame_runs_real_image_sdk_and_receipt_binds_observed_result(self):
        frame = image_frame(self.fixture.raw)
        self.assertEqual(frame[:18], b"PTARGET1\x01\x01" + struct.pack("!II", len(self.fixture.raw), 0))
        overrides = []
        def observe_override(argv):
            if argv[-3:] == ("config", "--format", "json"):
                files = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--file"]
                overrides.append(Path(files[-1]).read_text(encoding="utf-8"))
        executor = image_fixtures.FakeExecutor(hook=observe_override)
        invocation = image_invocation(self.fixture, executor=executor)
        reader, sink = self.run_frame(frame, invocation)
        receipt = sink.receipt()
        self.assertEqual(reader.requests[0], 18)
        self.assertTrue(all(0 <= size <= entry.MAX_STREAM_READ for size in reader.requests))
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["effect"], "verified")
        self.assertEqual(receipt["correlation"], invocation.correlation)
        self.assertEqual(receipt["output_kind"], "image")
        self.assertEqual((receipt["source_repository"], receipt["source_sha"], receipt["run_id"], receipt["attempt"]),
                         (image_fixtures.IDENTITY.repository, image_fixtures.IDENTITY.sha,
                          image_fixtures.IDENTITY.run_id, image_fixtures.IDENTITY.attempt))
        self.assertEqual((receipt["environment"], receipt["project"]), ("staging", "sample"))
        self.assertEqual(receipt["image"], image_fixtures.IMAGE)
        self.assertEqual(receipt["services"], ["db", "web", "worker"])
        self.assertTrue(receipt["migrated"])
        commands = [call[0] for call in executor.calls]
        up = next(command for command in commands if "up" in command)
        self.assertEqual(up[up.index("up"):],
                         ("up", "--detach", "--no-build", "--pull", "never", "--force-recreate", "--remove-orphans"))
        self.assertEqual(len(overrides), 1)
        self.assertEqual(overrides[0].count('image: ' + json.dumps(image_fixtures.IMAGE)), 2)
        self.assertEqual(overrides[0].count('build: !reset null'), 2)
        self.assertEqual(len(self.fixture.resolver_calls), 1)

    def test_partial_reads_and_varied_identity_and_registry_digest_succeed(self):
        identity = replace(image_fixtures.IDENTITY, sha="e" * 40, run_id=654321, attempt=3)
        digest = "sha256:" + "f" * 64
        image = image_fixtures.NAMESPACE + "@" + digest
        raw = rm.produce_image_manifest(
            identity, expected_namespace=image_fixtures.NAMESPACE,
            pushed_tag=rm.release_image_tag(identity),
            resolve_pushed_image=lambda namespace, tag: {
                "repository": namespace, "tag": tag, "digest": digest,
            },
        )
        executor = image_fixtures.FakeExecutor(
            config={"services": {"web": {"image": image}, "worker": {"image": image},
                                 "db": {"image": image_fixtures.SIDECAR}}},
            inspect=lambda service: (image if service in ("web", "worker") else image_fixtures.SIDECAR,
                                     "running", False, "healthy" if service != "worker" else None,
                                     "sample", service),
        )
        calls = []

        def resolver(namespace, tag):
            calls.append((namespace, tag))
            return {"repository": namespace, "tag": tag, "digest": digest}

        invocation = image_invocation(self.fixture, expected_identity=identity,
                                      resolve_pushed_image=resolver, executor=executor)
        reader, sink = self.run_frame(image_frame(raw), invocation, reader=GuardedReader(image_frame(raw), partial=3))
        receipt = sink.receipt()
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual((receipt["source_sha"], receipt["run_id"], receipt["attempt"]), ("e" * 40, 654321, 3))
        self.assertEqual(receipt["image"], image)
        self.assertEqual(calls, [(image_fixtures.NAMESPACE, rm.release_image_tag(identity))])
        self.assertGreater(len(reader.requests), 4)

    def test_wrong_manifest_identity_and_registry_digest_do_not_succeed(self):
        wrong_identity = replace(image_fixtures.IDENTITY, run_id=654321)
        wrong = rm.produce_image_manifest(
            wrong_identity, expected_namespace=image_fixtures.NAMESPACE,
            pushed_tag=rm.release_image_tag(wrong_identity),
            resolve_pushed_image=image_fixtures.registry_read,
        )
        executor = image_fixtures.FakeExecutor()
        sink = Capture()
        self.run_frame(image_frame(wrong), image_invocation(self.fixture, executor=executor), sink=sink)
        self.assertEqual(sink.receipt()["status"], "failed")
        self.assertEqual(executor.calls, [])
        self.assertEqual(self.fixture.resolver_calls, [])

        bad_resolver = lambda namespace, tag: {"repository": namespace, "tag": tag, "digest": "secret-token"}
        sink2 = Capture()
        self.run_frame(image_frame(self.fixture.raw),
                       image_invocation(self.fixture, resolve_pushed_image=bad_resolver,
                                        executor=executor), sink=sink2)
        self.assertEqual(sink2.receipt()["status"], "failed")
        self.assertNotIn("secret", bytes(sink2.data).decode("ascii"))
        self.assertEqual(executor.calls, [])
        positive_image(self.fixture)

    def test_frame_rejections_never_reach_registry_or_executor(self):
        valid = image_frame(self.fixture.raw)
        cases = [
            valid[:1] + b"X" + valid[2:],
            valid[:8] + b"\x02" + valid[9:],
            struct.pack("!8sBBII", b"PTARGET1", 1, 1, entry.MAX_MANIFEST_BYTES + 1, 0),
            struct.pack("!8sBBII", b"PTARGET1", 1, 1, len(self.fixture.raw), 1) + self.fixture.raw + b"x",
            valid[:-1],
            valid + b"trailing",
            struct.pack("!8sBBII", b"PTARGET1", 1, 2, len(self.fixture.raw), 0) + self.fixture.raw,
        ]
        for raw in cases:
            with self.subTest(length=len(raw)):
                executor = image_fixtures.FakeExecutor()
                sink = Capture()
                self.run_frame(raw, image_invocation(self.fixture, executor=executor), sink=sink)
                self.assertEqual(sink.receipt()["status"], "failed")
                self.assertEqual(executor.calls, [])
                self.assertEqual(self.fixture.resolver_calls, [])
        positive_image(self.fixture)

    def test_short_zero_overread_nonbytes_and_reader_error_are_bounded(self):
        valid = image_frame(self.fixture.raw)
        readers = [GuardedReader(valid[:0]), OverreadReader(valid), NonBytesReader(), RaisingReader()]
        for reader in readers:
            sink = Capture()
            executor = image_fixtures.FakeExecutor()
            self.run_frame(valid, image_invocation(self.fixture, executor=executor), reader=reader, sink=sink)
            self.assertEqual(sink.receipt()["status"], "failed")
            self.assertEqual(executor.calls, [])
            self.assertLessEqual(max(reader.requests), entry.MAX_STREAM_READ)
        self.assertNotIn(b"synthetic_secret_reader_error", bytes(sink.data))
        positive_image(self.fixture)

    def test_inactive_mixed_subclass_and_invalid_capability_refuse_before_read(self):
        class Untouched:
            calls = 0
            def read(self, size=-1):
                self.calls += 1
                return b""

        inactive = image_invocation(self.fixture, enabled=False)
        stream = Untouched()
        with self.assertRaises(entry.EntryError) as caught:
            entry.run_target_stream(inactive, stream, Capture())
        self.assertEqual(caught.exception.code, "inactive")
        self.assertEqual(stream.calls, 0)
        for invocation in (
            image_invocation(self.fixture, selected_artifact=object()),
            image_invocation(self.fixture, resolve_pushed_image=None),
        ):
            stream = Untouched()
            with self.assertRaises(entry.EntryError):
                entry.run_target_stream(invocation, stream, Capture())
            self.assertEqual(stream.calls, 0)

        class Derived(entry.Invocation):
            pass
        forged = object.__new__(Derived)
        stream = Untouched()
        with self.assertRaises(entry.EntryError):
            entry.run_target_stream(forged, stream, Capture())
        self.assertEqual(stream.calls, 0)
        positive_image(self.fixture)

    def test_clock_expiry_and_backward_clock_refuse_without_effects(self):
        valid = image_frame(self.fixture.raw)
        for changed, code in ((10.2, "deadline_exceeded"), (9.0, "clock_invalid")):
            clock = MutableClock(10)
            class ClockReader(GuardedReader):
                def read(self, amount):
                    value = super().read(amount)
                    clock.value = changed
                    return value
            reader = ClockReader(valid)
            sink = Capture()
            executor = image_fixtures.FakeExecutor()
            invocation = image_invocation(self.fixture, executor=executor, clock=clock, timeout_seconds=0.15)
            with self.assertRaises(entry.EntryError) as caught:
                self.run_frame(valid, invocation, reader=reader, sink=sink)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(reader.requests, [18])
            self.assertEqual(sink.data, b"")
            self.assertEqual(sink.calls, 0)
            self.assertEqual(executor.calls, [])

        nan_clock = MutableClock(float("nan"))
        with self.assertRaises(entry.EntryError):
            entry.run_target_stream(image_invocation(self.fixture, clock=nan_clock),
                                    GuardedReader(valid), Capture())

        clock = MutableClock()
        def expire_during_pull(argv):
            if "pull" in argv:
                clock.value = 2.0
        executor = image_fixtures.FakeExecutor(hook=expire_during_pull)
        sink = Capture()
        with self.assertRaises(entry.EntryError) as late:
            self.run_frame(valid, image_invocation(self.fixture, executor=executor,
                                                  clock=clock, timeout_seconds=1.0), sink=sink)
        self.assertEqual(late.exception.code, "receipt_write_failed_ambiguous")
        self.assertEqual(sink.data, b"")
        self.assertTrue(any("pull" in call[0] for call in executor.calls))
        self.assertFalse(any("up" in call[0] for call in executor.calls))
        positive_image(self.fixture)

    def test_sdk_failures_and_hostile_output_never_become_success_or_leak(self):
        for marker in ("pull", "up", "exec", "inspect"):
            executor = image_fixtures.FakeExecutor(fail_at=lambda argv, phase=marker: phase in argv)
            sink = Capture()
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.run_frame(image_frame(self.fixture.raw), image_invocation(self.fixture, executor=executor), sink=sink)
            self.assertEqual(sink.receipt()["status"], "failed")
            self.assertNotIn(b"synthetic-secret-child-output", bytes(sink.data))
            self.assertNotIn("synthetic-secret-child-output", stdout.getvalue() + stderr.getvalue())
            self.assertTrue(any(marker in call[0] for call in executor.calls))
            self.assertFalse(sink.receipt().get("migrated", False))
        positive_image(self.fixture)

    def test_writer_capability_checked_before_effect_and_lost_ack_is_not_retried(self):
        executor = image_fixtures.FakeExecutor()
        invocation = image_invocation(self.fixture, executor=executor)
        with self.assertRaises(entry.EntryError):
            entry.run_target_stream(invocation, GuardedReader(image_frame(self.fixture.raw)), object())
        self.assertEqual(executor.calls, [])

        class FailingWriter:
            calls = 0
            def write(self, data):
                self.calls += 1
                raise OSError("secret-output-failure")
        writer = FailingWriter()
        with self.assertRaises(entry.EntryError) as caught:
            entry.run_target_stream(invocation, GuardedReader(image_frame(self.fixture.raw)), writer)
        self.assertEqual(caught.exception.code, "receipt_write_failed_ambiguous")
        self.assertEqual(writer.calls, 1)
        self.assertTrue(any("up" in call[0] for call in executor.calls))


class StaticTargetEntryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = static_fixtures.StaticTargetTests(methodName="setUp")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.addCleanup(self.fixture.doCleanups)

    def run_frame(self, raw: bytes, invocation=None, *, reader=None, sink=None):
        reader = reader if reader is not None else GuardedReader(raw)
        sink = sink if sink is not None else Capture()
        entry.run_target_stream(invocation or static_invocation(self.fixture), reader, sink)
        return reader, sink

    def test_static_frame_executes_real_static_sdk_and_reports_bound_bytes(self):
        fake = static_fixtures.FakeExecutor(preserved=((str(self.fixture.app / "Caddyfile"), "/etc/caddy/Caddyfile"),))
        invocation = static_invocation(self.fixture, executor=fake)
        frame = static_frame(self.fixture.manifest, self.fixture.archive)
        reader, sink = self.run_frame(frame, invocation)
        receipt = sink.receipt()
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["effect"], "verified")
        self.assertEqual(receipt["output_kind"], "static")
        self.assertEqual(receipt["correlation"], invocation.correlation)
        self.assertEqual(receipt["artifact_id"], self.fixture.selection.artifact_id)
        self.assertEqual(receipt["artifact_sha256"], self.fixture.manifest and json.loads(self.fixture.manifest)["output"]["sha256"])
        self.assertEqual(receipt["image"], self.fixture.spec.static_image)
        self.assertEqual(receipt["health"], "healthy")
        self.assertNotIn("asset_directory", receipt)
        self.assertTrue(all(size <= entry.MAX_STREAM_READ for size in reader.requests))
        self.assertEqual((Path(next(self.fixture.assets_parent.iterdir())) / "report" / "index.html").read_bytes(),
                         b"immutable site")
        commands = [call[0] for call in fake.calls]
        up = next(command for command in commands if "up" in command)
        self.assertIn("--no-build", up)
        self.assertIn("--pull", up)

    def test_static_varied_identity_artifact_and_large_multichunk_bundle(self):
        identity = replace(static_fixtures.IDENTITY, sha="e" * 40, run_id=654321, attempt=3)
        selection = rm.StaticArtifactSelection(rm.static_artifact_name(identity), 987)
        bundle = static_fixtures.make_archive(b"x" * (70 * 1024))
        manifest = rm.produce_static_manifest(identity, artifact_id=selection.artifact_id,
                                               bundle=bundle, bundle_length=len(bundle))
        fake = static_fixtures.FakeExecutor(preserved=((str(self.fixture.app / "Caddyfile"), "/etc/caddy/Caddyfile"),))
        invocation = static_invocation(self.fixture, expected_identity=identity,
                                       selected_artifact=selection, executor=fake)
        frame = static_frame(manifest, bundle)
        reader, sink = self.run_frame(frame, invocation, reader=GuardedReader(frame, partial=17))
        receipt = sink.receipt()
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual((receipt["source_sha"], receipt["run_id"], receipt["attempt"]), ("e" * 40, 654321, 3))
        self.assertEqual(receipt["artifact_id"], 987)
        self.assertGreaterEqual(len(reader.requests), 10)
        self.assertLessEqual(max(reader.requests), entry.MAX_STREAM_READ)

    def test_static_bad_bundle_wrong_selection_and_malformed_archive_have_no_effect(self):
        fake = static_fixtures.FakeExecutor()
        altered = bytearray(self.fixture.archive)
        altered[-513] ^= 1
        cases = [
            static_frame(self.fixture.manifest, bytes(altered)),
            static_frame(self.fixture.manifest, self.fixture.archive + b"x"),
            struct.pack("!8sBBII", b"PTARGET1", 1, 2, len(self.fixture.manifest), 0) + self.fixture.manifest,
        ]
        for frame in cases:
            sink = Capture()
            self.run_frame(frame, static_invocation(self.fixture, executor=fake), sink=sink)
            self.assertEqual(sink.receipt()["status"], "failed")
            self.assertEqual(fake.calls, [])
            self.assertEqual(list(self.fixture.assets_parent.iterdir()), [])

        wrong_selection = rm.StaticArtifactSelection(self.fixture.selection.name, self.fixture.selection.artifact_id + 1)
        sink = Capture()
        self.run_frame(static_frame(self.fixture.manifest, self.fixture.archive),
                       static_invocation(self.fixture, selected_artifact=wrong_selection, executor=fake), sink=sink)
        self.assertEqual(sink.receipt()["status"], "failed")
        self.assertEqual(fake.calls, [])

        wrong_identity = replace(static_fixtures.IDENTITY, run_id=654321)
        wrong_selection = rm.StaticArtifactSelection(rm.static_artifact_name(wrong_identity), 991)
        wrong_source_manifest = rm.produce_static_manifest(
            wrong_identity, artifact_id=wrong_selection.artifact_id,
            bundle=self.fixture.archive, bundle_length=len(self.fixture.archive))
        sink = Capture()
        self.run_frame(static_frame(wrong_source_manifest, self.fixture.archive),
                       static_invocation(self.fixture, executor=fake), sink=sink)
        self.assertEqual(sink.receipt()["status"], "failed")
        self.assertEqual(fake.calls, [])

        linked = io.BytesIO()
        with tarfile.open(fileobj=linked, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            member = tarfile.TarInfo("report/escape")
            member.type = tarfile.SYMTYPE
            member.linkname = "../../outside"
            archive.addfile(member)
        unsafe_bundle = linked.getvalue()
        unsafe_manifest = rm.produce_static_manifest(
            static_fixtures.IDENTITY, artifact_id=self.fixture.selection.artifact_id,
            bundle=unsafe_bundle, bundle_length=len(unsafe_bundle))
        sink = Capture()
        self.run_frame(static_frame(unsafe_manifest, unsafe_bundle),
                       static_invocation(self.fixture, executor=fake), sink=sink)
        self.assertEqual(sink.receipt()["status"], "failed")
        self.assertEqual(fake.calls, [])
        self.assertEqual(list(self.fixture.assets_parent.iterdir()), [])
        fake = static_fixtures.FakeExecutor(preserved=((str(self.fixture.app / "Caddyfile"), "/etc/caddy/Caddyfile"),))
        _, good = self.run_frame(static_frame(self.fixture.manifest, self.fixture.archive),
            static_invocation(self.fixture, executor=fake))
        self.assertEqual(good.receipt()["status"], "succeeded")
        self.assertTrue(any("up" in call[0] for call in fake.calls))

    def test_static_post_activation_failure_reports_recovery_without_path(self):
        fake = static_fixtures.FakeExecutor(
            fail_at="up", preserved=((str(self.fixture.app / "Caddyfile"), "/etc/caddy/Caddyfile"),))
        sink = Capture()
        self.run_frame(static_frame(self.fixture.manifest, self.fixture.archive),
                       static_invocation(self.fixture, executor=fake), sink=sink)
        receipt = sink.receipt()
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["effect"], "unknown")
        self.assertTrue(receipt["recovery_required"])
        self.assertNotIn(str(self.fixture.assets_parent), bytes(sink.data).decode("ascii"))
        retained = list(self.fixture.assets_parent.iterdir())
        self.assertEqual(len(retained), 1)
        self.assertEqual((retained[0] / "report" / "index.html").read_bytes(), b"immutable site")

    def test_hostile_sdk_exception_text_never_appears_in_receipt(self):
        fake = static_fixtures.FakeExecutor(fail_at="up",
            preserved=((str(self.fixture.app / "Caddyfile"), "/etc/caddy/Caddyfile"),))
        # Exercise actual SDK error handling while keeping the executor output private.
        sink = Capture()
        self.run_frame(static_frame(self.fixture.manifest, self.fixture.archive),
                       static_invocation(self.fixture, executor=fake), sink=sink)
        self.assertNotIn(b"synthetic private error", bytes(sink.data))
        self.assertNotIn(b"synthetic_secret", bytes(sink.data))
        self.assertTrue(any("up" in call[0] for call in fake.calls))
        self.assertEqual(sink.receipt()["status"], "failed")
        self.assertTrue(sink.receipt()["recovery_required"])


@pytest.fixture
def image_case():
    fixture = image_fixtures.TargetFixture(methodName="setUp")
    fixture.setUp()
    try:
        yield fixture
    finally:
        fixture.tearDown()
        fixture.doCleanups()


def positive_image(fixture):
    executor = image_fixtures.FakeExecutor()
    sink = Capture()
    entry.run_target_stream(image_invocation(fixture, executor=executor), GuardedReader(image_frame(fixture.raw)), sink)
    assert sink.receipt()["status"] == "succeeded"
    assert sink.receipt()["image"] == image_fixtures.IMAGE
    assert any("up" in call[0] for call in executor.calls)


@pytest.mark.parametrize("kind", ["magic", "version", "unknown_kind", "zero_manifest", "large_manifest",
    "image_bundle", "large_bundle", "empty_static", "static_kind", "truncated_header", "truncated_manifest", "second_frame"])
def test_each_bad_frame_refuses_before_payload_effect_with_local_positive(image_case, kind):
    good = image_frame(image_case.raw)
    header = lambda version=1, mode=1, manifest=len(image_case.raw), bundle=0: struct.pack("!8sBBII", b"PTARGET1", version, mode, manifest, bundle)
    raw = {
        "magic": b"WRONGMAG" + good[8:], "version": header(version=2),
        "unknown_kind": header(mode=3), "zero_manifest": header(manifest=0),
        "large_manifest": header(manifest=16385), "image_bundle": header(bundle=1),
        "large_bundle": header(mode=2, bundle=67108865), "empty_static": header(mode=2),
        "static_kind": header(mode=2, bundle=1), "truncated_header": good[:17],
        "truncated_manifest": good[:-1], "second_frame": good + good,
    }[kind]
    reader, sink = GuardedReader(raw), Capture()
    executor = image_fixtures.FakeExecutor()
    entry.run_target_stream(image_invocation(image_case, executor=executor), reader, sink)
    assert sink.receipt()["status"] == "failed" and sink.receipt()["effect"] == "none"
    assert executor.calls == image_case.resolver_calls == [] and not list(image_case.tmp.iterdir())
    if kind not in ("truncated_header", "truncated_manifest", "second_frame"):
        assert reader.requests == [18]
    positive_image(image_case)


@pytest.mark.parametrize("kind", ["project", "environment", "path", "service", "namespace", "correlation", "subclass", "timeout_huge"])
def test_malformed_trusted_invocation_is_refused_before_input_and_not_reflected(image_case, kind):
    changes = {
        "project": {"target": replace(image_case.target, project_name="bad\nsynthetic_secret")},
        "environment": {"target": replace(image_case.target, environment="production")},
        "path": {"target": replace(image_case.target, application_dir="/srv/../../bad;synthetic_secret")},
        "service": {"target": replace(image_case.target, image_services=("bad;synthetic_secret",))},
        "namespace": {"image_namespace": "bad\nsynthetic_secret"},
        "correlation": {"correlation": "bad\nsynthetic_secret"},
        "timeout_huge": {"timeout_seconds": 10**1000},
    }
    if kind == "subclass":
        class Derived(pt.TargetSpec): pass
        altered = object.__new__(Derived)
        options = {"target": altered}
    else:
        options = changes[kind]
    reader, sink, executor = GuardedReader(image_frame(image_case.raw)), Capture(), image_fixtures.FakeExecutor()
    with pytest.raises(entry.EntryError) as caught:
        entry.run_target_stream(image_invocation(image_case, executor=executor, **options), reader, sink)
    assert reader.requests == [] and sink.data == b"" and executor.calls == image_case.resolver_calls == []
    assert "synthetic_secret" not in str(caught.value) and caught.value.__suppress_context__
    positive_image(image_case)


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), 10**1000])
def test_invalid_clock_is_fixed_refusal_before_input_with_positive(image_case, value):
    reader = GuardedReader(image_frame(image_case.raw))
    with pytest.raises(entry.EntryError, match="clock_invalid"):
        entry.run_target_stream(image_invocation(image_case, clock=lambda: value), reader, Capture())
    assert reader.requests == [] and image_case.resolver_calls == []
    positive_image(image_case)


@pytest.mark.parametrize("kind", ["short", "raise", "flush", "late_write", "late_flush"])
def test_lost_partial_or_late_receipt_after_real_sdk_does_not_retry_or_claim_rollback(image_case, kind):
    clock = MutableClock()
    class Writer(Capture):
        def write(self, data):
            if kind == "short":
                self.calls += 1; self.data.extend(data[:5]); return 5
            if kind == "raise":
                self.calls += 1; raise OSError("synthetic_secret_writer")
            result = super().write(data)
            if kind == "late_write": clock.value = 2
            return result
        def flush(self):
            if kind == "flush": raise OSError("synthetic_secret_flush")
            if kind == "late_flush": clock.value = 2
    writer, executor = Writer(), image_fixtures.FakeExecutor()
    with pytest.raises(entry.EntryError, match="receipt_write_failed_ambiguous") as caught:
        entry.run_target_stream(image_invocation(image_case, executor=executor, clock=clock, timeout_seconds=1),
            GuardedReader(image_frame(image_case.raw)), writer)
    assert writer.calls == 1 and sum("up" in call[0] for call in executor.calls) == 1
    assert "synthetic_secret" not in str(caught.value) and caught.value.__suppress_context__
    assert b'"status":"failed"' not in writer.data
    positive_image(image_case)


def test_clock_history_guard_is_shared_inside_actual_sdk_not_only_before_and_after(image_case):
    clock = MutableClock(10)
    def backwards_during_pull(argv):
        if "pull" in argv: clock.value = 9
    executor, sink = image_fixtures.FakeExecutor(hook=backwards_during_pull), Capture()
    with pytest.raises(entry.EntryError, match="receipt_write_failed_ambiguous"):
        entry.run_target_stream(image_invocation(image_case, executor=executor, clock=clock),
            GuardedReader(image_frame(image_case.raw)), sink)
    assert sink.data == b"" and clock.value == 9
    assert any("pull" in call[0] for call in executor.calls)
    assert not any("up" in call[0] for call in executor.calls)
    positive_image(image_case)


@pytest.mark.parametrize("kind,code", [("expiry", "deadline_exceeded"), ("clock", "clock_invalid")])
def test_pre_sdk_refusal_output_deadline_is_enforced_without_effect_or_second_receipt(image_case, kind, code):
    clock = MutableClock(10)
    class LateRefusal(Capture):
        def flush(self):
            clock.value = 11 if kind == "expiry" else float("nan")
    writer, executor = LateRefusal(), image_fixtures.FakeExecutor()
    bad = b"BADMAGIC" + image_frame(image_case.raw)[8:]
    with pytest.raises(entry.EntryError) as caught:
        entry.run_target_stream(image_invocation(image_case, executor=executor, clock=clock, timeout_seconds=.5),
            GuardedReader(bad), writer)
    assert caught.value.code == code and writer.calls == 1
    assert writer.receipt()["status"] == "failed" and writer.receipt()["effect"] == "none"
    assert executor.calls == image_case.resolver_calls == []
    positive_image(image_case)


def test_actual_sdk_cleanup_failure_cannot_be_reported_as_success(image_case):
    executor, sink = image_fixtures.FakeExecutor(), Capture()
    with mock.patch.object(pt.shutil, "rmtree", side_effect=OSError("synthetic_secret_cleanup")):
        entry.run_target_stream(image_invocation(image_case, executor=executor),
            GuardedReader(image_frame(image_case.raw)), sink)
    assert any("up" in call[0] for call in executor.calls)
    assert sink.receipt()["status"] == "failed" and sink.receipt()["effect"] == "unknown"
    assert b"synthetic_secret_cleanup" not in sink.data
    positive_image(image_case)


def test_real_static_unhealthy_observation_is_failure_and_retains_recovery_assets():
    fixture = static_fixtures.StaticTargetTests(methodName="setUp")
    fixture.setUp()
    try:
        executor = static_fixtures.FakeExecutor(unhealthy=True,
            preserved=((str(fixture.app / "Caddyfile"), "/etc/caddy/Caddyfile"),))
        sink = Capture()
        entry.run_target_stream(static_invocation(fixture, executor=executor),
            GuardedReader(static_frame(fixture.manifest, fixture.archive)), sink)
        assert sink.receipt()["status"] == "failed" and sink.receipt()["recovery_required"]
        assert len(list(fixture.assets_parent.iterdir())) == 1
        good = Capture()
        positive = static_fixtures.FakeExecutor(
            preserved=((str(fixture.app / "Caddyfile"), "/etc/caddy/Caddyfile"),))
        entry.run_target_stream(static_invocation(fixture, executor=positive),
            GuardedReader(static_frame(fixture.manifest, fixture.archive)), good)
        assert good.receipt()["status"] == "succeeded" and any("up" in call[0] for call in positive.calls)
    finally:
        fixture.tearDown()
        fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
