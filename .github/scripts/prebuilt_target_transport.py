"""Fixed SSH stream transport for the strict prebuilt target protocol.

Importing this module is inert.  A future protected controller must supply an
independently authorized, immutable route and release expectation.  This module
does not authorize work, install the remote launcher, or coordinate other
deployment channels.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import stat
import subprocess
import time
from typing import BinaryIO, Callable, Mapping

import prebuilt_static_target as static_target
import prebuilt_target as image_target
import prebuilt_target_entry as target_entry
import release_manifest


SSH = "/usr/bin/ssh"
REMOTE_LAUNCHER = "/usr/local/libexec/webapp-prebuilt-target-v1"
MAX_FRAME_BYTES = target_entry._HEADER.size + release_manifest.MAX_MANIFEST_BYTES + release_manifest.MAX_BUNDLE_BYTES
MAX_RECEIPT_BYTES = target_entry.MAX_RECEIPT_BYTES
MAX_EXECUTION_SECONDS = 1800.0
IO_CHUNK_BYTES = 64 * 1024
CLEANUP_GRACE_SECONDS = 0.2
_SAFE_USER = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,31}\Z")
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_REFUSAL_CODES = frozenset({
    "deadline_exceeded", "clock_invalid", "stream_read_failed",
    "stream_read_invalid", "frame_truncated", "frame_invalid", "manifest_length_invalid",
    "bundle_length_invalid", "frame_kind_mismatch", "frame_trailing_bytes",
})


class TransportError(RuntimeError):
    """Stable failure code; no raw route, path, exception, or peer text."""

    def __init__(self, code: str, *, ambiguous: bool = False):
        self.code = code
        self.ambiguous = ambiguous
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class TransportConfig:
    """Immutable trusted route and independently sourced expectations."""

    host: str
    port: int
    user: str
    identity_file: str
    known_hosts_file: str
    expected_identity: release_manifest.ReleaseIdentity
    target: image_target.TargetSpec | static_target.StaticTargetSpec
    correlation: str
    output_kind: str
    image_namespace: str | None = None
    resolve_pushed_image: Callable[[str, str], Mapping[str, object]] | None = None
    selected_artifact: release_manifest.StaticArtifactSelection | None = None
    enabled: bool = False


@dataclass(frozen=True, slots=True)
class DeploymentReceipt:
    """Validated receipt data; returned only after clean SSH completion."""

    status: str
    effect: str
    correlation: str
    source_repository: str
    source_sha: str
    run_id: int
    attempt: int
    environment: str
    project: str
    output_kind: str
    image: str | None = None
    services: tuple[str, ...] = ()
    migrated: bool | None = None
    artifact_id: int | None = None
    artifact_sha256: str | None = None
    error_code: str | None = None
    recovery_required: bool = False


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    path: str
    device: int
    inode: int
    mode: int
    owner: int
    links: int
    size: int
    mtime_ns: int


class _Monotonic:
    def __init__(self, clock: Callable[[], float], budget: float):
        self._clock = clock
        self._last: float | None = None
        try:
            now = self.read()
        except BaseException:
            raise TransportError("clock_invalid") from None
        self.deadline = now + budget
        if not math.isfinite(self.deadline) or self.deadline <= now:
            raise TransportError("deadline_invalid") from None

    def read(self) -> float:
        try:
            value = self._clock()
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError
            number = float(value)
            if not math.isfinite(number) or (self._last is not None and number < self._last):
                raise ValueError
            self._last = number
            return number
        except BaseException:
            raise TransportError("clock_invalid") from None

    def remaining(self) -> float:
        now = self.read()
        remaining = self.deadline - now
        if remaining <= 0:
            raise TransportError("deadline_exceeded") from None
        return remaining


def _canonical_host(value: object) -> bool:
    if (type(value) is not str or not value or len(value) > 253
            or value != value.lower() or '%' in value):
        return False
    try:
        address = ipaddress.ip_address(value)
        return str(address) == value
    except ValueError:
        if ":" in value or value.endswith(".") or all(c in "0123456789." for c in value):
            return False
        labels = value.split(".")
        return all(_DNS_LABEL.fullmatch(label) for label in labels)


def _absolute_provider_path(value: object) -> Path:
    if type(value) is not str or not value or len(value) > 4096:
        raise TransportError("provider_path_invalid") from None
    if "$" in value:
        raise TransportError("provider_path_invalid") from None
    path = PurePosixPath(value)
    if (not path.is_absolute() or ".." in path.parts or str(path) != value
            or any(ord(char) < 0x21 or ord(char) > 0x7e or char in '%\\"\'' for char in value)):
        raise TransportError("provider_path_invalid") from None
    return Path(value)


def _snapshot(path: Path, *, owner_rule: str, exact_mode: int | None = None) -> _FileSnapshot:
    try:
        info = path.lstat()
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1):
            raise ValueError
        mode = stat.S_IMODE(info.st_mode)
        euid = os.geteuid()
        if owner_rule == "identity" and info.st_uid != euid:
            raise ValueError
        if owner_rule == "known" and info.st_uid not in (0, euid):
            raise ValueError
        if mode & 0o022 or (exact_mode is not None and mode != exact_mode):
            raise ValueError
        if owner_rule == "identity" and info.st_size > 64 * 1024:
            raise ValueError
        if owner_rule == "known" and info.st_size > 4 * 1024 * 1024:
            raise ValueError
        return _FileSnapshot(str(path), info.st_dev, info.st_ino, info.st_mode,
                             info.st_uid, info.st_nlink, info.st_size, info.st_mtime_ns)
    except BaseException:
        raise TransportError("provider_file_invalid") from None


def _no_symlink_ancestors(path: Path) -> tuple[_FileSnapshot, ...]:
    """Bind the entire directory chain, including secure sticky-root temp roots.

    A root-owned sticky directory cannot let another UID replace the next
    root/current-owned component; all other writable ancestors are refused.
    Root and the controller UID remain trusted installation authorities.
    """
    cursor = Path(path.anchor)
    snapshots = []
    try:
        directories = [cursor]
        for part in path.parts[1:-1]:
            cursor /= part
            directories.append(cursor)
        for directory in directories:
            info = directory.lstat()
            mode = stat.S_IMODE(info.st_mode)
            root_sticky = info.st_uid == 0 and bool(mode & stat.S_ISVTX)
            if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or info.st_uid not in (0, os.geteuid())
                    or (mode & 0o022 and not root_sticky)):
                raise ValueError
            snapshots.append(_FileSnapshot(str(directory), info.st_dev, info.st_ino,
                                           info.st_mode, info.st_uid, 0, 0, 0))
        return tuple(snapshots)
    except BaseException:
        raise TransportError("provider_path_invalid") from None


def _private_parent(path: Path) -> _FileSnapshot:
    try:
        info = path.parent.lstat()
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError
        return _FileSnapshot(str(path.parent), info.st_dev, info.st_ino,
                             info.st_mode, info.st_uid, info.st_nlink, 0, 0)
    except BaseException:
        raise TransportError("provider_directory_invalid") from None


def _validate_provider_files(config: TransportConfig) -> tuple[_FileSnapshot, ...]:
    identity = _absolute_provider_path(config.identity_file)
    known_hosts = _absolute_provider_path(config.known_hosts_file)
    ancestors = _no_symlink_ancestors(identity) + _no_symlink_ancestors(known_hosts)
    ident_snap = _snapshot(identity, owner_rule="identity", exact_mode=0o600)
    known_snap = _snapshot(known_hosts, owner_rule="known")
    parent_snap = _private_parent(identity)
    # Root-owned known-host data may be installed centrally; it must never be writable by group/other.
    return ident_snap, known_snap, parent_snap, *ancestors


def _validate_config(config: object) -> tuple[TransportConfig, tuple[_FileSnapshot, ...], tuple[str, ...]]:
    if type(config) is not TransportConfig:
        raise TransportError("configuration_invalid") from None
    c = config
    if c.enabled is not True or type(c.enabled) is not bool:
        raise TransportError("inactive") from None
    if (not _canonical_host(c.host) or type(c.port) is not int or not 1 <= c.port <= 65535
            or type(c.user) is not str or not _SAFE_USER.fullmatch(c.user)
            or type(c.correlation) is not str or not target_entry._CORRELATION.fullmatch(c.correlation)
            or type(c.output_kind) is not str or c.output_kind not in ("image", "static")
            or type(c.expected_identity) is not release_manifest.ReleaseIdentity):
        raise TransportError("configuration_invalid") from None
    try:
        release_manifest._validate_identity(c.expected_identity)
        if c.output_kind == "image":
            if (type(c.target) is not image_target.TargetSpec
                    or type(c.image_namespace) is not str
                    or not release_manifest._IMAGE_NAMESPACE.fullmatch(c.image_namespace)
                    or not callable(c.resolve_pushed_image) or isinstance(c.resolve_pushed_image, type)
                    or c.selected_artifact is not None):
                raise ValueError
            image_target._validate_target(c.target, c.expected_identity)
            services = tuple(sorted((*c.target.image_services,
                                     *(name for name, _ in c.target.sidecar_images))))
        else:
            if (type(c.target) is not static_target.StaticTargetSpec
                    or c.image_namespace is not None or c.resolve_pushed_image is not None
                    or type(c.selected_artifact) is not release_manifest.StaticArtifactSelection
                    or type(c.selected_artifact.artifact_id) is not int
                    or not 0 < c.selected_artifact.artifact_id <= 2**63 - 1):
                raise ValueError
            static_target._validate_spec(c.target, c.expected_identity, c.selected_artifact)
            services = tuple(sorted((c.target.static_service,
                                     *(name for name, _ in c.target.sidecar_images))))
    except BaseException:
        raise TransportError("configuration_invalid") from None
    snapshots = _validate_provider_files(c)
    return c, snapshots, services


def _frame(config: TransportConfig, manifest: bytes, bundle: bytes) -> tuple[bytes, dict]:
    if type(manifest) is not bytes or not 0 < len(manifest) <= release_manifest.MAX_MANIFEST_BYTES:
        raise TransportError("manifest_invalid") from None
    if type(bundle) is not bytes or len(bundle) > release_manifest.MAX_BUNDLE_BYTES:
        raise TransportError("bundle_invalid") from None
    try:
        if config.output_kind == "image":
            if bundle:
                raise ValueError
            parsed = release_manifest.validate_manifest(
                manifest, expected=config.expected_identity,
                expected_image_namespace=config.image_namespace,
                resolve_pushed_image=config.resolve_pushed_image,
            )
            if parsed["kind"] != "image":
                raise ValueError
            kind = target_entry._IMAGE_KIND
        else:
            parsed = release_manifest.validate_manifest_structure(
                manifest, expected=config.expected_identity,
                expected_static_artifact=config.selected_artifact,
            )
            if parsed["kind"] != "static":
                raise ValueError
            release_manifest.verify_static_bundle(bundle, manifest=parsed)
            kind = target_entry._STATIC_KIND
        header = target_entry._HEADER.pack(target_entry._MAGIC, target_entry._VERSION,
                                           kind, len(manifest), len(bundle))
    except BaseException:
        raise TransportError("release_payload_invalid") from None
    frame = header + manifest + bundle
    if len(frame) > MAX_FRAME_BYTES:
        raise TransportError("frame_too_large") from None
    return frame, parsed


def _ssh_argv(config: TransportConfig) -> tuple[str, ...]:
    return (
        SSH, "-F", "/dev/null", "-T", "-p", str(config.port),
        "-i", config.identity_file,
        "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
        "-o", "CertificateFile=none", "-o", "PKCS11Provider=none",
        "-o", "SecurityKeyProvider=none", "-o", "AddKeysToAgent=no",
        "-o", "ConnectionAttempts=1", "-o", "EscapeChar=none",
        "-o", "BatchMode=yes", "-o", "NumberOfPasswordPrompts=0",
        "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
        "-o", "GSSAPIAuthentication=no", "-o", "HostbasedAuthentication=no",
        "-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no",
        "-o", "VerifyHostKeyDNS=no", "-o", f"UserKnownHostsFile={config.known_hosts_file}",
        "-o", "GlobalKnownHostsFile=/dev/null", "-o", "CanonicalizeHostname=no",
        "-o", "ClearAllForwardings=yes", "-o", "ForwardAgent=no", "-o", "ForwardX11=no",
        "-o", "PermitLocalCommand=no", "-o", "ProxyCommand=none", "-o", "ProxyJump=none",
        "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "ControlPersist=no",
        "-o", "RequestTTY=no", "-l", config.user, config.host,
        REMOTE_LAUNCHER, "--correlation", config.correlation,
    )


def _child_environment() -> dict[str, str]:
    return {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "HOME": "/nonexistent", "SSH_AUTH_SOCK": ""}


def _parse_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ValueError


def _parse_receipt(raw: bytes, config: TransportConfig, services: tuple[str, ...],
                   parsed: dict) -> DeploymentReceipt:
    try:
        if (type(raw) is not bytes or not raw or len(raw) > MAX_RECEIPT_BYTES
                or not raw.endswith(b"\n") or raw.count(b"\n") != 1):
            raise ValueError
        value = json.loads(raw[:-1].decode("ascii"), object_pairs_hook=_parse_pairs,
                           parse_constant=_reject_constant)
        if type(value) is not dict or not (type(value.get("version")) is int
                                           and value["version"] == target_entry._VERSION):
            raise ValueError
        if (json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("ascii") != raw:
            raise ValueError
        common = {"version", "correlation", "status", "effect", "source_repository", "source_sha",
                  "run_id", "attempt", "environment", "project", "output_kind"}
        ident, target = config.expected_identity, config.target
        expected_common = {
            "version": target_entry._VERSION, "correlation": config.correlation,
            "source_repository": ident.repository, "source_sha": ident.sha,
            "run_id": ident.run_id, "attempt": ident.attempt,
            "environment": target.environment, "project": target.project_name,
            "output_kind": config.output_kind,
        }
        for key, expected in expected_common.items():
            if type(value.get(key)) is not type(expected) or value.get(key) != expected:
                raise ValueError
        if value.get("status") == "succeeded" and value.get("effect") == "verified":
            if config.output_kind == "image":
                expected_keys = common | {"health", "image", "services", "migrated"}
                image = parsed["output"]["image"]
                if (set(value) != expected_keys or value.get("health") != "healthy"
                        or value.get("image") != image or type(value.get("image")) is not str
                        or type(value.get("services")) is not list
                        or tuple(value["services"]) != services
                        or any(type(item) is not str for item in value["services"])
                        or type(value.get("migrated")) is not bool
                        or value["migrated"] != (target.migrate_service is not None)):
                    raise ValueError
                return DeploymentReceipt("succeeded", "verified", config.correlation,
                    ident.repository, ident.sha, ident.run_id, ident.attempt,
                    target.environment, target.project_name, config.output_kind, image=image,
                    services=services, migrated=value["migrated"])
            expected_keys = common | {"health", "artifact_id", "artifact_sha256", "image", "services"}
            digest = parsed["output"]["sha256"]
            if (set(value) != expected_keys or value.get("health") != "healthy"
                    or type(value.get("artifact_id")) is not int
                    or value["artifact_id"] != config.selected_artifact.artifact_id
                    or value.get("artifact_sha256") != digest
                    or type(value.get("artifact_sha256")) is not str
                    or value.get("image") != target.static_image
                    or type(value.get("image")) is not str
                    or type(value.get("services")) is not list
                    or tuple(value["services"]) != services
                    or any(type(item) is not str for item in value["services"])):
                raise ValueError
            return DeploymentReceipt("succeeded", "verified", config.correlation,
                ident.repository, ident.sha, ident.run_id, ident.attempt,
                target.environment, target.project_name, config.output_kind,
                image=target.static_image, services=services,
                artifact_id=config.selected_artifact.artifact_id, artifact_sha256=digest)
        expected_keys = common | {"error_code", "recovery_required"}
        if (set(value) != expected_keys or value.get("status") != "failed"
                or value.get("effect") != "none" or type(value.get("error_code")) is not str
                or value["error_code"] not in _REFUSAL_CODES
                or type(value.get("recovery_required")) is not bool
                or value["recovery_required"]):
            raise ValueError
        return DeploymentReceipt("refused", "none", config.correlation,
            ident.repository, ident.sha, ident.run_id, ident.attempt,
            target.environment, target.project_name, config.output_kind,
            error_code="target_refused")
    except BaseException:
        raise TransportError("receipt_invalid", ambiguous=True) from None


def _same_snapshots(config: TransportConfig, before: tuple[_FileSnapshot, ...]) -> None:
    after = _validate_provider_files(config)
    if after != before:
        raise TransportError("provider_file_changed") from None


def _close_pipe(pipe: object) -> None:
    try:
        if pipe is not None:
            pipe.close()
    except BaseException:
        pass


def _reap(process: object) -> bool:
    """Best-effort bounded child cleanup; never signal an unrelated process."""
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=CLEANUP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=CLEANUP_GRACE_SECONDS)
        else:
            process.wait(timeout=CLEANUP_GRACE_SECONDS)
        return process.poll() is not None
    except BaseException:
        return False


def _run_process(argv: tuple[str, ...], payload: bytes, config: TransportConfig,
                  snapshots: tuple[_FileSnapshot, ...], timer: _Monotonic,
                  process_factory: Callable[..., object]) -> bytes:
    try:
        _same_snapshots(config, snapshots)
        timer.remaining()
        process = process_factory(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            shell=False, close_fds=True, bufsize=0, env=_child_environment(),
        )
    except TransportError:
        raise
    except BaseException:
        raise TransportError("ssh_start_failed") from None
    if process.stdin is None or process.stdout is None:
        _close_pipe(process.stdin)
        _close_pipe(process.stdout)
        if not _reap(process):
            raise TransportError("ssh_cleanup_failed", ambiguous=True) from None
        raise TransportError("ssh_pipe_unavailable", ambiguous=True) from None
    sent = 0
    try:
        stdin_fd, stdout_fd = process.stdin.fileno(), process.stdout.fileno()
        os.set_blocking(stdin_fd, False)
        os.set_blocking(stdout_fd, False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "read")
            selector.register(process.stdin, selectors.EVENT_WRITE, "write")
            received = bytearray()
            eof = False
            while True:
                remaining = timer.remaining()
                if sent >= len(payload) and process.stdin is not None:
                    selector.unregister(process.stdin)
                    _close_pipe(process.stdin)
                    process.stdin = None
                if eof and sent == len(payload):
                    try:
                        returncode = process.poll()
                    except BaseException:
                        raise TransportError("ssh_completion_invalid", ambiguous=sent > 0) from None
                    if returncode is not None:
                        if type(returncode) is not int or returncode != 0:
                            raise TransportError("ssh_failed", ambiguous=sent > 0) from None
                        waited = process.wait(timeout=timer.remaining())
                        if type(waited) is not int or waited != 0:
                            raise TransportError("ssh_completion_invalid", ambiguous=True) from None
                        timer.remaining()
                        return bytes(received)
                events = selector.select(min(remaining, 0.1))
                if not events and process.poll() is not None and not eof:
                    # Continue draining the pipe; EOF is required for success.
                    pass
                for key, _ in events:
                    if key.data == "write":
                        chunk = memoryview(payload)[sent:sent + IO_CHUNK_BYTES]
                        try:
                            count = os.write(stdin_fd, chunk)
                        except BlockingIOError:
                            continue
                        except BaseException:
                            raise TransportError("ssh_stream_write_failed", ambiguous=sent > 0) from None
                        if type(count) is not int or count <= 0 or count > len(chunk):
                            raise TransportError("ssh_stream_write_invalid", ambiguous=sent > 0) from None
                        sent += count
                    else:
                        try:
                            part = os.read(stdout_fd, min(IO_CHUNK_BYTES, MAX_RECEIPT_BYTES + 1 - len(received)))
                        except BlockingIOError:
                            continue
                        except BaseException:
                            raise TransportError("ssh_stream_read_failed", ambiguous=sent > 0) from None
                        if not part:
                            eof = True
                            selector.unregister(process.stdout)
                            _close_pipe(process.stdout)
                            process.stdout = None
                        else:
                            received.extend(part)
                            if len(received) > MAX_RECEIPT_BYTES:
                                raise TransportError("receipt_too_large", ambiguous=sent > 0) from None
            # unreachable
    except TransportError as exc:
        _close_pipe(process.stdin)
        _close_pipe(process.stdout)
        if not _reap(process):
            raise TransportError("ssh_cleanup_failed", ambiguous=True) from None
        if not exc.ambiguous:
            raise TransportError(exc.code, ambiguous=True) from None
        raise
    except BaseException:
        _close_pipe(process.stdin)
        _close_pipe(process.stdout)
        if not _reap(process):
            raise TransportError("ssh_cleanup_failed", ambiguous=True) from None
        raise TransportError("ssh_io_failed", ambiguous=True) from None
    finally:
        _close_pipe(process.stdin)
        _close_pipe(process.stdout)


def deploy(config: TransportConfig, manifest: bytes, bundle: bytes = b"", *,
           timeout_seconds: float = MAX_EXECUTION_SECONDS,
           clock: Callable[[], float] = time.monotonic,
           process_factory: Callable[..., object] = subprocess.Popen) -> DeploymentReceipt:
    """Validate a request, stream one frame to one fixed SSH launcher, and parse its receipt.

    ``process_factory`` is a trusted installation/test seam. It receives the
    exact argv and normal ``Popen`` stream options; request data cannot replace
    the factory or return a success value.
    """
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise TransportError("deadline_invalid") from None
    try:
        budget = float(timeout_seconds)
    except (ValueError, OverflowError):
        raise TransportError("deadline_invalid") from None
    if not math.isfinite(budget) or not 0 < budget <= MAX_EXECUTION_SECONDS:
        raise TransportError("deadline_invalid") from None
    timer = _Monotonic(clock, min(budget, MAX_EXECUTION_SECONDS))
    c, snapshots, services = _validate_config(config)
    payload, parsed = _frame(c, manifest, bundle)
    # Do not enter SSH after validation consumed the caller's finite budget.
    timer.remaining()
    try:
        stdout = _run_process(_ssh_argv(c), payload, c, snapshots, timer, process_factory)
    except TransportError:
        raise
    except BaseException:
        raise TransportError("transport_failed", ambiguous=True) from None
    receipt = _parse_receipt(stdout, c, services, parsed)
    try:
        timer.remaining()
    except TransportError as exc:
        raise TransportError(exc.code, ambiguous=True) from None
    return receipt
