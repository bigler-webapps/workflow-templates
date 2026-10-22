"""Strict, target-side execution of an already-built immutable image release.

This module is intentionally inert on import. It has no SSH, network, build,
credential acquisition, source checkout, or caller-selected Docker endpoint. A future
trusted caller must run it on the application target after authorization and
must supply all routing and scoped credential paths independently.
"""

from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from typing import Callable, Mapping, Protocol

import release_manifest


MAX_CONTROL_BYTES = 256 * 1024
MAX_METADATA_BYTES = 1024 * 1024
MAX_COMMAND_SECONDS = 300.0
MAX_DEPLOY_SECONDS = 1800.0
MAX_SERVICES = 32
COMPOSE_PARSER_VERSION = "6.0.3"
_PROJECT = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}\Z")
_ENVIRONMENTS = {"staging": "refs/heads/develop", "production": "refs/heads/main"}
_SERVICE = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_NAME_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_REGISTRY_LABEL = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
_IMAGE = re.compile(_REGISTRY_LABEL + r"(?:\." + _REGISTRY_LABEL + r")*(?::[1-9][0-9]{0,4})?/"
                    + _NAME_COMPONENT + r"(?:/" + _NAME_COMPONENT + r")*@sha256:[0-9a-f]{64}\Z")
_CONTAINER_ID = re.compile(r"[0-9a-f]{12,64}\Z")
_SAFE_COMPOSE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.ya?ml\Z")
_ENV_KEYS = {"PATH", "HOME", "LANG", "LC_ALL", "DOCKER_CONFIG"}
_DOCKER = ("docker", "--host", "unix:///var/run/docker.sock")
_PROCESS_READER_GATE = threading.Lock()
_INSPECT_TEMPLATE = (
    "{{json .Config.Image}}\n{{json .State.Status}}\n"
    "{{json .State.Restarting}}\n{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}}\n"
    '{{json (index .Config.Labels "com.docker.compose.project")}}\n'
    '{{json (index .Config.Labels "com.docker.compose.service")}}\n'
)


