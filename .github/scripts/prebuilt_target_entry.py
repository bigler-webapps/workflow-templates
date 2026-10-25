"""Fixed, bounded stdin entry framing for the existing prebuilt target SDKs.

This module is intentionally inactive when imported. An independently trusted,
installed launcher must construct an enabled :class:`Invocation` and provide
the target-side streams and capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass, replace, replace
import json
import math
import re
import struct
import time
from typing import BinaryIO, Callable, Mapping

import prebuilt_static_target as static_target
import prebuilt_target as image_target
import release_manifest


MAX_STREAM_READ = 64 * 1024
MAX_MANIFEST_BYTES = release_manifest.MAX_MANIFEST_BYTES
MAX_BUNDLE_BYTES = release_manifest.MAX_BUNDLE_BYTES
MAX_EXECUTION_SECONDS = image_target.MAX_DEPLOY_SECONDS
MAX_RECEIPT_BYTES = 4096
_MAGIC = b"PTARGET1"
_VERSION = 1
_IMAGE_KIND = 1
_STATIC_KIND = 2
_HEADER = struct.Struct("!8sBBII")
_CORRELATION = re.compile(r"deploy-[0-9a-f]{40}\Z")


class EntryError(RuntimeError):
    """Stable failure code with no input or exception text attached."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class Invocation:
    """Trusted launcher configuration, kept separate from frame bytes.

    Set ``enabled`` only in the future authorized target launcher. The two
    output-specific capability pairs are mutually exclusive and validated
    before the input stream is touched.
    """

    mode: str
    expected_identity: release_manifest.ReleaseIdentity
    target: image_target.TargetSpec | static_target.StaticTargetSpec
    correlation: str
    image_namespace: str | None = None
    resolve_pushed_image: Callable[[str, str], Mapping[str, object]] | None = None
    selected_artifact: release_manifest.StaticArtifactSelection | None = None
    executor: object | None = None
    clock: Callable[[], float] = time.monotonic
    timeout_seconds: float = MAX_EXECUTION_SECONDS
    enabled: bool = False


def _now(clock: Callable[[], float], *, previous: float | None = None) -> float:
    try:
        value = clock()
    except BaseException:
        raise EntryError("clock_invalid") from None
    if not image_target._finite_number(value):
        raise EntryError("clock_invalid") from None
    result = float(value)
    if previous is not None and result < previous:
        raise EntryError("clock_invalid") from None
    return result


class _ClockGuard:
    """One shared monotonic view used by parsing and the selected SDK."""

    def __init__(self, clock: Callable[[], float]):
        self._clock = clock
        self._previous: float | None = None

    def __call__(self) -> float:
        current = _now(self._clock, previous=self._previous)
        self._previous = current
        return current


def _validate_invocation(value: object) -> tuple[Invocation, float, float]:
    if type(value) is not Invocation:
        raise EntryError("invocation_invalid") from None
    inv = value
    if type(inv.enabled) is not bool or inv.enabled is not True:
        raise EntryError("inactive") from None
    try:
        if (type(inv.mode) is not str or inv.mode not in ("image", "static")
                or type(inv.expected_identity) is not release_manifest.ReleaseIdentity
                or type(inv.correlation) is not str or not _CORRELATION.fullmatch(inv.correlation)
                or not callable(inv.clock)):
            raise ValueError
        release_manifest._validate_identity(inv.expected_identity)
        if inv.mode == "image":
            if (type(inv.target) is not image_target.TargetSpec
                    or type(inv.image_namespace) is not str or len(inv.image_namespace) > 256
                    or not release_manifest._IMAGE_NAMESPACE.fullmatch(inv.image_namespace)
                    or not callable(inv.resolve_pushed_image)
                    or isinstance(inv.resolve_pushed_image, type)
                    or inv.selected_artifact is not None):
                raise ValueError
            # Existing pure validators inspect configuration data, not files.
            image_target._validate_target(inv.target, inv.expected_identity)
        else:
            selected = inv.selected_artifact
            if (type(inv.target) is not static_target.StaticTargetSpec
                    or inv.image_namespace is not None or inv.resolve_pushed_image is not None
                    or type(selected) is not release_manifest.StaticArtifactSelection
                    or type(selected.name) is not str
                    or selected.name != release_manifest.static_artifact_name(inv.expected_identity)
                    or type(selected.artifact_id) is not int or not 0 < selected.artifact_id <= 2**63 - 1):
                raise ValueError
            static_target._validate_spec(inv.target, inv.expected_identity, selected)
        if inv.executor is not None and (
                isinstance(inv.executor, type) or not callable(getattr(inv.executor, "run", None))):
            raise ValueError
    except BaseException:
        raise EntryError("invocation_invalid") from None
    if (not image_target._finite_number(inv.timeout_seconds)
            or not 0 < inv.timeout_seconds <= MAX_EXECUTION_SECONDS):
        raise EntryError("deadline_invalid") from None
    started = _now(inv.clock)
    deadline = started + float(inv.timeout_seconds)
    if not math.isfinite(deadline) or deadline <= started:
        raise EntryError("deadline_invalid") from None
    return inv, started, deadline


