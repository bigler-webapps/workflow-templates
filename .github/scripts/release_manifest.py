"""Offline producer and validator for bounded release manifests.

This module deliberately has no network, subprocess, environment, or archive
handling code. Trusted callers supply expected release identity and a registry
reader; manifest claims never select deployment routing or authorize a release.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import BinaryIO, Callable, Mapping


MANIFEST_VERSION = 1
MAX_MANIFEST_BYTES = 16 * 1024
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
CHUNK_BYTES = 64 * 1024
STATIC_ARTIFACT_PREFIX = "release-static"

_REPOSITORY = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_.-]{1,100}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_IMAGE = re.compile(r"ghcr\.io/[a-z0-9](?:[a-z0-9-]*[a-z0-9])?/[a-z0-9][a-z0-9._-]*-backend@sha256:[0-9a-f]{64}\Z")
_IMAGE_NAMESPACE = re.compile(r"ghcr\.io/[a-z0-9](?:[a-z0-9-]*[a-z0-9])?/[a-z0-9][a-z0-9._-]*-backend\Z")
_WORKFLOW = re.compile(r"\.github/workflows/[A-Za-z0-9][A-Za-z0-9._-]*\.yml\Z")
_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")
_SAFE_ARTIFACT = re.compile(r"release-static-[1-9][0-9]*-[1-9][0-9]*\Z")
_TOP_KEYS = {
    "version", "kind", "source_repository", "source_sha", "source_ref",
    "source_workflow", "run_id", "run_attempt", "output",
}


class ManifestError(ValueError):
    """A stable, non-sensitive manifest contract failure."""


@dataclass(frozen=True)
class ReleaseIdentity:
    """Expected values obtained independently from trusted release metadata."""

    repository: str
    sha: str
    ref: str
    workflow: str
    run_id: int
    attempt: int


@dataclass(frozen=True)
class StaticArtifactSelection:
    """Artifact name and ID selected by trusted external run metadata."""

    name: str
    artifact_id: int


def _fail(code: str) -> None:
    raise ManifestError(code) from None


def _valid_text(value: object) -> bool:
    return isinstance(value, str) and bool(value) and all(ord(char) >= 0x20 and ord(char) != 0x7F for char in value)


def _validate_identity(identity: ReleaseIdentity) -> None:
    if not isinstance(identity, ReleaseIdentity):
        _fail("invalid_expected_identity")
    if (not isinstance(identity.repository, str) or not _REPOSITORY.fullmatch(identity.repository)
            or identity.repository.split("/")[-1] in (".", "..")):
        _fail("invalid_expected_repository")
    if not isinstance(identity.sha, str) or not _SHA.fullmatch(identity.sha):
        _fail("invalid_expected_sha")
    if identity.ref not in ("refs/heads/develop", "refs/heads/main"):
        _fail("invalid_expected_ref")
    if not isinstance(identity.workflow, str) or not _WORKFLOW.fullmatch(identity.workflow):
        _fail("invalid_expected_workflow")
    if type(identity.run_id) is not int or identity.run_id <= 0:
        _fail("invalid_expected_run_id")
    if type(identity.attempt) is not int or identity.attempt <= 0:
        _fail("invalid_expected_attempt")


def _base(identity: ReleaseIdentity, kind: str, output: dict) -> dict:
    _validate_identity(identity)
    return {
        "version": MANIFEST_VERSION,
        "kind": kind,
        "source_repository": identity.repository,
        "source_sha": identity.sha,
        "source_ref": identity.ref,
        "source_workflow": identity.workflow,
        "run_id": identity.run_id,
        "run_attempt": identity.attempt,
        "output": output,
    }


def _encode_manifest(manifest: Mapping[str, object]) -> bytes:
    """Private bounded internal encoder; never a public dictionary input API."""
    try:
        encoded = json.dumps(
            manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _fail("invalid_manifest_value")
    if len(encoded) > MAX_MANIFEST_BYTES:
        _fail("manifest_too_large")
    return encoded


def release_image_tag(identity: ReleaseIdentity) -> str:
    """Bind registry lookup to the exact source SHA and workflow generation."""
    _validate_identity(identity)
    tag = f"release-{identity.run_id}-{identity.attempt}-{identity.sha}"
    if not _TAG.fullmatch(tag):
        _fail("invalid_expected_image_tag")
    return tag


def _resolved_image(identity, expected_namespace, pushed_tag, resolve_pushed_image):
    _validate_identity(identity)
    if not isinstance(expected_namespace, str) or not _IMAGE_NAMESPACE.fullmatch(expected_namespace):
        _fail("invalid_image_namespace")
    if not isinstance(pushed_tag, str) or not _TAG.fullmatch(pushed_tag):
        _fail("invalid_image_tag")
    if pushed_tag != release_image_tag(identity):
        _fail("release_image_tag_mismatch")
    if not callable(resolve_pushed_image):
        _fail("registry_reader_required")
    try:
        resolved = resolve_pushed_image(expected_namespace, pushed_tag)
    except Exception:
        _fail("registry_resolution_failed")
    if not isinstance(resolved, Mapping):
        _fail("registry_resolution_invalid")
    try:
        repository, tag, digest = (resolved.get(key) for key in ("repository", "tag", "digest"))
    except Exception:
        _fail("registry_resolution_failed")
    if repository != expected_namespace or tag != pushed_tag:
        _fail("registry_identity_mismatch")
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        _fail("registry_digest_invalid")
    image = f"{expected_namespace}@{digest}"
    if not _IMAGE.fullmatch(image):
        _fail("registry_image_invalid")
    return image


def produce_image_manifest(
    identity: ReleaseIdentity,
    *,
    expected_namespace: str,
    pushed_tag: str,
    resolve_pushed_image: Callable[[str, str], Mapping[str, object]],
) -> bytes:
    """Resolve the just-pushed tag via an injected read client, then emit digest.

    The resolver must return repository, tag, and digest from registry metadata;
    a caller-supplied digest string is not accepted as an alternative.
    """
    image = _resolved_image(identity, expected_namespace, pushed_tag, resolve_pushed_image)
    return _encode_manifest(_base(identity, "image", {"image": image}))


def static_artifact_name(identity: ReleaseIdentity) -> str:
    _validate_identity(identity)
    return f"{STATIC_ARTIFACT_PREFIX}-{identity.run_id}-{identity.attempt}"


def _read_bundle(source: bytes | bytearray | BinaryIO, expected_length: int) -> tuple[int, str]:
    if type(expected_length) is not int or not 0 <= expected_length <= MAX_BUNDLE_BYTES:
        _fail("bundle_length_invalid")
    digest = hashlib.sha256()
    total = 0
    if isinstance(source, (bytes, bytearray)):
        if len(source) > MAX_BUNDLE_BYTES:
            _fail("bundle_too_large")
        chunks = (source[i:i + CHUNK_BYTES] for i in range(0, len(source), CHUNK_BYTES))
    elif callable(getattr(source, "read", None)):
        def stream_chunks():
            while True:
                limit = min(CHUNK_BYTES, expected_length - total + 1)
                try:
                    chunk = source.read(limit)
                except Exception:
                    _fail("bundle_read_failed")
                if not isinstance(chunk, bytes) or len(chunk) > limit:
                    _fail("bundle_stream_invalid")
                if chunk == b"":
                    break
                yield chunk
        chunks = stream_chunks()
    else:
        _fail("bundle_stream_invalid")
    for chunk in chunks:
        total += len(chunk)
        if total > MAX_BUNDLE_BYTES or total > expected_length:
            _fail("bundle_length_mismatch")
        digest.update(chunk)
    if total != expected_length:
        _fail("bundle_length_mismatch")
    return total, digest.hexdigest()


def produce_static_manifest(
    identity: ReleaseIdentity,
    *,
    artifact_id: int,
    bundle: bytes | bytearray | BinaryIO,
    bundle_length: int,
) -> bytes:
    """Create a manifest for a static bundle, without an image or extraction."""
    name = static_artifact_name(identity)
    if type(artifact_id) is not int or artifact_id <= 0:
        _fail("artifact_id_invalid")
    length, sha256 = _read_bundle(bundle, bundle_length)
    output = {"artifact_name": name, "artifact_id": artifact_id, "byte_length": length, "sha256": sha256}
    return _encode_manifest(_base(identity, "static", output))


def _pairs_no_duplicates(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    _fail("non_finite_json_number")


def _parse_manifest(raw: bytes | bytearray) -> dict:
    if not isinstance(raw, (bytes, bytearray)):
        _fail("manifest_bytes_required")
    if len(raw) > MAX_MANIFEST_BYTES:
        _fail("manifest_too_large")
    try:
        text = bytes(raw).decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _fail("manifest_utf8_invalid")
    try:
        value = json.loads(text, object_pairs_hook=_pairs_no_duplicates, parse_constant=_reject_constant)
    except ManifestError:
        raise
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
        _fail("manifest_json_invalid")
    if not isinstance(value, dict):
        _fail("manifest_object_required")
    if set(value) != _TOP_KEYS:
        _fail("manifest_fields_invalid")
    if type(value.get("version")) is not int or value["version"] != MANIFEST_VERSION:
        _fail("manifest_version_invalid")
    if value.get("kind") not in ("image", "static"):
        _fail("manifest_kind_invalid")
    if type(value.get("run_id")) is not int or value["run_id"] <= 0:
        _fail("manifest_run_id_invalid")
    if type(value.get("run_attempt")) is not int or value["run_attempt"] <= 0:
        _fail("manifest_attempt_invalid")
    for field in ("source_repository", "source_sha", "source_ref", "source_workflow"):
        if not _valid_text(value.get(field)):
            _fail("manifest_identity_invalid")
    if not isinstance(value["output"], dict):
        _fail("manifest_output_invalid")
    if bytes(raw) != _encode_manifest(value):
        _fail("manifest_noncanonical")
    return value


def validate_manifest_structure(
    raw: bytes | bytearray,
    *,
    expected: ReleaseIdentity,
    expected_image_namespace: str | None = None,
    expected_static_artifact: StaticArtifactSelection | None = None,
) -> dict:
    """Pure preflight only; image digests are NOT verified against a registry."""
    _validate_identity(expected)
    if (expected_image_namespace is None) == (expected_static_artifact is None):
        _fail("exactly_one_output_expectation_required")
    manifest = _parse_manifest(raw)
    actual = (
        manifest["source_repository"], manifest["source_sha"], manifest["source_ref"],
        manifest["source_workflow"], manifest["run_id"], manifest["run_attempt"],
    )
    wanted = (expected.repository, expected.sha, expected.ref, expected.workflow, expected.run_id, expected.attempt)
    if actual != wanted:
        _fail("release_identity_mismatch")
    output = manifest["output"]
    if manifest["kind"] == "image":
        if set(output) != {"image"}:
            _fail("image_output_fields_invalid")
        if not isinstance(expected_image_namespace, str) or not _IMAGE_NAMESPACE.fullmatch(expected_image_namespace):
            _fail("expected_image_namespace_required")
        image = output.get("image")
        if not isinstance(image, str) or not _IMAGE.fullmatch(image):
            _fail("immutable_image_required")
        prefix = expected_image_namespace + "@"
        if not image.startswith(prefix):
            _fail("image_namespace_mismatch")
    else:
        if set(output) != {"artifact_name", "artifact_id", "byte_length", "sha256"}:
            _fail("static_output_fields_invalid")
        if not isinstance(expected_static_artifact, StaticArtifactSelection):
            _fail("expected_static_artifact_required")
        if not isinstance(expected_static_artifact.name, str) or not _SAFE_ARTIFACT.fullmatch(expected_static_artifact.name):
            _fail("expected_static_artifact_invalid")
        if type(expected_static_artifact.artifact_id) is not int or expected_static_artifact.artifact_id <= 0:
            _fail("expected_static_artifact_invalid")
        if output.get("artifact_name") != expected_static_artifact.name or type(output.get("artifact_id")) is not int or output["artifact_id"] != expected_static_artifact.artifact_id:
            _fail("static_artifact_mismatch")
        if expected_static_artifact.name != static_artifact_name(expected):
            _fail("static_artifact_name_mismatch")
        length, digest = output.get("byte_length"), output.get("sha256")
        if type(length) is not int or not 0 <= length <= MAX_BUNDLE_BYTES:
            _fail("static_bundle_metadata_invalid")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}\Z", digest):
            _fail("static_bundle_metadata_invalid")
    return manifest


def validate_manifest(
    raw: bytes | bytearray,
    *,
    expected: ReleaseIdentity,
    expected_image_namespace: str | None = None,
    expected_static_artifact: StaticArtifactSelection | None = None,
    resolve_pushed_image: Callable[[str, str], Mapping[str, object]] | None = None,
) -> dict:
    """Validate identity and bind image output to an independent registry read."""
    manifest = validate_manifest_structure(raw, expected=expected,
        expected_image_namespace=expected_image_namespace, expected_static_artifact=expected_static_artifact)
    if manifest["kind"] == "image":
        observed = _resolved_image(expected, expected_image_namespace,
                                   release_image_tag(expected), resolve_pushed_image)
        if manifest["output"]["image"] != observed:
            _fail("image_digest_mismatch")
    return manifest


def verify_static_bundle(source: bytes | bytearray | BinaryIO, *, manifest: Mapping[str, object]) -> tuple[int, str]:
    """Verify bytes against a previously validated static manifest; no extraction."""
    if not isinstance(manifest, Mapping) or manifest.get("kind") != "static" or not isinstance(manifest.get("output"), dict):
        _fail("validated_static_manifest_required")
    output = manifest["output"]
    length = output.get("byte_length")
    expected_hash = output.get("sha256")
    if type(length) is not int or not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}\Z", expected_hash):
        _fail("static_bundle_metadata_invalid")
    actual_length, actual_hash = _read_bundle(source, length)
    if actual_hash != expected_hash:
        _fail("static_bundle_hash_mismatch")
    return actual_length, actual_hash