class PrebuiltTargetError(RuntimeError):
    """A fixed, non-sensitive execution failure code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ComposeControl:
    filename: str
    sha256: str


@dataclass(frozen=True)
class TargetSpec:
    """Trusted target capabilities; no field is read from a release artifact.

    ``application_dir`` is an existing target project directory. ``compose``
    lists its reviewed non-secret YAML controls and externally bound SHA-256s.
    ``image_services`` receive the selected manifest image. ``sidecar_images``
    maps all other expected services to exact digest-pinned references. The
    runtime env file is provider-created and only stat-checked; this API never
    opens it. Docker config and temporary parent are existing private dirs.
    """

    project_name: str
    environment: str
    application_dir: str
    compose: tuple[ComposeControl, ...]
    image_services: tuple[str, ...]
    sidecar_images: tuple[tuple[str, str], ...]
    runtime_env_path: str
    docker_config_dir: str
    temporary_parent: str
    migrate_service: str | None = None


@dataclass(frozen=True)
class DeploymentResult:
    """Safe immutable evidence returned only after observed container checks."""

    image: str
    project_name: str
    environment: str
    source_repository: str
    source_sha: str
    run_id: int
    attempt: int
    services: tuple[str, ...]
    migrated: bool


@dataclass(frozen=True)
class _CommandResult:
    returncode: int
    stdout: bytes


class _Executor(Protocol):
    def run(self, argv: tuple[str, ...], *, cwd: str, env: Mapping[str, str],
            timeout: float, capture: bool = False) -> _CommandResult: ...


def _fail(code: str) -> None:
    raise PrebuiltTargetError(code) from None


def _plain_text(value: object, pattern: re.Pattern[str]) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


def _valid_pinned_image(value: object) -> bool:
    """Shared canonical image/namespace/port boundary for target policy."""
    if not _plain_text(value, _IMAGE) or len(value.partition('@')[0]) > 255:
        return False
    registry = value.partition('/')[0]
    return ':' not in registry or int(registry.rsplit(':', 1)[1]) <= 65535


def _absolute_path(value: object) -> Path:
    if type(value) is not str or not value or len(value) > 4096:
        _fail("target_path_invalid")
    if any(ord(char) < 0x20 or ord(char) == 0x7f for char in value) or any(char in value for char in "\r\n;&|`$<>\"'"):
        _fail("target_path_invalid")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or value != os.path.normpath(value):
        _fail("target_path_invalid")
    return path


def _validate_target(target: object, expected: object) -> tuple[Path, Path, Path, Path, tuple[tuple[str, str], ...]]:
    if type(target) is not TargetSpec:
        _fail("target_spec_invalid")
    if not _plain_text(target.project_name, _PROJECT):
        _fail("project_invalid")
    if type(target.environment) is not str or target.environment not in _ENVIRONMENTS:
        _fail("environment_invalid")
    if type(expected) is not release_manifest.ReleaseIdentity:
        _fail("expected_identity_invalid")
    try:
        release_manifest._validate_identity(expected)
    except release_manifest.ManifestError:
        _fail("expected_identity_invalid")
    if (len(expected.repository) > 256 or len(expected.workflow) > 256
            or not 0 < expected.run_id <= 2**63-1 or not 0 < expected.attempt <= 2**31-1):
        _fail("expected_identity_invalid")
    if expected.ref != _ENVIRONMENTS[target.environment]:
        _fail("environment_ref_mismatch")
    app = _absolute_path(target.application_dir)
    runtime = _absolute_path(target.runtime_env_path)
    docker_config = _absolute_path(target.docker_config_dir)
    temp_parent = _absolute_path(target.temporary_parent)
    if any(path == app or app in path.parents for path in (runtime, docker_config, temp_parent)):
        _fail("runtime_scope_invalid")
    if runtime == docker_config / "config.json":
        _fail("runtime_scope_invalid")
    if type(target.compose) is not tuple or not 1 <= len(target.compose) <= 8:
        _fail("compose_controls_invalid")
    names: list[str] = []
    for control in target.compose:
        if type(control) is not ComposeControl or not _plain_text(control.filename, _SAFE_COMPOSE):
            _fail("compose_controls_invalid")
        if control.filename in (".", "..") or "/" in control.filename or "\\" in control.filename:
            _fail("compose_controls_invalid")
        if not _plain_text(control.sha256, _HASH):
            _fail("compose_hash_invalid")
        names.append(control.filename)
    if len(set(names)) != len(names):
        _fail("compose_controls_duplicate")
    if type(target.image_services) is not tuple or not 1 <= len(target.image_services) <= MAX_SERVICES:
        _fail("image_services_invalid")
    if any(not _plain_text(name, _SERVICE) for name in target.image_services):
        _fail("image_services_invalid")
    if len(set(target.image_services)) != len(target.image_services):
        _fail("service_duplicate")
    if type(target.sidecar_images) is not tuple or len(target.sidecar_images) > MAX_SERVICES:
        _fail("sidecar_services_invalid")
    sidecars: list[tuple[str, str]] = []
    for item in target.sidecar_images:
        if type(item) is not tuple or len(item) != 2:
            _fail("sidecar_services_invalid")
        service, image = item
        if not _plain_text(service, _SERVICE) or not _valid_pinned_image(image):
            _fail("sidecar_image_invalid")
        sidecars.append((service, image))
    sidecar_names = [service for service, _ in sidecars]
    if len(set(sidecar_names)) != len(sidecar_names) or set(sidecar_names) & set(target.image_services):
        _fail("service_conflict")
    if not target.image_services and not sidecars:
        _fail("service_shape_empty")
    if len(target.image_services) + len(sidecars) > MAX_SERVICES:
        _fail("service_count_invalid")
    if target.migrate_service is not None and (
        type(target.migrate_service) is not str or target.migrate_service not in target.image_services
    ):
        _fail("migrate_service_invalid")
    expected_services = tuple(sorted((*target.image_services, *sidecar_names)))
    if not expected_services:
        _fail("service_shape_empty")
    return app, runtime, docker_config, temp_parent, tuple(sorted(sidecars))


def _identity(path: Path, *, kind: str, required_mode: int | None = None) -> tuple[int, int, int, int, int, int]:
    try:
        info = path.lstat()
    except Exception:
        _fail("target_path_missing")
    if stat.S_ISLNK(info.st_mode):
        _fail("target_symlink_rejected")
    if kind == "dir" and not stat.S_ISDIR(info.st_mode):
        _fail("target_directory_invalid")
    if kind == "file" and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
        _fail("target_file_invalid")
    if os.name != "nt":
        if info.st_uid != os.geteuid():
            _fail("target_owner_invalid")
        if stat.S_IMODE(info.st_mode) & 0o022:
            _fail("target_mode_invalid")
        if required_mode is not None and stat.S_IMODE(info.st_mode) != required_mode:
            _fail("target_mode_invalid")
    # Creating our own child changes directory size/mtime, not its identity.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid,
            info.st_size if kind == "file" else 0, info.st_mtime_ns if kind == "file" else 0)


def _no_symlink_ancestors(path: Path) -> None:
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor = cursor / part
        try:
            info = cursor.lstat()
        except Exception:
            _fail("target_path_missing")
        if stat.S_ISLNK(info.st_mode):
            _fail("target_symlink_rejected")


def _read_control(path: Path, expected_hash: str) -> tuple[tuple[int, int, int, int, int, int], bytes]:
    before = _identity(path, kind="file")
    if before[4] > MAX_CONTROL_BYTES:
        _fail("compose_control_too_large")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_uid,
                    opened.st_size, opened.st_mtime_ns) != before:
                _fail("compose_control_changed")
            payload = stream.read(MAX_CONTROL_BYTES + 1)
    except PrebuiltTargetError:
        raise
    except Exception:
        _fail("compose_control_unreadable")
    after = _identity(path, kind="file")
    if before != after or len(payload) != before[4] or hashlib.sha256(payload).hexdigest() != expected_hash:
        _fail("compose_control_changed")
    return before, payload


def _private_inputs(target: TargetSpec, app: Path, runtime: Path, docker_config: Path,
                    temp_parent: Path) -> tuple[dict[Path, tuple[int, int, int, int, int, int]], dict[Path, bytes]]:
    for directory in (app, docker_config, temp_parent, runtime.parent):
        _no_symlink_ancestors(directory)
    snapshots: dict[Path, tuple[int, int, int, int, int, int]] = {}
    for directory in (app, docker_config, temp_parent, runtime.parent):
        snapshots[directory] = _identity(directory, kind="dir")
    # Runtime values are deliberately never opened or read.
    snapshots[runtime] = _identity(runtime, kind="file", required_mode=0o600)
    _identity(docker_config, kind="dir", required_mode=0o700)
    _identity(temp_parent, kind="dir", required_mode=0o700)
    _identity(runtime.parent, kind="dir", required_mode=0o700)
    # This is an explicit, provider-created registry profile. An empty Docker
    # config can auto-detect a host keychain; no helpers/plugins are accepted.
    controls: dict[Path, bytes] = {}
    for control in target.compose:
        path = app / control.filename
        _no_symlink_ancestors(path)
        snapshot, payload = _read_control(path, control.sha256)
        _validate_compose_control(payload)
        snapshots[path] = snapshot
        controls[path] = payload
    snapshots[docker_config / "config.json"] = _docker_profile(docker_config)
    return snapshots, controls


def _validate_compose_control(raw: bytes):
    """Parse only reviewed controls; never resolve YAML/Compose file references."""
    try:
        import yaml
        if yaml.__version__ != COMPOSE_PARSER_VERSION:
            _fail("compose_parser_unavailable")
    except ImportError:
        _fail("compose_parser_unavailable")
    try:
        text = raw.decode("utf-8", errors="strict")
        depth = 0
        events = yaml.parse(text, Loader=yaml.SafeLoader)
        try:
            for count, event in enumerate(events, 1):
                if (count > 4096 or isinstance(event, yaml.AliasEvent)
                        or getattr(event, "anchor", None) is not None or getattr(event, "tag", None) is not None):
                    _fail("compose_control_invalid")
                if isinstance(event, (yaml.MappingStartEvent,yaml.SequenceStartEvent)):
                    depth += 1
                    if depth > 64: _fail("compose_control_invalid")
                elif isinstance(event, (yaml.MappingEndEvent,yaml.SequenceEndEvent)):
                    depth -= 1
        finally:
            events.close()
        root = yaml.compose(text, Loader=yaml.SafeLoader)
        def mapping(node):
            if not isinstance(node,yaml.MappingNode): _fail("compose_control_invalid")
            result = {}
            for key,value in node.value:
                if (not isinstance(key,yaml.ScalarNode) or key.tag != "tag:yaml.org,2002:str" or key.value in result):
                    _fail("compose_control_invalid")
                result[key.value] = value
            return result
        # Check duplicate/non-string keys throughout the document, without
        # constructing tagged Python values or invoking any YAML constructor.
        pending = [root]
        while pending:
            node = pending.pop()
            if isinstance(node,yaml.MappingNode): pending.extend(mapping(node).values())
            elif isinstance(node,yaml.SequenceNode): pending.extend(node.value)
        top = mapping(root)
        if "include" in top: _fail("compose_external_reference")
        if "services" in top:
            for service in mapping(top["services"]).values():
                if set(mapping(service)) & {"extends","env_file","label_file"}:
                    _fail("compose_external_reference")
        for kind in ("configs","secrets"):
            if kind in top:
                for declaration in mapping(top[kind]).values():
                    if "file" in mapping(declaration): _fail("compose_external_reference")
    except PrebuiltTargetError:
        raise
    except BaseException:
        _fail("compose_control_invalid")


def _docker_profile(directory: Path):
    try:
        with os.scandir(directory) as entries:
            names = []
            for entry in entries:
                names.append(entry.name)
                if len(names) > 1:
                    _fail("docker_profile_invalid")
        if names != ["config.json"]:
            _fail("docker_profile_invalid")
        path = directory / "config.json"
        before = _identity(path, kind="file", required_mode=0o600)
        if before[4] > 16384:
            _fail("docker_profile_invalid")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_size, info.st_mtime_ns) != before:
                _fail("docker_profile_invalid")
            raw = stream.read(16385)
        if _identity(path, kind="file", required_mode=0o600) != before or len(raw) != before[4]:
            _fail("docker_profile_invalid")
        value = _safe_json(raw, max_bytes=16384)
        if type(value) is not dict or set(value) != {"auths"}:
            _fail("docker_profile_invalid")
        auths = value["auths"]
        if type(auths) is not dict or set(auths) != {"ghcr.io"}:
            _fail("docker_profile_invalid")
        entry = auths["ghcr.io"]
        if type(entry) is not dict or set(entry) not in (set(), {"auth"}):
            _fail("docker_profile_invalid")
        if entry:
            credential = entry["auth"]
            if type(credential) is not str or not 1 <= len(credential) <= 4096:
                _fail("docker_profile_invalid")
            decoded = base64.b64decode(credential, validate=True)
            username, separator, token = decoded.partition(b":")
            if not separator or not username or not token or any(c < 32 or c == 127 for c in decoded):
                _fail("docker_profile_invalid")
        # Return metadata only; never hash, return or log credential contents.
        return before
    except BaseException:
        _fail("docker_profile_invalid")


def _check_snapshots(snapshots: Mapping[Path, tuple[int, int, int, int, int, int]]) -> None:
    for path, expected in snapshots.items():
        kind = "dir" if path.is_dir() else "file"
        if _identity(path, kind=kind) != expected:
            _fail("target_path_replaced")


def _safe_json(raw: bytes, *, max_bytes: int = MAX_METADATA_BYTES) -> object:
    if type(raw) is not bytes or len(raw) > max_bytes:
        _fail("command_metadata_invalid")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _fail("command_metadata_duplicate")
            result[key] = value
        return result
    try:
        return json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=pairs,
                          parse_constant=lambda _: _fail("command_metadata_invalid"))
    except PrebuiltTargetError:
        raise
    except Exception:
        _fail("command_metadata_invalid")


class _SubprocessExecutor:
    """Fixed argv adapter with suppressed diagnostics and bounded metadata."""

    def run(self, argv: tuple[str, ...], *, cwd: str, env: Mapping[str, str],
            timeout: float, capture: bool = False) -> _CommandResult:
        if type(argv) is not tuple or not argv or any(type(part) is not str for part in argv):
            _fail("command_invalid")
        if not _finite_number(timeout) or timeout <= 0:
            _fail("deadline_exceeded")
        timeout = min(MAX_COMMAND_SECONDS, float(timeout))
        process = None
        chunks: list[bytes] = []
        total = 0
        overflow = threading.Event()
        reader = None
        reader_started = False
        reader_reserved = False
        end = time.monotonic() + timeout
        try:
            if capture:
                if not _PROCESS_READER_GATE.acquire(blocking=False):
                    _fail("command_reader_busy")
                reader_reserved = True
            process = subprocess.Popen(
                argv, cwd=cwd, env=dict(env), shell=False, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True, bufsize=0,
            )
            if capture:
                def drain():
                    nonlocal total
                    try:
                        while True:
                            chunk = process.stdout.read(8192)
                            if not chunk:
                                return
                            total += len(chunk)
                            if total > MAX_METADATA_BYTES:
                                overflow.set()
                                return
                            chunks.append(chunk)
                    except BaseException:
                        overflow.set()
                    finally:
                        _PROCESS_READER_GATE.release()
                reader = threading.Thread(target=drain, daemon=True)
                reader.start()
                reader_started = True
            while process.poll() is None:
                if overflow.is_set():
                    process.kill()
                    try:
                        process.wait(timeout=max(0.0, min(1.0, end-time.monotonic())))
                    except subprocess.TimeoutExpired:
                        pass
                    _fail("command_metadata_too_large")
                if time.monotonic() >= end:
                    process.kill()
                    try:
                        process.wait(timeout=0)
                    except subprocess.TimeoutExpired:
                        pass
                    _fail("command_timeout")
                time.sleep(min(0.02, max(0.001, end - time.monotonic())))
            process.wait(timeout=max(0.0, end-time.monotonic()))
            if reader is not None:
                reader.join(timeout=max(0.0, min(.5, end-time.monotonic())))
            if overflow.is_set() or total > MAX_METADATA_BYTES or (reader and reader.is_alive()):
                _fail("command_metadata_too_large")
            if time.monotonic() >= end:
                _fail("command_timeout")
            return _CommandResult(process.returncode, b"".join(chunks))
        except PrebuiltTargetError:
            raise
        except BaseException:
            _fail("command_failed")
        finally:
            if reader_reserved and not reader_started:
                _PROCESS_READER_GATE.release()
            if process is not None:
                try:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=1)
                except BaseException:
                    pass
                for handle in (getattr(process, "stdout", None), getattr(process, "stderr", None), getattr(process, "stdin", None)):
                    if handle is not None:
                        try:
                            handle.close()
                        except BaseException:
                            pass
            if reader_started and reader is not None:
                reader.join(timeout=.05)


def _decode_ps(raw: bytes, services: set[str], project: str) -> dict[str, tuple[str, str, str]]:
    if type(raw) is not bytes or len(raw) > MAX_METADATA_BYTES:
        _fail("compose_ps_invalid")
    try:
        text = raw.decode("utf-8", errors="strict")
    except Exception:
        _fail("compose_ps_invalid")
    if text.lstrip().startswith("["):
        rows = _safe_json(raw)
    else:
        # Compose v2 emits one JSON object per line. Mixed forms and duplicate
        # keys are rejected; the final newline is part of the accepted shape.
        if not text or not text.endswith("\n"):
            _fail("compose_ps_invalid")
        rows = []
        for line in text.splitlines():
            if not line:
                _fail("compose_ps_invalid")
            row = _safe_json(line.encode("utf-8"))
            if type(row) is not dict:
                _fail("compose_ps_invalid")
            rows.append(row)
    if type(rows) is not list or not 1 <= len(rows) <= MAX_SERVICES:
        _fail("compose_ps_invalid")
    seen: dict[str, tuple[str, str, str]] = {}
    allowed = {"ID", "Name", "Command", "Project", "Service", "State", "Health", "ExitCode", "Publishers"}
    for row in rows:
        if type(row) is not dict or set(row) - allowed:
            _fail("compose_ps_invalid")
        cid, service, state, health = (row.get("ID"), row.get("Service"), row.get("State"), row.get("Health"))
        if (type(cid) is not str or not _CONTAINER_ID.fullmatch(cid) or type(service) is not str
                or service not in services or row.get("Project") != project):
            _fail("compose_ps_identity_invalid")
        if (state != "running" or health not in ("", None, "healthy")
                or ("ExitCode" in row and (type(row["ExitCode"]) is not int or row["ExitCode"] != 0))):
            _fail("compose_ps_state_invalid")
        if service in seen or cid in {value[0] for value in seen.values()}:
            _fail("compose_ps_duplicate_service")
        seen[service] = (cid, state, health or "")
    if set(seen) != services:
        _fail("compose_ps_coverage_invalid")
    return seen


def _finite_number(value):
    return (type(value) in (int, float)
            and (type(value) is not int or -(2**63) <= value <= 2**63-1)
            and math.isfinite(value))


def _now(clock):
    try:
        value = clock()
    except BaseException:
        _fail("clock_failed")
    if not _finite_number(value):
        _fail("clock_invalid")
    return float(value)


def execute_image_deployment(
    raw_manifest: bytes,
    *,
    expected_identity: release_manifest.ReleaseIdentity,
    expected_image_namespace: str,
    resolve_pushed_image: Callable[[str, str], Mapping[str, object]],
    target: TargetSpec,
    executor: _Executor | None = None,
    clock: Callable[[], float] = time.monotonic,
    deadline: float | None = None,
) -> DeploymentResult:
    """Validate, pull and activate one immutable image using target Docker.

    The caller owns WM-SEC-17 authorization, source/artifact retrieval and the
    scoped runtime-env provider. This function runs only on the app target.
    """
    app, runtime, docker_config, temp_parent, sidecars = _validate_target(target, expected_identity)
    if type(raw_manifest) is not bytes or len(raw_manifest) > 16384:
        _fail("manifest_bytes_required")
    if (type(expected_image_namespace) is not str or len(expected_image_namespace) > 256
            or not release_manifest._IMAGE_NAMESPACE.fullmatch(expected_image_namespace)):
        _fail("image_namespace_invalid")
    if not callable(resolve_pushed_image) or not callable(clock):
        _fail("executor_invalid")
    if executor is not None and not callable(getattr(executor, "run", None)):
        _fail("executor_invalid")
    start = _now(clock)
    if deadline is None:
        whole_deadline = float(start) + MAX_DEPLOY_SECONDS
    else:
        if not _finite_number(deadline) or deadline <= start:
            _fail("deadline_invalid")
        whole_deadline = min(float(deadline), float(start) + MAX_DEPLOY_SECONDS)
    try:
        preflight = release_manifest.validate_manifest_structure(raw_manifest,
            expected=expected_identity, expected_image_namespace=expected_image_namespace)
        if preflight["kind"] != "image":
            _fail("image_manifest_required")
    except release_manifest.ManifestError:
        _fail("manifest_invalid")
    if _now(clock) >= whole_deadline:
        _fail("deadline_exceeded")
    snapshots, _ = _private_inputs(target, app, runtime, docker_config, temp_parent)
    _check_snapshots(snapshots)
    def bounded_resolver(namespace, tag):
        if _now(clock) >= whole_deadline:
            _fail("deadline_exceeded")
        value = resolve_pushed_image(namespace, tag)
        if _now(clock) >= whole_deadline:
            _fail("deadline_exceeded")
        return value
    try:
        if _now(clock) >= whole_deadline:
            _fail("deadline_exceeded")
        manifest = release_manifest.validate_manifest(
            raw_manifest, expected=expected_identity,
            expected_image_namespace=expected_image_namespace,
            resolve_pushed_image=bounded_resolver,
        )
    except release_manifest.ManifestError:
        _fail("manifest_invalid")
    except PrebuiltTargetError:
        raise
    except BaseException:
        _fail("manifest_validation_failed")
    try:
        if _now(clock) >= whole_deadline:
            _fail("deadline_exceeded")
    except PrebuiltTargetError:
        raise
    except BaseException:
        _fail("clock_failed")
    if manifest.get("kind") != "image":
        _fail("image_manifest_required")
    image = manifest["output"]["image"]
    if expected_identity.ref != _ENVIRONMENTS[target.environment]:
        _fail("environment_ref_mismatch")
    output_dir: Path | None = None
    output_identity = None
    failure: PrebuiltTargetError | None = None
    deployment_result: DeploymentResult | None = None
    try:
        if _now(clock) >= whole_deadline:
            _fail("deadline_exceeded")
        output_dir = Path(tempfile.mkdtemp(prefix="prebuilt-target-", dir=temp_parent))
        output_identity = _identity(output_dir, kind="dir", required_mode=0o700)
        override = output_dir / "override.yaml"
        # !reset removes a base build mapping; plain null would inherit it.
        override_data = ("services:\n" + "".join(
            f"  {service}:\n    image: {json.dumps(image)}\n    build: !reset null\n"
            f"    env_file: [{json.dumps(str(runtime))}]\n"
            for service in target.image_services)).encode("ascii")
        fd = os.open(override, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(override_data)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                os.close(fd)
            except BaseException:
                pass
            raise
        override_identity = _identity(override, kind="file")
        snapshots[output_dir] = output_identity
        snapshots[override] = override_identity
        compose_prefix = [*_DOCKER, "compose", "--project-name", target.project_name,
                          "--project-directory", str(app)]
        for control in target.compose:
            compose_prefix.extend(("--file", control.filename))
        compose_prefix.extend(("--file", str(override)))
        compose_prefix.extend(("--env-file", str(runtime)))
        command_prefix = tuple(compose_prefix)
        child_env = {"PATH": os.defpath, "HOME": str(Path.home()), "LANG": "C.UTF-8",
                     "LC_ALL": "C.UTF-8", "DOCKER_CONFIG": str(docker_config)}
        selected_executor = executor if executor is not None else _SubprocessExecutor()
        run = lambda args, capture=False: _run(
            selected_executor, args, cwd=str(app), env=child_env,
            timeout=whole_deadline - _now(clock), capture=capture,
            deadline=whole_deadline, clock=clock, snapshots=snapshots,
        )
        config_raw = run(command_prefix + ("config", "--format", "json"), capture=True)
        config = _safe_json(config_raw)
        if type(config) is not dict or type(config.get("services")) is not dict:
            _fail("compose_config_invalid")
        service_config = config["services"]
        expected_services = set(target.image_services) | {name for name, _ in sidecars}
        if set(service_config) != expected_services:
            _fail("compose_services_mismatch")
        sidecar_map = dict(sidecars)
        health_required = set()
        for service, rendered in service_config.items():
            if type(rendered) is not dict or "build" in rendered:
                _fail("compose_build_rejected")
            wanted = image if service in target.image_services else sidecar_map[service]
            if rendered.get("image") != wanted:
                _fail("compose_image_mismatch")
            healthcheck = rendered.get("healthcheck")
            if healthcheck is not None:
                if (type(healthcheck) is not dict or ("disable" in healthcheck
                        and type(healthcheck["disable"]) is not bool)):
                    _fail("compose_healthcheck_invalid")
                if healthcheck.get("disable") is not True and healthcheck.get("test") != ["NONE"]:
                    health_required.add(service)
        run(command_prefix + ("pull",))
        run(command_prefix + ("up", "--detach", "--no-build", "--pull", "never",
                              "--force-recreate", "--remove-orphans"))
        migrated = target.migrate_service is not None
        if migrated:
            run(command_prefix + ("exec", "-T", target.migrate_service, "python", "manage.py",
                                  "migrate", "--noinput"))
        ps_raw = run(command_prefix + ("ps", "--all", "--format", "json"), capture=True)
        observed = _decode_ps(ps_raw, expected_services, target.project_name)
        for service in sorted(expected_services):
            cid, _, _ = observed[service]
            inspect_raw = _run(
                selected_executor,
                _DOCKER + ("inspect", "--format", _INSPECT_TEMPLATE, cid),
                cwd=str(app), env={"PATH": os.defpath, "HOME": str(Path.home()), "LANG": "C.UTF-8",
                                   "LC_ALL": "C.UTF-8", "DOCKER_CONFIG": str(docker_config)},
                timeout=min(MAX_COMMAND_SECONDS, whole_deadline - _now(clock)), capture=True,
                deadline=whole_deadline, clock=clock, snapshots=snapshots,
            )
            fields = inspect_raw.decode("utf-8", errors="strict").splitlines()
            if len(fields) != 6:
                _fail("container_inspect_invalid")
            values = [_safe_json((field + "\n").encode("utf-8")) for field in fields]
            actual_image, state, restarting, health, actual_project, actual_service = values
            wanted = image if service in target.image_services else sidecar_map[service]
            if actual_image != wanted or actual_project != target.project_name or actual_service != service:
                _fail("container_identity_mismatch")
            if (state != "running" or restarting is not False or health not in (None, "healthy")
                    or (service in health_required and health != "healthy")):
                _fail("container_unhealthy")
        if _now(clock) >= whole_deadline:
            _fail("deadline_exceeded")
        deployment_result = DeploymentResult(
            image=image, project_name=target.project_name, environment=target.environment,
            source_repository=expected_identity.repository, source_sha=expected_identity.sha,
            run_id=expected_identity.run_id, attempt=expected_identity.attempt,
            services=tuple(sorted(expected_services)), migrated=migrated,
        )
    except PrebuiltTargetError as exc:
        failure = exc
    except BaseException:
        failure = PrebuiltTargetError("execution_failed")
    finally:
        if output_dir is not None:
            try:
                _check_snapshots({temp_parent: snapshots[temp_parent]})
                if output_identity is None:
                    # No payload exists before identity validation. Only remove
                    # the newly allocated empty directory, never recurse here.
                    os.rmdir(output_dir)
                else:
                    current = _identity(output_dir, kind="dir")
                    if current != output_identity:
                        _fail("cleanup_identity_changed")
                    shutil.rmtree(output_dir)
            except PrebuiltTargetError as exc:
                failure = exc
            except BaseException:
                failure = PrebuiltTargetError("cleanup_failed")
    if failure is not None:
        raise PrebuiltTargetError(failure.code) from None
    if deployment_result is None:
        _fail("execution_failed")
    return deployment_result


def _run(executor: _Executor, argv: tuple[str, ...], *, cwd: str, env: Mapping[str, str],
         timeout: float, capture: bool, deadline: float, clock: Callable[[], float],
         snapshots: Mapping[Path, tuple[int, int, int, int, int, int]]) -> bytes:
    now = _now(clock)
    if now >= deadline:
        _fail("deadline_exceeded")
    _check_snapshots(snapshots)
    try:
        result = executor.run(argv, cwd=cwd, env=env, timeout=min(MAX_COMMAND_SECONDS, timeout), capture=capture)
    except PrebuiltTargetError:
        raise
    except BaseException:
        _fail("command_failed")
    after = _now(clock)
    if after >= deadline:
        _fail("deadline_exceeded")
    _check_snapshots(snapshots)
    if type(result) is not _CommandResult or type(result.returncode) is not int or result.returncode != 0:
        _fail("command_failed")
    if type(result.stdout) is not bytes or len(result.stdout) > MAX_METADATA_BYTES:
        _fail("command_metadata_invalid")
    if not capture and result.stdout:
        _fail("command_metadata_invalid")
    return result.stdout
