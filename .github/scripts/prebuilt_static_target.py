"""Strict, target-side execution for immutable static releases.

Importing this module has no effects. A trusted caller supplies the exact
release identity, selected artifact, application routing and pinned runtime
images. Static bytes are prepared by the shared bounded archive preparer and
are retained after activation so a running container never loses its mount.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import time
from typing import Callable

import prebuilt_deploy
import prebuilt_target as target_exec
import release_manifest


MAX_STATIC_DEPLOY_SECONDS = target_exec.MAX_DEPLOY_SECONDS
_DESTINATION = re.compile(r"/[A-Za-z0-9._/-]{1,255}\Z")
_ASSET_ROOT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}\Z")


@dataclass(frozen=True)
class StaticMount:
    """A trusted, reviewed read-only bind mount retained alongside assets."""

    service: str
    source: str
    destination: str
    sha256: str


@dataclass(frozen=True)
class AssetMount:
    """Map one approved archive root to a read-only container directory."""

    archive_root: str
    destination: str


@dataclass(frozen=True)
class StaticTargetSpec:
    """Trusted static target policy; no value is selected by artifact bytes."""

    project_name: str
    environment: str
    application_dir: str
    compose: tuple[target_exec.ComposeControl, ...]
    static_service: str
    static_image: str
    sidecar_images: tuple[tuple[str, str], ...]
    allowed_asset_roots: tuple[str, ...]
    asset_mounts: tuple[AssetMount, ...]
    preserved_mounts: tuple[StaticMount, ...]
    assets_parent: str
    temporary_parent: str
    runtime_env_path: str | None = None


@dataclass(frozen=True)
class StaticDeploymentResult:
    """Safe evidence returned only after observed static service health."""

    project_name: str
    environment: str
    source_repository: str
    source_sha: str
    run_id: int
    attempt: int
    artifact_id: int
    artifact_sha256: str
    static_image: str
    services: tuple[str, ...]
    asset_directory: str


class StaticTargetError(RuntimeError):
    """A fixed non-sensitive static deployment failure code."""

    def __init__(self, code: str, *, retained_asset_directory: str | None = None):
        self.code = code
        self.retained_asset_directory = retained_asset_directory
        super().__init__(code)


def _fail(code: str) -> None:
    raise StaticTargetError(code) from None


def _now(clock):
    try:
        return target_exec._now(clock)
    except BaseException:
        _fail("clock_invalid")


def _valid_root(value: object) -> bool:
    return (type(value) is str and bool(_ASSET_ROOT.fullmatch(value))
            and "\\" not in value and ":" not in value
            and all(part not in ("", ".", "..") for part in value.split("/")))


def _valid_destination(value: object) -> bool:
    return (type(value) is str and bool(_DESTINATION.fullmatch(value))
            and "//" not in value and all(part not in ("", ".", "..") for part in value.split("/")[1:]))


def _valid_image(value: object) -> bool:
    return target_exec._valid_pinned_image(value)


def _absolute(value: object, code: str = "target_path_invalid") -> Path:
    try:
        path = target_exec._absolute_path(value)
        return path
    except target_exec.PrebuiltTargetError:
        _fail(code)


def _validate_spec(spec: object, identity: object, selected: object):
    if type(spec) is not StaticTargetSpec:
        _fail("target_spec_invalid")
    if (not target_exec._plain_text(spec.project_name, target_exec._PROJECT)
            or type(spec.environment) is not str
            or spec.environment not in ("staging", "production")):
        _fail("target_spec_invalid")
    if type(identity) is not release_manifest.ReleaseIdentity:
        _fail("release_identity_invalid")
    try:
        release_manifest._validate_identity(identity)
    except BaseException:
        _fail("release_identity_invalid")
    expected_ref = "refs/heads/develop" if spec.environment == "staging" else "refs/heads/main"
    if identity.ref != expected_ref:
        _fail("environment_ref_mismatch")
    if type(selected) is not release_manifest.StaticArtifactSelection:
        _fail("artifact_selection_invalid")
    for path_value in (spec.application_dir, spec.assets_parent, spec.temporary_parent):
        _absolute(path_value)
    app = Path(spec.application_dir)
    assets_parent = Path(spec.assets_parent)
    temporary_parent = Path(spec.temporary_parent)
    if (assets_parent == app or app in assets_parent.parents or temporary_parent == app
            or app in temporary_parent.parents or assets_parent == temporary_parent):
        _fail("target_path_scope_invalid")
    if spec.runtime_env_path is not None:
        runtime = _absolute(spec.runtime_env_path)
        if runtime == app or app in runtime.parents or runtime == assets_parent or assets_parent in runtime.parents:
            _fail("runtime_input_scope_invalid")
    if type(spec.compose) is not tuple or not 1 <= len(spec.compose) <= 8:
        _fail("compose_controls_invalid")
    if (type(spec.static_service) is not str
            or not target_exec._plain_text(spec.static_service, target_exec._SERVICE)):
        _fail("service_invalid")
    if not _valid_image(spec.static_image):
        _fail("static_image_invalid")
    if type(spec.sidecar_images) is not tuple or len(spec.sidecar_images) > target_exec.MAX_SERVICES - 1:
        _fail("sidecar_images_invalid")
    sidecars = {}
    for pair in spec.sidecar_images:
        if (type(pair) is not tuple or len(pair) != 2 or type(pair[0]) is not str
                or not target_exec._plain_text(pair[0], target_exec._SERVICE)
                or not _valid_image(pair[1])):
            _fail("sidecar_images_invalid")
        if pair[0] == spec.static_service or pair[0] in sidecars:
            _fail("service_conflict")
        sidecars[pair[0]] = pair[1]
    if type(spec.allowed_asset_roots) is not tuple or not spec.allowed_asset_roots:
        _fail("asset_roots_invalid")
    if any(not _valid_root(root) for root in spec.allowed_asset_roots):
        _fail("asset_roots_invalid")
    roots = tuple(spec.allowed_asset_roots)
    if len(set(root.casefold() for root in roots)) != len(roots):
        _fail("asset_roots_invalid")
    if type(spec.asset_mounts) is not tuple or len(spec.asset_mounts) != len(roots):
        _fail("asset_mounts_invalid")
    mounts = []
    for mount in spec.asset_mounts:
        if (type(mount) is not AssetMount or mount.archive_root not in roots
                or not _valid_destination(mount.destination)):
            _fail("asset_mounts_invalid")
        mounts.append(mount)
    if set(m.archive_root for m in mounts) != set(roots):
        _fail("asset_mounts_invalid")
    if type(spec.preserved_mounts) is not tuple or len(spec.preserved_mounts) > 32:
        _fail("preserved_mounts_invalid")
    preserved = []
    destinations = {(spec.static_service, m.destination) for m in mounts}
    for mount in spec.preserved_mounts:
        if (type(mount) is not StaticMount or mount.service not in ({spec.static_service} | set(sidecars))
                or not _valid_destination(mount.destination)
                or not target_exec._plain_text(mount.sha256, target_exec._HASH)):
            _fail("preserved_mounts_invalid")
        source = _absolute(mount.source)
        if (not source.is_relative_to(app)
                or any(part.casefold().startswith('.env') or part.casefold() in
                       {'.ssh', '.secrets', 'secrets.yaml', 'secrets.yml', 'secrets.json', 'id_rsa', 'id_ed25519'}
                       or part.casefold().endswith(('.key', '.pem', '.p12', '.pfx')) for part in source.parts)):
            _fail("preserved_control_scope_invalid")
        if (mount.service, mount.destination) in destinations:
            _fail("mount_destination_conflict")
        destinations.add((mount.service, mount.destination))
        preserved.append((mount.service, str(source), mount.destination))
    if len(destinations) != len(mounts) + len(preserved):
        _fail("mount_destination_conflict")
    controls = []
    control_names = []
    for control in spec.compose:
        if (type(control) is not target_exec.ComposeControl
                or not target_exec._plain_text(control.filename, target_exec._SAFE_COMPOSE)
                or "/" in control.filename or "\\" in control.filename
                or not target_exec._plain_text(control.sha256, target_exec._HASH)):
            _fail("compose_controls_invalid")
        control_names.append(control.filename)
        controls.append(control)
    if len(set(control_names)) != len(control_names):
        _fail("compose_controls_invalid")
    return app, assets_parent, temporary_parent, sidecars, tuple(mounts), tuple(preserved)


def _identity(path: Path, *, kind: str, mode: int | None = None):
    try:
        return target_exec._identity(path, kind=kind, required_mode=mode)
    except target_exec.PrebuiltTargetError as exc:
        _fail(exc.code)


def _snapshots(spec, app: Path, assets_parent: Path, temporary_parent: Path):
    paths = (app, assets_parent, temporary_parent)
    result = {}
    for directory in paths:
        target_exec._no_symlink_ancestors(directory)
        result[directory] = _identity(directory, kind="dir")
    _identity(assets_parent, kind="dir", mode=0o700)
    _identity(temporary_parent, kind="dir", mode=0o700)
    controls = {}
    for control in spec.compose:
        path = app / control.filename
        target_exec._no_symlink_ancestors(path)
        snapshot, content = target_exec._read_control(path, control.sha256)
        target_exec._validate_compose_control(content)
        result[path] = snapshot
        controls[path] = content
    for mount in spec.preserved_mounts:
        path = Path(mount.source)
        target_exec._no_symlink_ancestors(path)
        snapshot, _content = target_exec._read_control(path, mount.sha256)
        result[path] = snapshot
    runtime = None
    if spec.runtime_env_path is not None:
        runtime = Path(spec.runtime_env_path)
        target_exec._no_symlink_ancestors(runtime.parent)
        result[runtime.parent] = _identity(runtime.parent, kind="dir", mode=0o700)
        result[runtime] = _identity(runtime, kind="file", mode=0o600)
    return result, controls, runtime


def _chmod_assets(root: Path) -> dict[Path, tuple[int, int, int, int, int, int]]:
    snapshots = {}
    stack = [root]
    while stack:
        current = stack.pop()
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            _fail("asset_tree_invalid")
        if stat.S_ISDIR(info.st_mode):
            os.chmod(current, 0o755)
            snapshots[current] = _identity(current, kind="dir")
            try:
                children = list(current.iterdir())
            except BaseException:
                _fail("asset_tree_invalid")
            stack.extend(children)
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            os.chmod(current, 0o644)
            snapshots[current] = _identity(current, kind="file")
        else:
            _fail("asset_tree_invalid")
    return snapshots


def _write_private(path: Path, data: bytes) -> tuple[int, int, int, int, int, int]:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        _fail("private_control_write_failed")
    return _identity(path, kind="file", mode=0o600)


def _rendered_mounts(service: object):
    if type(service) is not dict:
        _fail("compose_service_invalid")
    values = service.get("volumes", [])
    if type(values) is not list:
        _fail("compose_mounts_invalid")
    result = []
    for volume in values:
        if type(volume) is not dict or volume.get("type") != "bind":
            _fail("compose_mounts_invalid")
        source, target, read_only = volume.get("source"), volume.get("target"), volume.get("read_only")
        if type(source) is not str or type(target) is not str or type(read_only) is not bool:
            _fail("compose_mounts_invalid")
        result.append((source, target, read_only))
    if len(set((source, target) for source, target, _ in result)) != len(result):
        _fail("compose_mounts_duplicate")
    return result


def execute_static_deployment(
    raw_manifest: bytes,
    *,
    expected_identity: release_manifest.ReleaseIdentity,
    selected_artifact: release_manifest.StaticArtifactSelection,
    bundle: bytes | bytearray,
    target: StaticTargetSpec,
    executor=None,
    clock: Callable[[], float] = time.monotonic,
    deadline: float | None = None,
) -> StaticDeploymentResult:
    """Validate, materialize, pull and activate one static release on target.

    Authorization, artifact retrieval binding and runtime-input custody belong
    to a trusted caller. This function never executes artifact-controlled code.
    """
    try:
        app, assets_parent, temporary_parent, sidecars, asset_mounts, preserved = _validate_spec(
            target, expected_identity, selected_artifact)
    except StaticTargetError:
        raise
    except BaseException:
        _fail('target_spec_invalid')
    if type(raw_manifest) is not bytes or len(raw_manifest) > release_manifest.MAX_MANIFEST_BYTES:
        _fail("manifest_bytes_required")
    if type(bundle) not in (bytes, bytearray) or len(bundle) > release_manifest.MAX_BUNDLE_BYTES:
        _fail("bundle_bytes_invalid")
    if not callable(clock) or (executor is not None and not callable(getattr(executor, "run", None))):
        _fail("executor_invalid")
    try:
        manifest = release_manifest.validate_manifest(
            raw_manifest, expected=expected_identity, expected_static_artifact=selected_artifact)
        if manifest.get("kind") != "static":
            _fail("static_manifest_required")
        release_manifest.verify_static_bundle(bundle, manifest=manifest)
    except StaticTargetError:
        raise
    except BaseException:
        _fail("manifest_or_bundle_invalid")
    try:
        start = _now(clock)
    except BaseException:
        _fail("clock_invalid")
    if deadline is None:
        end = start + MAX_STATIC_DEPLOY_SECONDS
    elif (not target_exec._finite_number(deadline) or deadline <= start):
        _fail("deadline_invalid")
    else:
        end = min(float(deadline), start + MAX_STATIC_DEPLOY_SECONDS)
    if _now(clock) >= end:
        _fail("deadline_exceeded")
    try:
        snapshots, _controls, runtime = _snapshots(target, app, assets_parent, temporary_parent)
    except target_exec.PrebuiltTargetError:
        _fail("target_inputs_invalid")
    except BaseException:
        _fail("target_inputs_invalid")
    selected_executor = executor if executor is not None else target_exec._SubprocessExecutor()
    assets: Path | None = None
    assets_identity = None
    temp_dir: Path | None = None
    temp_identity = None
    private_files = {}
    retain_assets = False
    activated = False
    failure: StaticTargetError | None = None
    result: StaticDeploymentResult | None = None
    try:
        assets = prebuilt_deploy.prepare_static_assets(
            raw_manifest, expected_identity=expected_identity, selected_artifact=selected_artifact,
            bundle=bundle, allowed_asset_roots=target.allowed_asset_roots,
            destination_parent=str(assets_parent))
        assets_identity = _identity(assets, kind="dir", mode=0o700)
        snapshots.update(_chmod_assets(assets))
        assets_identity = _identity(assets, kind="dir")
        temp_dir = Path(tempfile.mkdtemp(prefix="prebuilt-static-", dir=temporary_parent))
        temp_identity = _identity(temp_dir, kind="dir", mode=0o700)
        override = temp_dir / "override.yaml"
        config = temp_dir / "config.json"
        # A NONEMPTY auth map prevents Docker's default credential-store detection.
        # Every entry is anonymous; no helper/store/context or credential is supplied.
        registries = {image.split('/', 1)[0] for image in (target.static_image, *sidecars.values())}
        auths = {('https://index.docker.io/v1/' if name == 'docker.io' else name): {} for name in registries}
        private_files[config] = _write_private(config, json.dumps({'auths': auths}, sort_keys=True).encode('ascii'))
        if runtime is None:
            runtime = temp_dir / "interpolation-input.txt"
            private_files[runtime] = _write_private(runtime, b"")
        generated = ["services:"]
        mounts_by_service: dict[str, list[tuple[str, str]]] = {}
        for mount in asset_mounts:
            source = assets.joinpath(*mount.archive_root.split("/"))
            _identity(source, kind="dir")
            mounts_by_service.setdefault(target.static_service, []).append((str(source), mount.destination))
        for service, source, destination in preserved:
            mounts_by_service.setdefault(service, []).append((source, destination))
        for service in sorted({target.static_service, *sidecars}):
            image = target.static_image if service == target.static_service else sidecars[service]
            generated.extend((f"  {service}:", f"    image: {json.dumps(image)}", "    build: !reset null"))
            service_mounts = mounts_by_service.get(service, [])
            if service_mounts:
                generated.append("    volumes:")
            for source, destination in service_mounts:
                generated.extend(("      - type: bind", f"        source: {json.dumps(source)}",
                                  f"        target: {json.dumps(destination)}", "        read_only: true"))
        private_files[override] = _write_private(override, ("\n".join(generated) + "\n").encode("utf-8"))
        snapshots[temp_dir] = temp_identity
        snapshots.update(private_files)
        prefix = [*target_exec._DOCKER, "compose", "--project-name", target.project_name,
                  "--project-directory", str(app)]
        for control in target.compose:
            prefix.extend(("--file", control.filename))
        prefix.extend(("--file", str(override)))
        # Always explicit: no ambient project interpolation-file fallback.
        prefix.extend(("--env-file", str(runtime)))
        command = tuple(prefix)
        child_env = {"PATH": os.defpath, "HOME": str(temp_dir), "LANG": "C.UTF-8",
                     "LC_ALL": "C.UTF-8", "DOCKER_CONFIG": str(temp_dir)}

        def run(args, capture=False):
            if _now(clock) >= end:
                _fail("deadline_exceeded")
            target_exec._check_snapshots(snapshots)
            try:
                outcome = target_exec._run(selected_executor, args, cwd=str(app), env=child_env,
                    timeout=min(target_exec.MAX_COMMAND_SECONDS, end-_now(clock)),
                    capture=capture, deadline=end, clock=clock, snapshots=snapshots)
            except StaticTargetError:
                raise
            except target_exec.PrebuiltTargetError:
                _fail("command_failed")
            except BaseException:
                _fail("command_failed")
            return outcome

        config_raw = run(command + ("config", "--format", "json"), capture=True)
        rendered = target_exec._safe_json(config_raw)
        services = rendered.get("services") if type(rendered) is dict else None
        expected = {target.static_service, *sidecars.keys()}
        if type(services) is not dict or set(services) != expected:
            _fail("compose_services_mismatch")
        health_required = set()
        for name, service in services.items():
            if type(service) is not dict or "build" in service:
                _fail("compose_build_rejected")
            wanted_image = target.static_image if name == target.static_service else sidecars[name]
            if service.get("image") != wanted_image:
                _fail("compose_image_mismatch")
            wanted_mounts = [(str(assets.joinpath(*m.archive_root.split("/"))), m.destination, True)
                             for m in asset_mounts] if name == target.static_service else []
            wanted_mounts.extend((source, destination, True) for svc, source, destination in preserved if svc == name)
            if sorted(_rendered_mounts(service)) != sorted(wanted_mounts):
                _fail("compose_mounts_mismatch")
            healthcheck = service.get('healthcheck')
            if healthcheck is not None:
                if type(healthcheck) is not dict or ('disable' in healthcheck and type(healthcheck['disable']) is not bool):
                    _fail('compose_healthcheck_invalid')
                if healthcheck.get('disable') is not True and healthcheck.get('test') != ['NONE']:
                    health_required.add(name)
        run(command + ("pull",))
        if _now(clock) >= end:
            _fail("deadline_exceeded")
        activated = True
        run(command + ("up", "--detach", "--no-build", "--pull", "never",
                       "--force-recreate", "--remove-orphans"))
        ps_raw = run(command + ("ps", "--all", "--format", "json"), capture=True)
        observed = target_exec._decode_ps(ps_raw, expected, target.project_name)
        for service in sorted(expected):
            cid = observed[service][0]
            inspect_raw = target_exec._run(selected_executor,
                target_exec._DOCKER + ("inspect", "--format", target_exec._INSPECT_TEMPLATE, cid),
                cwd=str(app), env={"PATH": os.defpath, "HOME": str(temp_dir), "LANG": "C.UTF-8",
                                   "LC_ALL": "C.UTF-8", "DOCKER_CONFIG": str(temp_dir)},
                timeout=min(target_exec.MAX_COMMAND_SECONDS, end-_now(clock)), capture=True,
                deadline=end, clock=clock, snapshots=snapshots)
            if _now(clock) >= end:
                _fail("deadline_exceeded")
            fields = inspect_raw.decode("utf-8", errors="strict").splitlines()
            if len(fields) != 6:
                _fail("container_inspect_invalid")
            values = [target_exec._safe_json((line + "\n").encode("utf-8")) for line in fields]
            image, state, restarting, health, project, actual_service = values
            wanted = target.static_image if service == target.static_service else sidecars[service]
            if image != wanted or project != target.project_name or actual_service != service:
                _fail("container_identity_mismatch")
            if (state != "running" or restarting is not False or health not in (None, "healthy")
                    or (service in health_required and health != 'healthy')):
                _fail("container_unhealthy")
        if _now(clock) >= end:
            _fail("deadline_exceeded")
        output = manifest["output"]
        result = StaticDeploymentResult(
            project_name=target.project_name, environment=target.environment,
            source_repository=expected_identity.repository, source_sha=expected_identity.sha,
            run_id=expected_identity.run_id, attempt=expected_identity.attempt,
            artifact_id=output["artifact_id"], artifact_sha256=output["sha256"],
            static_image=target.static_image, services=tuple(sorted(expected)),
            asset_directory=str(assets))
        retain_assets = True
    except StaticTargetError as exc:
        failure = exc
    except prebuilt_deploy.PreparationError:
        failure = StaticTargetError("static_assets_invalid")
    except target_exec.PrebuiltTargetError:
        failure = StaticTargetError("target_execution_failed")
    except BaseException:
        failure = StaticTargetError("execution_failed")
    finally:
        if temp_dir is not None:
            try:
                target_exec._check_snapshots({temporary_parent: snapshots[temporary_parent]})
                target_exec._no_symlink_ancestors(temp_dir)
                if temp_identity is None:
                    # Only remove the empty directory just allocated; never
                    # recurse into an unverified path after initial stat failure.
                    temp_dir.rmdir()
                else:
                    if _identity(temp_dir, kind="dir") != temp_identity:
                        _fail("cleanup_identity_changed")
                    shutil.rmtree(temp_dir)
            except BaseException:
                failure = StaticTargetError("cleanup_failed")
        if assets is not None and not retain_assets and not activated:
            try:
                target_exec._check_snapshots({assets_parent: snapshots[assets_parent]})
                target_exec._no_symlink_ancestors(assets)
                current = _identity(assets, kind="dir")
                if (assets_identity is None or current[:2] != assets_identity[:2]
                        or current[3] != assets_identity[3]
                        or stat.S_IMODE(current[2]) not in (0o700, 0o755)):
                    _fail("asset_cleanup_identity_changed")
                shutil.rmtree(assets)
            except BaseException:
                failure = StaticTargetError("asset_cleanup_failed")
    if failure is not None:
        raise StaticTargetError(failure.code, retained_asset_directory=str(assets) if activated and assets is not None else None) from None
    if result is None:
        _fail("execution_failed")
    return result