def _check_deadline(inv: Invocation, previous: float, deadline: float) -> float:
    current = _now(inv.clock, previous=previous)
    if current >= deadline:
        raise EntryError("deadline_exceeded") from None
    return current


def _read_exact(stream: BinaryIO, count: int, inv: Invocation,
                previous: float, deadline: float) -> tuple[bytes, float]:
    if type(count) is not int or count < 0:
        raise EntryError("frame_invalid") from None
    chunks = bytearray()
    while len(chunks) < count:
        previous = _check_deadline(inv, previous, deadline)
        requested = min(MAX_STREAM_READ, count - len(chunks))
        try:
            part = stream.read(requested)
        except BaseException:
            raise EntryError("stream_read_failed") from None
        previous = _check_deadline(inv, previous, deadline)
        if type(part) is not bytes or len(part) > requested:
            raise EntryError("stream_read_invalid") from None
        if not part:
            raise EntryError("frame_truncated") from None
        chunks.extend(part)
    return bytes(chunks), previous


def _read_frame(stream: BinaryIO, inv: Invocation, previous: float,
                deadline: float) -> tuple[bytes, bytes, float]:
    raw_header, previous = _read_exact(stream, _HEADER.size, inv, previous, deadline)
    try:
        magic, version, kind, manifest_length, bundle_length = _HEADER.unpack(raw_header)
    except struct.error:
        raise EntryError("frame_invalid") from None
    if magic != _MAGIC or version != _VERSION or kind not in (_IMAGE_KIND, _STATIC_KIND):
        raise EntryError("frame_invalid") from None
    if manifest_length <= 0 or manifest_length > MAX_MANIFEST_BYTES:
        raise EntryError("manifest_length_invalid") from None
    if kind == _IMAGE_KIND:
        if bundle_length != 0:
            raise EntryError("frame_kind_mismatch") from None
    elif bundle_length <= 0 or bundle_length > MAX_BUNDLE_BYTES:
        raise EntryError("bundle_length_invalid") from None
    if (inv.mode == "image") != (kind == _IMAGE_KIND):
        raise EntryError("frame_kind_mismatch") from None
    manifest, previous = _read_exact(stream, manifest_length, inv, previous, deadline)
    bundle = b""
    if kind == _STATIC_KIND:
        bundle, previous = _read_exact(stream, bundle_length, inv, previous, deadline)
    previous = _check_deadline(inv, previous, deadline)
    try:
        trailing = stream.read(1)
    except BaseException:
        raise EntryError("stream_read_failed") from None
    previous = _check_deadline(inv, previous, deadline)
    if type(trailing) is not bytes or len(trailing) > 1:
        raise EntryError("stream_read_invalid") from None
    if trailing:
        raise EntryError("frame_trailing_bytes") from None
    return manifest, bundle, previous


def _safe_sdk_code(exc: BaseException) -> str:
    # Exception messages and even exception attributes are untrusted output.
    # Keep SDK failures in a single fixed bucket; never echo caller text.
    return "deployment_failed"


def _base_receipt(inv: Invocation, status: str, effect: str) -> dict[str, object]:
    target = inv.target
    identity = inv.expected_identity
    return {
        "version": _VERSION,
        "correlation": inv.correlation,
        "status": status,
        "effect": effect,
        "source_repository": identity.repository,
        "source_sha": identity.sha,
        "run_id": identity.run_id,
        "attempt": identity.attempt,
        "environment": target.environment,
        "project": target.project_name,
        "output_kind": inv.mode,
    }


def _error_receipt(inv: Invocation, code: str, effect: str,
                   *, recovery_required: bool = False) -> dict[str, object]:
    receipt = _base_receipt(inv, "failed", effect)
    receipt.update({"error_code": code, "recovery_required": recovery_required})
    return receipt


