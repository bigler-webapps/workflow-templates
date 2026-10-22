"""Synthetic behavioral tests for strict static target execution."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from dataclasses import replace
from unittest import mock

import prebuilt_static_target as static_target
import prebuilt_target
import release_manifest


IDENTITY = release_manifest.ReleaseIdentity(
    repository="owner/static-site", sha="a" * 40, ref="refs/heads/develop",
    workflow=".github/workflows/release.yml", run_id=123456, attempt=2,
)
IMAGE = "docker.io/library/caddy@sha256:" + "b" * 64


def make_archive(content: bytes = b"immutable site") -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        info = tarfile.TarInfo("report/index.html")
        info.size = len(content)
        info.mode = 0o644
        archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


class FakeExecutor:
    def __init__(self, *, fail_at: str | None = None, unhealthy: bool = False,
                 wrong_mount: bool = False, preserved=(), image=IMAGE, service='web', project='static-site', sidecar_images=None):
        self.calls = []
        self.fail_at = fail_at
        self.unhealthy = unhealthy
        self.wrong_mount = wrong_mount
        self.preserved = tuple(preserved)
        self.image, self.service, self.project = image, service, project
        self.images = {service: image, **(sidecar_images or {})}
        self.container_services = {f'{index + 10:064x}': name for index, name in enumerate(sorted(self.images))}
        self.commands_after_up = False
        self.profiles = []
        self.interpolation_inputs = []

    def run(self, argv, *, cwd, env, timeout, capture=False):
        self.calls.append((argv, cwd, dict(env), timeout, capture))
        self.profiles.append(json.loads((Path(env['DOCKER_CONFIG']) / 'config.json').read_text(encoding='utf-8')))
        if '--env-file' in argv:
            selected_input = Path(argv[argv.index('--env-file') + 1])
            if selected_input.name == 'interpolation-input.txt':
                self.interpolation_inputs.append(selected_input.read_bytes())
        if argv[:3] == ("docker", "--host", "unix:///var/run/docker.sock"):
            args = argv[3:]
        else:
            args = argv
        joined = " ".join(args)
        if " up " in f" {joined} ":
            self.commands_after_up = True
        if self.fail_at and self.fail_at in joined:
            return prebuilt_target._CommandResult(17, b"synthetic private error")
        if args[-3:] == ("config", "--format", "json"):
            override = Path(args[args.index("--file", args.index("--file") + 1) + 1])
            text = override.read_text(encoding="utf-8")
            lines = text.splitlines()
            self.override_text = text
            original = Path(cwd, args[args.index('--file') + 1]).read_text(encoding='utf-8')
            services, reset, name, volume = {}, set(), None, None
            for line in lines:
                value = line.strip()
                if line.startswith('  ') and not line.startswith('   ') and value.endswith(':'):
                    name = value[:-1]
                    services[name] = {'volumes': []}
                elif name is not None and value.startswith('image: '):
                    services[name]['image'] = json.loads(value[len('image: '):])
                elif name is not None and value == 'build: !reset null':
                    reset.add(name)
                elif name is not None and value == '- type: bind':
                    volume = {'type': 'bind'}; services[name]['volumes'].append(volume)
                elif volume is not None and value.startswith('source: '):
                    volume['source'] = json.loads(value[len('source: '):])
                elif volume is not None and value.startswith('target: '):
                    volume['target'] = json.loads(value[len('target: '):])
                elif volume is not None and value == 'read_only: true':
                    volume['read_only'] = True
            for name, service in services.items():
                if 'image' not in service:
                    service['image'] = next(line.split('image:', 1)[1].strip() for line in original.splitlines() if 'image:' in line)
                if 'build:' in original and name not in reset:
                    service['build'] = {'context': '.'}
            if self.wrong_mount:
                services[self.service]['volumes'][0]['source'] = '/wrong/assets'
            rendered = {"services": services}
            return prebuilt_target._CommandResult(0, json.dumps(rendered).encode())
        if args[-4:] == ("ps", "--all", "--format", "json"):
            rows = [{"ID": cid, "Name": f'static-{service}-1', "Project": self.project,
                     "Service": service, "State": "running", "Health": "healthy"}
                    for cid, service in self.container_services.items()]
            return prebuilt_target._CommandResult(0, ''.join(json.dumps(row) + '\n' for row in rows).encode())
        if args[0:2] == ("inspect", "--format"):
            health = "unhealthy" if self.unhealthy else "healthy"
            service = self.container_services[argv[-1]]
            values = [self.images[service], "running", False, health, self.project, service]
            return prebuilt_target._CommandResult(0, "\n".join(json.dumps(x) for x in values).encode())
        return prebuilt_target._CommandResult(0, b"")


class StaticTargetTests(unittest.TestCase):
    def setUp(self):
        self.process_guard = mock.patch.object(prebuilt_target.subprocess, 'Popen', side_effect=AssertionError('real process forbidden'))
        self.process_guard.start()
        self.addCleanup(self.process_guard.stop)
        self.temp = tempfile.TemporaryDirectory(prefix="ci45-static-")
        self.root = Path(self.temp.name)
        os.chmod(self.root, 0o700)
        self.app = self.root / "app"
        self.assets_parent = self.root / "assets"
        self.temp_parent = self.root / "private"
        self.app.mkdir(mode=0o700)
        self.assets_parent.mkdir(mode=0o700)
        self.temp_parent.mkdir(mode=0o700)
        self.compose = self.app / "compose.yaml"
        (self.app / "Caddyfile").write_text(":80 { root * /srv/report; file_server }\n", encoding="utf-8")
        caddy_hash = hashlib.sha256((self.app / "Caddyfile").read_bytes()).hexdigest()
        content = (f"services:\n  web:\n    image: {IMAGE}\n    volumes:\n"
                   "      - ./Caddyfile:/etc/caddy/Caddyfile:ro\n")
        self.compose.write_text(content, encoding="utf-8")
        self.archive = make_archive()
        self.selection = release_manifest.StaticArtifactSelection(
            release_manifest.static_artifact_name(IDENTITY), 789)
        self.manifest = release_manifest.produce_static_manifest(
            IDENTITY, artifact_id=self.selection.artifact_id,
            bundle=self.archive, bundle_length=len(self.archive))
        self.spec = static_target.StaticTargetSpec(
            project_name="static-site", environment="staging", application_dir=str(self.app),
            compose=(prebuilt_target.ComposeControl("compose.yaml", hashlib.sha256(content.encode()).hexdigest()),),
            static_service="web", static_image=IMAGE, sidecar_images=(),
            allowed_asset_roots=("report",),
            asset_mounts=(static_target.AssetMount("report", "/srv/report"),),
            preserved_mounts=(static_target.StaticMount(
                "web", str(self.app / "Caddyfile"), "/etc/caddy/Caddyfile", caddy_hash),),
            assets_parent=str(self.assets_parent),
            temporary_parent=str(self.temp_parent),
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_valid_release_uses_pinned_image_and_persists_readable_assets(self):
        fake = FakeExecutor(preserved=((str(self.app / "Caddyfile"), "/etc/caddy/Caddyfile"),))
        with mock.patch.object(prebuilt_target.subprocess, "Popen", side_effect=AssertionError("real process")):
            result = static_target.execute_static_deployment(
                self.manifest, expected_identity=IDENTITY, selected_artifact=self.selection,
                bundle=self.archive, target=self.spec, executor=fake)
        self.assertEqual(result.static_image, IMAGE)
        asset_root = Path(result.asset_directory)
        self.assertEqual((asset_root / "report" / "index.html").read_bytes(), b"immutable site")
        self.assertEqual((asset_root / "report").stat().st_mode & 0o777, 0o755)
        self.assertEqual((asset_root / "report" / "index.html").stat().st_mode & 0o777, 0o644)
        commands = [call[0] for call in fake.calls]
        self.assertTrue(any(command[-1:] == ("pull",) for command in commands))
        up = next(command for command in commands if "up" in command)
        self.assertIn("--no-build", up)
        self.assertIn("--pull", up)
        self.assertFalse(any("build" in command for command in commands))
        for _, _, env, _, _ in fake.calls:
            self.assertNotIn("DOCKER_HOST", env)
            self.assertNotIn("DOCKER_CONTEXT", env)
            self.assertEqual(env.get("DOCKER_CONFIG", ""), env.get("HOME", ""))
        self.assertTrue(fake.profiles)
        self.assertTrue(all(profile == {'auths': {'https://index.docker.io/v1/': {}}} for profile in fake.profiles))
        self.assertTrue(fake.interpolation_inputs)
        self.assertTrue(all(value == b'' for value in fake.interpolation_inputs))
        self.assertFalse(list(self.temp_parent.iterdir()))

    def test_bad_artifact_never_reaches_executor_or_asset_filesystem(self):
        fake = FakeExecutor()
        wrong = make_archive(b"changed bytes")
        with self.assertRaises(static_target.StaticTargetError):
            static_target.execute_static_deployment(
                self.manifest, expected_identity=IDENTITY, selected_artifact=self.selection,
                bundle=wrong, target=self.spec, executor=fake)
        self.assertEqual(fake.calls, [])
        self.assertEqual(list(self.assets_parent.iterdir()), [])

    def test_wrong_rendered_mount_fails_before_pull_and_cleans_own_assets(self):
        fake = FakeExecutor(wrong_mount=True,
                            preserved=((str(self.app / "Caddyfile"), "/etc/caddy/Caddyfile"),))
        with self.assertRaises(static_target.StaticTargetError):
            static_target.execute_static_deployment(
                self.manifest, expected_identity=IDENTITY, selected_artifact=self.selection,
                bundle=self.archive, target=self.spec, executor=fake)
        self.assertFalse(any(call[0][-1:] == ("pull",) for call in fake.calls))
        self.assertEqual(list(self.assets_parent.iterdir()), [])

    def test_post_activation_failure_retains_assets_for_recovery(self):
        fake = FakeExecutor(fail_at="up", preserved=((str(self.app / "Caddyfile"), "/etc/caddy/Caddyfile"),))
        with self.assertRaises(static_target.StaticTargetError) as caught:
            static_target.execute_static_deployment(
                self.manifest, expected_identity=IDENTITY, selected_artifact=self.selection,
                bundle=self.archive, target=self.spec, executor=fake)
        retained = list(self.assets_parent.iterdir())
        self.assertEqual(caught.exception.retained_asset_directory, str(retained[0]))
        self.assertEqual(len(retained), 1)
        self.assertEqual((retained[0] / "report" / "index.html").read_bytes(), b"immutable site")
        self.assertTrue(fake.commands_after_up)

    def test_unhealthy_observation_never_returns_success_and_retains_assets(self):
        fake = FakeExecutor(unhealthy=True,
                            preserved=((str(self.app / "Caddyfile"), "/etc/caddy/Caddyfile"),))
        with self.assertRaises(static_target.StaticTargetError):
            static_target.execute_static_deployment(
                self.manifest, expected_identity=IDENTITY, selected_artifact=self.selection,
                bundle=self.archive, target=self.spec, executor=fake)
        self.assertEqual(len(list(self.assets_parent.iterdir())), 1)

    def test_no_runtime_or_package_secret_profile_is_forwarded(self):
        fake = FakeExecutor(preserved=((str(self.app / "Caddyfile"), "/etc/caddy/Caddyfile"),))
        result = static_target.execute_static_deployment(
            self.manifest, expected_identity=IDENTITY, selected_artifact=self.selection,
            bundle=self.archive, target=self.spec, executor=fake)
        self.assertTrue(Path(result.asset_directory).is_dir())
        self.assertIn("--env-file", fake.calls[0][0])
        self.assertTrue(all(value == b'' for value in fake.interpolation_inputs))
        self.assertEqual(fake.profiles[0], {'auths': {'https://index.docker.io/v1/': {}}})

    def test_invalid_target_policy_never_reaches_executor_or_assets(self):
        changes = [
            {'project_name': 'x\nEVIL=value'}, {'environment': 'production'},
            {'static_image': 'caddy:2-alpine'}, {'assets_parent': str(self.app / 'assets')},
            {'static_image': 'registry.example/' + 'long' * 70 + '@sha256:' + 'e' * 64},
            {'asset_mounts': (static_target.AssetMount('report', '/srv/../etc'),)},
            {'preserved_mounts': (replace(self.spec.preserved_mounts[0], source=str(self.temp_parent / 'runtime-input.txt')),)},
            {'preserved_mounts': (replace(self.spec.preserved_mounts[0], sha256='invalid'),)},
        ]
        for change in changes:
            with self.subTest(change=change):
                fake = FakeExecutor()
                with self.assertRaises(static_target.StaticTargetError):
                    static_target.execute_static_deployment(self.manifest, expected_identity=IDENTITY,
                        selected_artifact=self.selection, bundle=self.archive, target=replace(self.spec, **change), executor=fake)
                self.assertEqual(fake.calls, [])
                self.assertEqual(list(self.assets_parent.iterdir()), [])

    def test_wrong_source_and_static_artifact_selection_fail_before_files(self):
        for identity, selection in [
            (replace(IDENTITY, sha='b' * 40), self.selection),
            (replace(IDENTITY, run_id=987), self.selection),
            (IDENTITY, replace(self.selection, artifact_id=790)),
        ]:
            with self.subTest(identity=identity, selection=selection):
                fake = FakeExecutor()
                with self.assertRaises(static_target.StaticTargetError):
                    static_target.execute_static_deployment(self.manifest, expected_identity=identity,
                        selected_artifact=selection, bundle=self.archive, target=self.spec, executor=fake)
                self.assertEqual(fake.calls, [])
                self.assertEqual(list(self.assets_parent.iterdir()), [])

    def test_valid_manifest_cannot_authorize_unsafe_archive_members(self):
        for name, kind in [('report/../../escape', tarfile.REGTYPE), ('report/Caddyfile', tarfile.REGTYPE),
                           ('report/link', tarfile.SYMTYPE), ('report/hard', tarfile.LNKTYPE)]:
            with self.subTest(name=name, kind=kind):
                buffer = io.BytesIO()
                with tarfile.open(fileobj=buffer, mode='w', format=tarfile.USTAR_FORMAT) as archive:
                    info = tarfile.TarInfo(name); info.type = kind
                    if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE): info.linkname = '/private/runtime-input.txt'
                    archive.addfile(info)
                bundle = buffer.getvalue()
                manifest = release_manifest.produce_static_manifest(IDENTITY, artifact_id=self.selection.artifact_id,
                    bundle=bundle, bundle_length=len(bundle))
                fake = FakeExecutor()
                with self.assertRaises(static_target.StaticTargetError):
                    static_target.execute_static_deployment(manifest, expected_identity=IDENTITY,
                        selected_artifact=self.selection, bundle=bundle, target=self.spec, executor=fake)
                self.assertEqual(fake.calls, [])
                self.assertEqual(list(self.assets_parent.iterdir()), [])

    def test_bad_rendered_service_image_build_or_mount_never_pulls(self):
        owner = self
        class BadRender(FakeExecutor):
            def run(self, argv, **kwargs):
                result = super().run(argv, **kwargs)
                if argv[-3:] == ('config', '--format', 'json'):
                    data = json.loads(result.stdout)
                    service = data['services']['web']
                    if owner.variant == 'image': service['image'] = 'caddy:latest'
                    elif owner.variant == 'build': service['build'] = {'context': '.'}
                    elif owner.variant == 'writable': service['volumes'][0]['read_only'] = False
                    elif owner.variant == 'service': data['services']['rogue'] = dict(service)
                    return prebuilt_target._CommandResult(0, json.dumps(data).encode())
                return result
        for self.variant in ('image', 'build', 'writable', 'service'):
            with self.subTest(variant=self.variant):
                fake = BadRender()
                with self.assertRaises(static_target.StaticTargetError):
                    static_target.execute_static_deployment(self.manifest, expected_identity=IDENTITY,
                        selected_artifact=self.selection, bundle=self.archive, target=self.spec, executor=fake)
                self.assertFalse(any(call[0][-1:] == ('pull',) for call in fake.calls))
                self.assertEqual(list(self.assets_parent.iterdir()), [])

    def test_pull_failure_cleans_only_own_new_assets_and_preserves_previous(self):
        previous = self.assets_parent / 'previous'; previous.mkdir(mode=0o700)
        (previous / 'sentinel.txt').write_bytes(b'previous-release')
        fake = FakeExecutor(fail_at='pull')
        with self.assertRaises(static_target.StaticTargetError) as caught:
            static_target.execute_static_deployment(self.manifest, expected_identity=IDENTITY,
                selected_artifact=self.selection, bundle=self.archive, target=self.spec, executor=fake)
        self.assertIsNone(caught.exception.retained_asset_directory)
        self.assertEqual(list(self.assets_parent.iterdir()), [previous])
        self.assertEqual((previous / 'sentinel.txt').read_bytes(), b'previous-release')

    def test_enabled_healthcheck_requires_observed_healthy_status(self):
        class MissingHealth(FakeExecutor):
            def run(self, argv, **kwargs):
                result = super().run(argv, **kwargs)
                if argv[-3:] == ('config', '--format', 'json'):
                    data = json.loads(result.stdout)
                    data['services']['web']['healthcheck'] = {'test': ['CMD', 'fixed-healthcheck']}
                    return prebuilt_target._CommandResult(0, json.dumps(data).encode())
                if 'inspect' in argv:
                    values = [IMAGE, 'running', False, None, 'static-site', 'web']
                    return prebuilt_target._CommandResult(0, '\n'.join(json.dumps(x) for x in values).encode())
                return result
        with self.assertRaises(static_target.StaticTargetError) as caught:
            static_target.execute_static_deployment(self.manifest, expected_identity=IDENTITY,
                selected_artifact=self.selection, bundle=self.archive, target=self.spec, executor=MissingHealth())
        self.assertIsNotNone(caught.exception.retained_asset_directory)

    def test_provider_runtime_is_metadata_only_never_opened_by_library(self):
        parent = self.root / 'provider'; parent.mkdir(mode=0o700)
        runtime = parent / 'runtime-input.txt'; runtime.write_bytes(b'synthetic provider input'); runtime.chmod(0o600)
        original_open = os.open
        original_path_open = Path.open
        def guarded_open(path, *args, **kwargs):
            if Path(path) == runtime: raise AssertionError('runtime value read')
            return original_open(path, *args, **kwargs)
        def guarded_path_open(path, *args, **kwargs):
            if path == runtime: raise AssertionError('runtime value read')
            return original_path_open(path, *args, **kwargs)
        fake = FakeExecutor()
        with mock.patch.object(os, 'open', side_effect=guarded_open), mock.patch.object(Path, 'open', guarded_path_open):
            result = static_target.execute_static_deployment(self.manifest, expected_identity=IDENTITY,
                selected_artifact=self.selection, bundle=self.archive,
                target=replace(self.spec, runtime_env_path=str(runtime)), executor=fake)
        self.assertTrue(Path(result.asset_directory).is_dir())
        self.assertIn(str(runtime), fake.calls[0][0])

    def test_pre_and_post_activation_cancellation_preserve_correct_asset_boundary(self):
        for phase in ('config', 'up'):
            with self.subTest(phase=phase):
                class Cancel(FakeExecutor):
                    def run(self, argv, **kwargs):
                        if phase in argv: raise KeyboardInterrupt('synthetic cancellation')
                        return super().run(argv, **kwargs)
                with self.assertRaises(static_target.StaticTargetError) as caught:
                    static_target.execute_static_deployment(self.manifest, expected_identity=IDENTITY,
                        selected_artifact=self.selection, bundle=self.archive, target=self.spec, executor=Cancel())
                self.assertEqual(caught.exception.retained_asset_directory is not None, phase == 'up')
                self.assertEqual(len(list(self.assets_parent.iterdir())), 1 if phase == 'up' else 0)

    def test_replaced_asset_directory_is_not_recursively_removed(self):
        owner = self
        class ReplaceAssets(FakeExecutor):
            def run(self, argv, **kwargs):
                result = super().run(argv, **kwargs)
                if argv[-3:] == ('config', '--format', 'json'):
                    asset = next(owner.assets_parent.iterdir())
                    asset.rename(owner.root / 'retained-original')
                    asset.mkdir(mode=0o700); (asset / 'sentinel').write_bytes(b'do not remove')
                return result
        with self.assertRaises(static_target.StaticTargetError):
            static_target.execute_static_deployment(self.manifest, expected_identity=IDENTITY,
                selected_artifact=self.selection, bundle=self.archive, target=self.spec, executor=ReplaceAssets())
        retained = next(self.assets_parent.iterdir())
        self.assertEqual((retained / 'sentinel').read_bytes(), b'do not remove')
        self.assertEqual((self.root / 'retained-original' / 'report' / 'index.html').read_bytes(), b'immutable site')

    def test_initial_temporary_identity_failure_removes_only_empty_owned_directory(self):
        original = static_target._identity
        reached = []
        def fail_initial(path, **kwargs):
            if Path(path).name.startswith('prebuilt-static-'):
                reached.append(Path(path))
                raise static_target.StaticTargetError('synthetic_initial_identity_failure')
            return original(path, **kwargs)
        with mock.patch.object(static_target, '_identity', side_effect=fail_initial):
            with self.assertRaises(static_target.StaticTargetError):
                static_target.execute_static_deployment(self.manifest, expected_identity=IDENTITY,
                    selected_artifact=self.selection, bundle=self.archive, target=self.spec, executor=FakeExecutor())
        self.assertEqual(list(self.temp_parent.iterdir()), [])
        self.assertEqual(list(self.assets_parent.iterdir()), [])
        self.assertEqual(len(reached), 1)
        self.assertFalse(reached[0].exists())

    def test_invalid_or_expired_clock_never_materializes_assets(self):
        for values in ([float('nan')], [False], [0, float('inf')], [0, 100000]):
            with self.subTest(values=values):
                ticks = iter(values)
                def clock(): return next(ticks)
                fake = FakeExecutor()
                with self.assertRaises(static_target.StaticTargetError):
                    static_target.execute_static_deployment(self.manifest, expected_identity=IDENTITY,
                        selected_artifact=self.selection, bundle=self.archive, target=self.spec, executor=fake, clock=clock)
                self.assertEqual(fake.calls, [])
                self.assertEqual(list(self.assets_parent.iterdir()), [])

    def test_partial_asset_mode_adjustment_failure_cleans_owned_tree(self):
        reached = []
        def partial(root):
            root.chmod(0o755)
            reached.append((root, root.stat().st_mode & 0o777))
            raise static_target.StaticTargetError('synthetic_mode_failure')
        with mock.patch.object(static_target, '_chmod_assets', side_effect=partial):
            with self.assertRaises(static_target.StaticTargetError):
                static_target.execute_static_deployment(self.manifest, expected_identity=IDENTITY,
                    selected_artifact=self.selection, bundle=self.archive, target=self.spec, executor=FakeExecutor())
        self.assertEqual(list(self.assets_parent.iterdir()), [])
        self.assertEqual(len(reached), 1)
        self.assertEqual(reached[0][1], 0o755)
        self.assertFalse(reached[0][0].exists())

    def test_varied_static_identity_and_trusted_image_replace_legacy_build_controls(self):
        image = 'docker.io/library/caddy@sha256:' + 'c' * 64
        identity = replace(IDENTITY, repository='owner/other-site', sha='d' * 40, run_id=98765, attempt=3)
        selection = release_manifest.StaticArtifactSelection(release_manifest.static_artifact_name(identity), 987)
        manifest = release_manifest.produce_static_manifest(identity, artifact_id=selection.artifact_id,
            bundle=self.archive, bundle_length=len(self.archive))
        content = self.compose.read_text(encoding='utf-8').replace('  web:', '  site:').replace(IMAGE, 'caddy:2-alpine')
        content += '    build:\n      context: .\n'
        self.compose.write_text(content, encoding='utf-8')
        spec = replace(self.spec, project_name='another-project', static_service='site', static_image=image,
            compose=(prebuilt_target.ComposeControl('compose.yaml', hashlib.sha256(content.encode()).hexdigest()),),
            preserved_mounts=(replace(self.spec.preserved_mounts[0], service='site'),))
        fake = FakeExecutor(image=image, service='site', project='another-project')
        result = static_target.execute_static_deployment(manifest, expected_identity=identity,
            selected_artifact=selection, bundle=self.archive, target=spec, executor=fake)
        self.assertEqual(result.source_repository, identity.repository)
        self.assertEqual(result.source_sha, identity.sha)
        self.assertEqual((result.run_id, result.attempt, result.artifact_id), (98765, 3, 987))
        self.assertEqual(result.static_image, image)
        self.assertEqual(result.services, ('site',))
        self.assertIn('build: !reset null', fake.override_text)
        self.assertIn(image, fake.override_text)
        self.assertTrue(any('up' in call[0] for call in fake.calls))

    def test_private_write_fdopen_failure_closes_allocated_descriptor(self):
        original = os.open
        descriptors = []
        def tracked(*args, **kwargs):
            fd = original(*args, **kwargs); descriptors.append(fd); return fd
        with mock.patch.object(os, 'open', side_effect=tracked), mock.patch.object(os, 'fdopen', side_effect=OSError('synthetic')):
            with self.assertRaises(static_target.StaticTargetError):
                static_target._write_private(self.root / 'write-failure.txt', b'')
        self.assertEqual(len(descriptors), 1)
        with self.assertRaises(OSError): os.fstat(descriptors[0])

    def test_static_and_sidecar_registry_ports_are_validated_before_effects(self):
        for field in ('static_image', 'sidecar_images'):
            bad = 'registry.example:99999/runtime@sha256:' + 'e' * 64
            changed = {field: bad if field == 'static_image' else (('cache', bad),)}
            fake = FakeExecutor()
            with self.subTest(field=field), self.assertRaises(static_target.StaticTargetError):
                static_target.execute_static_deployment(self.manifest, expected_identity=IDENTITY,
                    selected_artifact=self.selection, bundle=self.archive, target=replace(self.spec, **changed), executor=fake)
            self.assertEqual(fake.calls, [])
            self.assertEqual(list(self.assets_parent.iterdir()), [])
        for port in (1, 65535):
            image = f'registry.example:{port}/runtime@sha256:' + 'e' * 64
            static_target._validate_spec(replace(self.spec, static_image=image), IDENTITY, self.selection)

    def test_sidecar_override_and_each_observed_digest_are_required(self):
        sidecar = 'docker.io/library/redis@sha256:' + 'e' * 64
        spec = replace(self.spec, sidecar_images=(('cache', sidecar),))
        fake = FakeExecutor(sidecar_images={'cache': sidecar})
        result = static_target.execute_static_deployment(self.manifest, expected_identity=IDENTITY,
            selected_artifact=self.selection, bundle=self.archive, target=spec, executor=fake)
        self.assertEqual(result.services, ('cache', 'web'))
        self.assertIn(sidecar, fake.override_text)
        inspections = [call[0] for call in fake.calls if 'inspect' in call[0]]
        self.assertEqual(len(inspections), 2)
        wrong = FakeExecutor(sidecar_images={'cache': 'docker.io/library/redis@sha256:' + 'f' * 64})
        with self.assertRaises(static_target.StaticTargetError) as caught:
            static_target.execute_static_deployment(self.manifest, expected_identity=IDENTITY,
                selected_artifact=self.selection, bundle=self.archive, target=spec, executor=wrong)
        self.assertIsNotNone(caught.exception.retained_asset_directory)

    def test_default_adapter_selection_uses_shared_executor_without_real_process(self):
        fake = FakeExecutor()
        with mock.patch.object(prebuilt_target, '_SubprocessExecutor', return_value=fake) as selected:
            result = static_target.execute_static_deployment(self.manifest, expected_identity=IDENTITY,
                selected_artifact=self.selection, bundle=self.archive, target=self.spec)
        selected.assert_called_once_with()
        self.assertTrue(Path(result.asset_directory).is_dir())
        self.assertTrue(any('up' in call[0] for call in fake.calls))

    def test_falsey_supplied_executor_is_not_replaced_by_real_default(self):
        class Falsey(FakeExecutor):
            def __bool__(self): return False
        fake = Falsey()
        with mock.patch.object(prebuilt_target, '_SubprocessExecutor', side_effect=AssertionError('unexpected default')):
            result = static_target.execute_static_deployment(self.manifest, expected_identity=IDENTITY,
                selected_artifact=self.selection, bundle=self.archive, target=self.spec, executor=fake)
        self.assertTrue(Path(result.asset_directory).is_dir())
        self.assertTrue(any('up' in call[0] for call in fake.calls))

    def test_cancellation_inside_shared_preparer_has_no_orphan_assets(self):
        original = static_target.prebuilt_deploy._write_file
        def cancel(root, archive, member):
            original(root, archive, member)
            raise KeyboardInterrupt('synthetic extraction cancellation')
        with mock.patch.object(static_target.prebuilt_deploy, '_write_file', side_effect=cancel):
            with self.assertRaises(static_target.StaticTargetError):
                static_target.execute_static_deployment(self.manifest, expected_identity=IDENTITY,
                    selected_artifact=self.selection, bundle=self.archive, target=self.spec, executor=FakeExecutor())
        self.assertEqual(list(self.assets_parent.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