def _success_receipt(inv: Invocation, result: object) -> dict[str, object]:
    if inv.mode == "image":
        if type(result) is not image_target.DeploymentResult:
            raise EntryError("sdk_result_invalid") from None
        if (result.project_name != inv.target.project_name
                or result.environment != inv.target.environment
                or result.source_repository != inv.expected_identity.repository
                or result.source_sha != inv.expected_identity.sha
                or result.run_id != inv.expected_identity.run_id
                or result.attempt != inv.expected_identity.attempt
                or type(result.image) is not str
                or not result.image.startswith(inv.image_namespace + "@sha256:")
                or type(result.services) is not tuple
                or any(type(service) is not str for service in result.services)
                or type(result.migrated) is not bool):
            raise EntryError("sdk_result_invalid") from None
        receipt = _base_receipt(inv, "succeeded", "verified")
        receipt.update({"health": "healthy", "image": result.image,
                        "services": list(result.services), "migrated": result.migrated})
        return receipt
    if type(result) is not static_target.StaticDeploymentResult:
        raise EntryError("sdk_result_invalid") from None
    if (result.project_name != inv.target.project_name
            or result.environment != inv.target.environment
            or result.source_repository != inv.expected_identity.repository
            or result.source_sha != inv.expected_identity.sha
            or result.run_id != inv.expected_identity.run_id
            or result.attempt != inv.expected_identity.attempt
            or type(result.artifact_id) is not int
            or result.artifact_id != inv.selected_artifact.artifact_id
            or type(result.artifact_sha256) is not str
            or not re.fullmatch(r"[0-9a-f]{64}\Z", result.artifact_sha256)
            or type(result.static_image) is not str
            or type(result.services) is not tuple
            or any(type(service) is not str for service in result.services)):
        raise EntryError("sdk_result_invalid") from None
    receipt = _base_receipt(inv, "succeeded", "verified")
    receipt.update({"health": "healthy", "artifact_id": result.artifact_id,
                    "artifact_sha256": result.artifact_sha256, "image": result.static_image,
                    "services": list(result.services)})
    return receipt


def _encode_receipt(value: dict[str, object]) -> bytes:
    try:
        encoded = (json.dumps(value, ensure_ascii=True, allow_nan=False,
                              separators=(",", ":"), sort_keys=True) + "\n").encode("ascii")
    except BaseException:
        raise EntryError("receipt_invalid") from None
    if len(encoded) > MAX_RECEIPT_BYTES:
        raise EntryError("receipt_too_large") from None
    return encoded


def _write_receipt(stream: BinaryIO, payload: bytes) -> None:
    try:
        written = stream.write(payload)
        if type(written) is not int or written != len(payload):
            raise OSError
        flush = getattr(stream, "flush", None)
        if flush is not None:
            if not callable(flush):
                raise OSError
            flush()
    except BaseException:
        # An effect may already have happened. Never retry a lost acknowledgement.
        raise EntryError("receipt_write_failed_ambiguous") from None


def run_target_stream(invocation: Invocation, source: BinaryIO, sink: BinaryIO) -> None:
    """Read one frame, call exactly one actual SDK, and emit one safe receipt.

    Reads are cooperative: the local transport must enforce its own blocking
    read deadline. This function never creates threads or starts processes.
    """
    inv, started, deadline = _validate_invocation(invocation)
    original_clock = inv.clock
    def guarded_clock():
        current = _now(original_clock, previous=guarded_clock.last)
        guarded_clock.last = current
        return current
    guarded_clock.last = started
    # The same history guard spans parsing, every SDK clock read and output.
    inv = replace(inv, clock=guarded_clock)
    sink_flush = getattr(sink, "flush", None)
    if (not callable(getattr(source, "read", None))
            or not callable(getattr(sink, "write", None))
            or (sink_flush is not None and not callable(sink_flush))):
        raise EntryError("stream_capability_missing") from None
    last = started
    sdk_started = False
    recovery_required = False
    try:
        manifest, bundle, last = _read_frame(source, inv, last, deadline)
        last = _check_deadline(inv, last, deadline)
        sdk_started = True
        if inv.mode == "image":
            result = image_target.execute_image_deployment(
                manifest, expected_identity=inv.expected_identity,
                expected_image_namespace=inv.image_namespace,
                resolve_pushed_image=inv.resolve_pushed_image,
                target=inv.target, executor=inv.executor, clock=inv.clock, deadline=deadline)
        else:
            result = static_target.execute_static_deployment(
                manifest, expected_identity=inv.expected_identity,
                selected_artifact=inv.selected_artifact, bundle=bundle,
                target=inv.target, executor=inv.executor, clock=inv.clock, deadline=deadline)
        last = _check_deadline(inv, last, deadline)
        receipt = _success_receipt(inv, result)
    except EntryError as exc:
        effect = "unknown" if sdk_started else "none"
        receipt = _error_receipt(inv, exc.code, effect)
    except image_target.PrebuiltTargetError as exc:
        receipt = _error_receipt(inv, _safe_sdk_code(exc), "unknown")
    except static_target.StaticTargetError as exc:
        recovery_required = type(getattr(exc, "retained_asset_directory", None)) is str
        receipt = _error_receipt(inv, _safe_sdk_code(exc), "unknown",
                                 recovery_required=recovery_required)
    except BaseException:
        receipt = _error_receipt(inv, "deployment_failed", "unknown")
    payload = _encode_receipt(receipt)
    try:
        last = _check_deadline(inv, last, deadline)
    except EntryError:
        if sdk_started:
            raise EntryError("receipt_write_failed_ambiguous") from None
        raise
    _write_receipt(sink, payload)
    try:
        _check_deadline(inv, last, deadline)
    except EntryError:
        # Output cannot be retracted; the transport must reconcile the effect.
        # Do not retry or send a contradictory second receipt.
        if sdk_started:
            raise EntryError("receipt_write_failed_ambiguous") from None
        raise
