"""Behavioral tests for the CI static release producer; no live Git or upload."""
from __future__ import annotations

import importlib.util
import io
import json
import re
import os
from pathlib import Path
import stat
import sys
import shlex
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = ROOT / ".github" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
import prebuilt_deploy
import release_manifest
import static_release


SHA = "0123456789abcdef0123456789abcdef01234567"
IDENTITY = release_manifest.ReleaseIdentity(
    "bigler-webapps/sample-app", SHA, "refs/heads/develop",
    ".github/workflows/release.yml", 123456, 2,
)


def environment(root: Path) -> dict[str, str]:
    return {
        "RELEASE_CONTRACT": "v1",
        "GITHUB_REPOSITORY": IDENTITY.repository,
        "GITHUB_SHA": IDENTITY.sha,
        "GITHUB_REF": IDENTITY.ref,
        "GITHUB_WORKFLOW_REF": IDENTITY.repository + "/" + IDENTITY.workflow + "@" + IDENTITY.ref,
        "RELEASE_WORKFLOW_REF": IDENTITY.repository + "/" + IDENTITY.workflow + "@" + IDENTITY.ref,
        "GITHUB_RUN_ID": str(IDENTITY.run_id),
        "GITHUB_RUN_ATTEMPT": str(IDENTITY.attempt),
        "RUNNER_TEMP": str(root / "runner-temp"),
        "GITHUB_OUTPUT": str(root / "github-output"),
        "GITHUB_TOKEN": "synthetic-token-must-not-forward",
        "REGISTRY_TOKEN": "synthetic-registry-token-must-not-forward",
    }


def git_ok(calls=None, sha=SHA, root=None):
    def run(argv, *, env, timeout):
        if calls is not None:
            calls.append((argv, env, timeout))
        if argv == ['git', 'rev-parse', '--show-toplevel']:
            return (str(root or Path.cwd()) + '\n').encode('utf-8')
        if argv != ["git", "rev-parse", "HEAD"]:
            raise AssertionError("non-fixed Git command")
        return (sha + "\n").encode("ascii")
    return run


class StaticReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="static-release-test-")
        self.root = Path(self.temp.name)
        (self.root / "runner-temp").mkdir()
        (self.root / "github-output").write_text("", encoding="utf-8")
        self.original_cwd = Path.cwd()
        self.env = environment(self.root)

    def tearDown(self):
        os.chdir(self.original_cwd)
        self.temp.cleanup()

    def posix_required(self):
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
            self.skipTest("Linux-style private filesystem protections are unavailable on this platform")

    def test_identity_and_checkout_are_checked_before_asset_path_open(self):
        calls = []
        bad = dict(self.env, GITHUB_SHA="f" * 40)
        with self.assertRaises(static_release.StaticReleaseError) as caught:
            static_release.prepare(bad, "missing", "report/report", run=git_ok(calls))
        self.assertEqual(str(caught.exception), "source_identity_invalid")
        self.assertEqual(len(calls), 1)
        self.assertEqual(list((self.root / "runner-temp").iterdir()), [])
        self.assertNotIn("synthetic", str(caught.exception))
        def wrong_head(argv, *, env, timeout):
            return ("f" * 40 + "\n").encode("ascii")
        with self.assertRaises(static_release.StaticReleaseError) as caught:
            static_release.prepare(self.env, "missing", "report/report", run=wrong_head)
        self.assertEqual(str(caught.exception), "source_identity_invalid")
        self.assertEqual(list((self.root / "runner-temp").iterdir()), [])

    def test_rejects_workflow_checkout_contract_and_scalar_mismatches(self):
        for key, value, code in (
            ("RELEASE_CONTRACT", "v2", "release_contract_unsupported"),
            ("GITHUB_REF", "refs/pull/2/merge", "source_ref_unsupported"),
            ("RELEASE_WORKFLOW_REF", "other/.github/workflows/release.yml@refs/heads/develop", "workflow_identity_invalid"),
            ("GITHUB_WORKFLOW_REF", "other/.github/workflows/release.yml@refs/heads/develop", "workflow_identity_invalid"),
            ("GITHUB_RUN_ATTEMPT", "2\nGITHUB_OUTPUT=x", "run_identity_invalid"),
        ):
            with self.subTest(key=key, value=value):
                changed = dict(self.env, **{key: value})
                with self.assertRaises(static_release.StaticReleaseError) as caught:
                    static_release.prepare(changed, "report", "report/report", run=git_ok())
                self.assertEqual(str(caught.exception), code)
                self.assertEqual(list((self.root / "runner-temp").iterdir()), [])

    def test_path_and_mode_validation_precedes_publication(self):
        self.posix_required()
        workspace = self.root / "workspace"
        workspace.mkdir()
        os.chdir(workspace)
        (workspace / "report").mkdir()
        (workspace / "report" / "index.html").write_text("ok", encoding="utf-8")
        for asset_path in ("../report", "/report", "report\\assets", "C:/assets", "--help", "report/.github"):
            with self.subTest(asset_path=asset_path):
                with self.assertRaises(static_release.StaticReleaseError):
                    static_release.prepare(self.env, asset_path, "report/report", run=git_ok())
                self.assertEqual(list((self.root / "runner-temp").iterdir()), [])
        with self.assertRaises(static_release.StaticReleaseError):
            static_release.prepare(self.env, "report", "report/../outside", run=git_ok())
        self.assertEqual(list((self.root / "runner-temp").iterdir()), [])

    @unittest.skipUnless(os.name == "posix", "POSIX file type/mode fixtures required")
    def test_forbidden_control_is_rejected_without_opening_it(self):
        workspace = self.root / "workspace"
        assets = workspace / "dist"
        assets.mkdir(parents=True)
        secret = assets / ".env.production"
        secret.write_text("synthetic-secret-do-not-read", encoding="utf-8")
        os.chdir(workspace)
        original_open = Path.open
        def guarded_open(path, *args, **kwargs):
            if path.name == ".env.production":
                raise AssertionError("forbidden file was opened")
            return original_open(path, *args, **kwargs)
        with mock.patch.object(Path, "open", guarded_open):
            with self.assertRaises(static_release.StaticReleaseError) as caught:
                static_release.prepare(self.env, "dist", "report/report", run=git_ok())
        self.assertEqual(str(caught.exception), "asset_control_file_rejected")
        self.assertEqual(list((self.root / "runner-temp").iterdir()), [])

    @unittest.skipUnless(os.name == "posix", "POSIX file type/mode fixtures required")
    def test_rejects_symlink_and_executable_assets(self):
        workspace = self.root / "workspace"
        assets = workspace / "dist"
        assets.mkdir(parents=True)
        target = workspace / "outside.txt"
        target.write_text("outside", encoding="utf-8")
        try:
            (assets / "link.txt").symlink_to(target)
        except OSError:
            self.skipTest("symlink creation unavailable")
        os.chdir(workspace)
        with self.assertRaises(static_release.StaticReleaseError) as caught:
            static_release.prepare(self.env, "dist", "report/report", run=git_ok())
        self.assertEqual(str(caught.exception), "asset_file_type_rejected")
        (assets / "link.txt").unlink()
        executable = assets / "run.js"
        executable.write_text("ordinary data", encoding="utf-8")
        executable.chmod(0o755)
        with self.assertRaises(static_release.StaticReleaseError) as caught:
            static_release.prepare(self.env, "dist", "report/report", run=git_ok())
        self.assertEqual(str(caught.exception), "asset_file_type_rejected")

    @unittest.skipUnless(os.name == "posix", "POSIX file type/mode fixtures required")
    def test_rejects_real_file_member_total_and_name_bounds(self):
        workspace = self.root / "workspace"
        assets = workspace / "dist"
        assets.mkdir(parents=True)
        too_large = assets / "large.bin"
        with too_large.open("wb") as stream:
            stream.truncate(static_release.MAX_FILE + 1)
        os.chdir(workspace)
        with self.assertRaises(static_release.StaticReleaseError) as caught:
            static_release.prepare(self.env, "dist", "report/report", run=git_ok())
        self.assertEqual(str(caught.exception), "asset_file_limit")
        too_large.unlink()
        for index in range(static_release.MAX_MEMBERS + 1):
            (assets / f"asset-{index:04d}.txt").touch()
        with self.assertRaises(static_release.StaticReleaseError) as caught:
            static_release.prepare(self.env, "dist", "report/report", run=git_ok())
        self.assertEqual(str(caught.exception), "asset_member_limit")
        for child in assets.iterdir():
            child.unlink()
        for index in range(4):
            with (assets / f"chunk-{index}.bin").open("wb") as stream:
                stream.truncate(static_release.MAX_FILE)
        with (assets / "chunk-small.bin").open("wb") as stream:
            stream.write(b"x")
        with self.assertRaises(static_release.StaticReleaseError) as caught:
            static_release.prepare(self.env, "dist", "report/report", run=git_ok())
        self.assertEqual(str(caught.exception), "asset_total_limit")

    @unittest.skipUnless(os.name == "posix", "POSIX rename protections required")
    def test_detects_file_replacement_during_pack_and_cleans_its_output(self):
        workspace = self.root / "workspace"
        assets = workspace / "dist"
        assets.mkdir(parents=True)
        asset = assets / "index.html"
        asset.write_bytes(b"before")
        os.chdir(workspace)
        collect = static_release._collect
        def replace_after_scan(root_fd, prefix, output_root):
            result = collect(root_fd, prefix, output_root)
            asset.unlink()
            asset.write_bytes(b"after!")
            return result
        with mock.patch.object(static_release, "_collect", replace_after_scan):
            with self.assertRaises(static_release.StaticReleaseError) as caught:
                static_release.prepare(self.env, "dist", "report/report", run=git_ok())
        self.assertEqual(str(caught.exception), "asset_changed")
        self.assertEqual(list((self.root / "runner-temp").iterdir()), [])

    @unittest.skipUnless(os.name == "posix", "POSIX private output protections required")
    def test_cleanup_refuses_foreign_directory_and_preserves_it(self):
        foreign = self.root / "runner-temp" / "release-static-foreign"
        foreign.mkdir()
        marker = foreign / "keep"
        marker.write_text("preserve", encoding="utf-8")
        with self.assertRaises(static_release.StaticReleaseError):
            static_release.cleanup(foreign, self.env)
        self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")

    @unittest.skipUnless(os.name == "posix", "POSIX private output protections required")
    def test_bundle_manifest_and_real_preparer_roundtrip(self):
        self.posix_required()
        workspace = self.root / "workspace"
        dist = workspace / "frontend" / "dist"
        (dist / "nested").mkdir(parents=True)
        (dist / "index.html").write_bytes(b"<h1>Report</h1>")
        (dist / "nested" / "app.js").write_bytes(b"console.log('data');")
        os.chdir(workspace)
        calls = []
        private, name = static_release.prepare(self.env, "frontend/dist", "report/report", run=git_ok(calls))
        self.assertEqual(name, "release-static-123456-2")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("GITHUB_TOKEN", calls[0][1])
        self.assertNotIn("REGISTRY_TOKEN", calls[0][1])
        self.assertEqual(calls[0][0], ["git", "rev-parse", "HEAD"])
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o700)
        bundle_path = private / "bundle.tar"
        self.assertEqual(stat.S_IMODE(bundle_path.stat().st_mode), 0o600)
        with tarfile.open(bundle_path, "r:") as archive:
            members = archive.getmembers()
            self.assertEqual([m.name for m in members], ["report/report/index.html", "report/report/nested/app.js"])
            for member in members:
                self.assertEqual(member.type, tarfile.REGTYPE)
                self.assertEqual(member.mode & 0o111, 0)
                self.assertEqual((member.uid, member.gid, member.devmajor, member.devminor), (0, 0, 0, 0))
                self.assertEqual((member.uname, member.gname, member.linkname), ("", "", ""))
        artifact_id = "778899"
        static_release.finalize(self.env, str(private), artifact_id, run=git_ok())
        raw = (private / "release.json").read_bytes()
        self.assertEqual(stat.S_IMODE((private / "release.json").stat().st_mode), 0o600)
        selected = release_manifest.StaticArtifactSelection(name, int(artifact_id))
        manifest = release_manifest.validate_manifest(raw, expected=IDENTITY, expected_static_artifact=selected)
        bundle = bundle_path.read_bytes()
        self.assertEqual(release_manifest.verify_static_bundle(bundle, manifest=manifest)[0], len(bundle))
        altered = bytearray(bundle)
        altered[600] ^= 1
        with self.assertRaises(release_manifest.ManifestError):
            release_manifest.verify_static_bundle(altered, manifest=manifest)
        output = self.root / "prepared"
        output.mkdir()
        extracted = prebuilt_deploy.prepare_static_assets(
            raw, expected_identity=IDENTITY, selected_artifact=selected, bundle=bundle,
            allowed_asset_roots=("report/report",), destination_parent=output,
        )
        self.assertEqual((extracted / "report/report/index.html").read_bytes(), b"<h1>Report</h1>")
        self.assertEqual((extracted / "report/report/nested/app.js").read_bytes(), b"console.log('data');")
        self.assertEqual(stat.S_IMODE((extracted / "report/report/index.html").stat().st_mode), 0o600)
        static_release.cleanup(private, self.env)
        self.assertFalse(private.exists())
        alternate_sha = "abcdef0123456789abcdef0123456789abcdef01"
        alternate_env = dict(self.env)
        alternate_env.update({
            "GITHUB_REPOSITORY": "bigler-webapps/other-static",
            "GITHUB_SHA": alternate_sha,
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_WORKFLOW_REF": "bigler-webapps/other-static/.github/workflows/publish.yml@refs/heads/main",
            "RELEASE_WORKFLOW_REF": "bigler-webapps/other-static/.github/workflows/publish.yml@refs/heads/main",
            "GITHUB_RUN_ID": "9001",
            "GITHUB_RUN_ATTEMPT": "3",
            "GITHUB_OUTPUT": str(self.root / "alternate-output"),
        })
        Path(alternate_env["GITHUB_OUTPUT"]).write_text("", encoding="utf-8")
        alternate_identity = release_manifest.ReleaseIdentity(
            "bigler-webapps/other-static", alternate_sha, "refs/heads/main",
            ".github/workflows/publish.yml", 9001, 3,
        )
        (workspace / "alternate-dist").mkdir()
        (workspace / "alternate-dist" / "home.html").write_bytes(b"alternate")
        alternate_head = git_ok(sha=alternate_sha)
        alternate_private, alternate_name = static_release.prepare(
            alternate_env, "alternate-dist", "public/site", run=alternate_head,
        )
        self.assertEqual(alternate_name, "release-static-9001-3")
        with tarfile.open(alternate_private / "bundle.tar", "r:") as archive:
            self.assertEqual(archive.getnames(), ["public/site/home.html"])
        static_release.finalize(alternate_env, str(alternate_private), "333444", run=alternate_head)
        alternate_raw = (alternate_private / "release.json").read_bytes()
        alternate_bundle = (alternate_private / "bundle.tar").read_bytes()
        alternate_selected = release_manifest.StaticArtifactSelection(alternate_name, 333444)
        alternate_manifest = release_manifest.validate_manifest(
            alternate_raw, expected=alternate_identity, expected_static_artifact=alternate_selected,
        )
        self.assertEqual(release_manifest.verify_static_bundle(alternate_bundle, manifest=alternate_manifest)[0], len(alternate_bundle))
        alternate_output = self.root / "alternate-prepared"
        alternate_output.mkdir()
        extracted_alt = prebuilt_deploy.prepare_static_assets(
            alternate_raw, expected_identity=alternate_identity, selected_artifact=alternate_selected,
            bundle=alternate_bundle, allowed_asset_roots=("public/site",), destination_parent=alternate_output,
        )
        self.assertEqual((extracted_alt / "public/site/home.html").read_bytes(), b"alternate")
        static_release.cleanup(alternate_private, alternate_env)
        unrelated = self.root / "runner-temp" / "release-static-unrelated"
        unrelated.mkdir()
        (unrelated / "keep").write_text("keep", encoding="utf-8")
        self.assertEqual((unrelated / "keep").read_text(encoding="utf-8"), "keep")

    @unittest.skipUnless(os.name == "posix", "POSIX private output protections required")
    def test_missing_or_invalid_artifact_id_never_writes_manifest(self):
        workspace = self.root / "workspace"
        dist = workspace / "dist"
        dist.mkdir(parents=True)
        (dist / "index.html").write_text("ok", encoding="utf-8")
        os.chdir(workspace)
        private, _ = static_release.prepare(self.env, "dist", "report/report", run=git_ok())
        for invalid in ("", "0", "-1", "2\nartifact_id=3"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(static_release.StaticReleaseError):
                    static_release.finalize(self.env, str(private), invalid, run=git_ok())
                self.assertFalse((private / "release.json").exists())
        static_release.cleanup(private, self.env)

    def test_diagnostics_never_echo_exception_or_secret(self):
        def noisy_git(*args, **kwargs):
            raise RuntimeError("synthetic-secret-do-not-print " + "path/to/private")
        with self.assertRaises(static_release.StaticReleaseError) as caught:
            static_release._identity(self.env, noisy_git)
        self.assertEqual(str(caught.exception), "source_lookup_failed")
        self.assertNotIn("synthetic-secret", str(caught.exception))
        self.assertNotIn("private", str(caught.exception))

    def test_composite_action_has_two_pinned_data_upload_phases(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("controller PyYAML is not installed")
        action = yaml.safe_load((ROOT / ".github/actions/publish-static-release/action.yml").read_text(encoding="utf-8"))
        steps = action["runs"]["steps"]
        uploads = [step for step in steps if str(step.get("uses", "")).startswith("actions/upload-artifact@")]
        self.assertEqual(len(uploads), 2)
        self.assertTrue(all(step["uses"] == "actions/upload-artifact@65c4c4a1ddee5b72f698fdd19549f0f0fb45cf08" for step in uploads))
        self.assertEqual([step["with"]["path"] for step in uploads], [
            "${{ steps.prepare.outputs.bundle_path }}", "${{ steps.finalize.outputs.manifest_path }}"])
        self.assertTrue(all(step["with"]["if-no-files-found"] == "error" and step["with"]["overwrite"] is False for step in uploads))
        self.assertEqual(uploads[0]["with"]["name"], "${{ steps.prepare.outputs.bundle_name }}")
        self.assertEqual(uploads[1]["with"]["name"], "${{ steps.finalize.outputs.manifest_name }}")
        self.assertEqual(steps[-1]["if"], "${{ always() }}")
        self.assertEqual(steps[0]['env']['ACTION_PATH'], '${{ github.action_path }}')
        self.assertIn('python3 -I "$ACTION_PATH/../../scripts/static_release.py"', steps[0]['run'])
        self.assertNotIn("${{", steps[0]["run"])

    def test_cli_prepare_finalize_cleanup_with_stubbed_git(self):
        self.posix_required()
        workspace = self.root / "workspace"
        dist = workspace / "dist"
        dist.mkdir(parents=True)
        (dist / "index.html").write_text("cli", encoding="utf-8")
        os.chdir(workspace)
        env = dict(self.env)
        class FakeGitProcess:
            def __init__(self, argv, **kwargs):
                if argv not in (["git", "rev-parse", "HEAD"], ['git', 'rev-parse', '--show-toplevel']):
                    raise AssertionError("CLI attempted a non-fixed Git command")
                if "GITHUB_TOKEN" in kwargs["env"] or "REGISTRY_TOKEN" in kwargs["env"]:
                    raise AssertionError("job credentials reached Git")
                result = SHA if argv[-1] == 'HEAD' else str(Path.cwd())
                self.stdout = io.BytesIO((result + "\n").encode("utf-8"))
                self.returncode = None
            def wait(self, timeout=None):
                self.returncode = 0
                return 0
            def poll(self):
                return self.returncode
            def kill(self):
                self.returncode = -9
            def communicate(self, *args, **kwargs):
                self.returncode = 0
                return b"", b""
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(static_release.subprocess, "Popen", FakeGitProcess):
            # Exercise the actual CLI dispatcher without executing a system Git binary.
            self.assertEqual(static_release.main(["prepare", "--asset-path", "dist"]), 0)
        rows = dict(line.split("=", 1) for line in Path(env["GITHUB_OUTPUT"]).read_text(encoding="utf-8").splitlines())
        private = Path(rows["private_dir"])
        self.assertEqual(rows["bundle_name"], "release-static-123456-2")
        # This mirrors the upload action result: a positive immutable artifact ID.
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(static_release.subprocess, "Popen", FakeGitProcess):
            self.assertEqual(static_release.main(["finalize", "--private-dir", str(private), "--artifact-id", "998877"]), 0)
            selected = release_manifest.StaticArtifactSelection('release-static-123456-2', 998877)
            manifest = release_manifest.validate_manifest((private / 'release.json').read_bytes(), expected=IDENTITY, expected_static_artifact=selected)
            self.assertEqual(release_manifest.verify_static_bundle((private / 'bundle.tar').read_bytes(), manifest=manifest)[0], (private / 'bundle.tar').stat().st_size)
            with tarfile.open(private / 'bundle.tar', 'r:') as archive:
                self.assertEqual(archive.extractfile('report/report/index.html').read(), b'cli')
            self.assertEqual(static_release.main(["cleanup", "--private-dir", str(private)]), 0)
        self.assertFalse(private.exists())

    @unittest.skipUnless(os.name == 'posix', 'POSIX source tree protections required')
    def test_real_name_bound_and_archive_cap_defense(self):
        workspace = self.root / 'workspace'
        assets = workspace / 'dist'
        assets.mkdir(parents=True)
        os.chdir(workspace)
        long_name = assets / ('a' * 101)
        long_name.write_bytes(b'data')
        with self.assertRaisesRegex(static_release.StaticReleaseError, '^asset_name_limit$'):
            static_release.prepare(self.env, 'dist', 'report/report', run=git_ok())
        long_name.unlink()
        (assets / 'index.html').write_bytes(b'data')
        # The public 48MiB payload/512-member limits already imply <64MiB tar.
        # Lower only the secondary cap to prove its guard and cleanup executes.
        with mock.patch.object(static_release, 'MAX_ARCHIVE', 1024):
            with self.assertRaisesRegex(static_release.StaticReleaseError, '^asset_archive_limit$'):
                static_release.prepare(self.env, 'dist', 'report/report', run=git_ok())
        self.assertEqual(list((self.root / 'runner-temp').iterdir()), [])

    @unittest.skipUnless(os.name == 'posix', 'POSIX source tree protections required')
    def test_subdirectory_cwd_uses_real_checkout_root_for_assets_and_output_boundary(self):
        workspace = self.root / 'workspace'
        assets = workspace / 'dist'
        assets.mkdir(parents=True)
        (assets / 'index.html').write_bytes(b'root data')
        subdir = workspace / 'subdir'
        subdir.mkdir()
        bad_temp = workspace / 'temp'
        bad_temp.mkdir()
        os.chdir(subdir)
        run = git_ok(root=workspace)
        with self.assertRaisesRegex(static_release.StaticReleaseError, '^runner_temp_invalid$'):
            static_release.prepare(dict(self.env, RUNNER_TEMP=str(bad_temp)), 'dist', 'report/report', run=run)
        self.assertEqual(list(bad_temp.iterdir()), [])
        private, _ = static_release.prepare(self.env, 'dist', 'report/report', run=run)
        with tarfile.open(private / 'bundle.tar', 'r:') as archive:
            self.assertEqual(archive.extractfile('report/report/index.html').read(), b'root data')
        static_release.cleanup(private, self.env)

    @unittest.skipUnless(os.name == 'posix', 'POSIX private output protections required')
    def test_prepare_cancellation_cleans_output_and_publishes_cleanup_path_early(self):
        workspace = self.root / 'workspace'
        assets = workspace / 'dist'
        assets.mkdir(parents=True)
        (assets / 'index.html').write_bytes(b'data')
        os.chdir(workspace)
        original_identity = static_release._identity
        def identity(env, run=None):
            return original_identity(env, run=git_ok())
        def cancelled(*args):
            rows = dict(row.split('=', 1) for row in Path(self.env['GITHUB_OUTPUT']).read_text(encoding='utf-8').splitlines())
            private = Path(rows['private_dir'])
            self.assertTrue((private / '.owned').is_file())
            self.assertNotIn('bundle_path', rows)
            raise KeyboardInterrupt
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(static_release, '_identity', identity), \
                mock.patch.object(static_release, '_checkout_root', return_value=workspace), \
                mock.patch.object(static_release, '_collect', cancelled), mock.patch('sys.stderr', new_callable=io.StringIO) as error:
            self.assertEqual(static_release.main(['prepare', '--asset-path', 'dist']), 1)
        self.assertEqual(list((self.root / 'runner-temp').iterdir()), [])
        rows = dict(row.split('=', 1) for row in Path(self.env['GITHUB_OUTPUT']).read_text(encoding='utf-8').splitlines())
        static_release.cleanup(rows['private_dir'], self.env)
        self.assertNotIn('Traceback', error.getvalue())

    @unittest.skipUnless(os.name == 'posix', 'POSIX source tree protections required')
    def test_key_control_names_are_rejected_before_descriptor_content_reads(self):
        workspace = self.root / 'workspace'
        assets = workspace / 'dist'
        assets.mkdir(parents=True)
        os.chdir(workspace)
        original = os.open
        for name in ('.env.local', 'site.pem', 'site.key', 'id_ed25519', '.ssh', 'project.yaml'):
            path = assets / name
            path.write_bytes(b'synthetic private fixture')
            def guard(candidate, flags, *args, **kwargs):
                if candidate == name:
                    raise AssertionError('forbidden content descriptor opened')
                return original(candidate, flags, *args, **kwargs)
            with mock.patch.object(static_release.os, 'open', guard):
                with self.assertRaisesRegex(static_release.StaticReleaseError, '^asset_control_file_rejected$'):
                    static_release.prepare(self.env, 'dist', 'report/report', run=git_ok())
            self.assertEqual(list((self.root / 'runner-temp').iterdir()), [])
            path.unlink()

    @unittest.skipUnless(os.name == 'posix', 'POSIX source tree protections required')
    def test_case_collisions_hardlinks_and_name_injection_fail_closed(self):
        workspace = self.root / 'workspace'
        assets = workspace / 'dist'
        assets.mkdir(parents=True)
        os.chdir(workspace)
        for names in (('File.txt', 'file.txt'), ('bad\nname.txt',), ('bad:name.txt',)):
            for name in names:
                (assets / name).write_bytes(b'data')
            with self.assertRaises(static_release.StaticReleaseError):
                static_release.prepare(self.env, 'dist', 'report/report', run=git_ok())
            for child in assets.iterdir():
                child.unlink()
        outside = workspace / 'outside.txt'
        outside.write_bytes(b'outside')
        os.link(outside, assets / 'linked.txt')
        with self.assertRaisesRegex(static_release.StaticReleaseError, '^asset_file_type_rejected$'):
            static_release.prepare(self.env, 'dist', 'report/report', run=git_ok())

    @unittest.skipUnless(os.name == 'posix', 'POSIX source tree protections required')
    def test_regular_file_replaced_by_fifo_does_not_block(self):
        workspace = self.root / 'workspace'
        assets = workspace / 'dist'
        assets.mkdir(parents=True)
        source = assets / 'index.html'
        source.write_bytes(b'before')
        os.chdir(workspace)
        collect = static_release._collect
        def replace(*args):
            result = collect(*args)
            source.unlink()
            os.mkfifo(source)
            return result
        with mock.patch.object(static_release, '_collect', replace):
            with self.assertRaisesRegex(static_release.StaticReleaseError, '^asset_changed$'):
                static_release.prepare(self.env, 'dist', 'report/report', run=git_ok())
        self.assertEqual(list((self.root / 'runner-temp').iterdir()), [])

    @unittest.skipUnless(os.name == 'posix', 'POSIX private output protections required')
    def test_prepared_identity_and_same_length_bundle_are_bound_to_finalization(self):
        workspace = self.root / 'workspace'
        dist = workspace / 'dist'
        dist.mkdir(parents=True)
        (dist / 'index.html').write_bytes(b'original')
        os.chdir(workspace)
        private, _ = static_release.prepare(self.env, 'dist', 'report/report', run=git_ok())
        changed = dict(self.env, GITHUB_RUN_ATTEMPT='3')
        with self.assertRaisesRegex(static_release.StaticReleaseError, '^source_identity_changed$'):
            static_release.finalize(changed, str(private), '123', run=git_ok())
        bundle = private / 'bundle.tar'
        data = bytearray(bundle.read_bytes())
        data[512] ^= 1
        bundle.write_bytes(data)
        with self.assertRaisesRegex(static_release.StaticReleaseError, '^bundle_changed$'):
            static_release.finalize(self.env, str(private), '123', run=git_ok())
        self.assertFalse((private / 'release.json').exists())
        static_release.cleanup(private, self.env)

    @unittest.skipUnless(os.name == 'posix', 'POSIX private output protections required')
    def test_temp_inside_checkout_is_rejected_and_cleanup_prevalidates_all_children(self):
        workspace = self.root / 'workspace'
        assets = workspace / 'dist'
        assets.mkdir(parents=True)
        (assets / 'index.html').write_bytes(b'data')
        os.chdir(workspace)
        bad = dict(self.env, RUNNER_TEMP=str(assets))
        with self.assertRaisesRegex(static_release.StaticReleaseError, '^runner_temp_invalid$'):
            static_release.prepare(bad, 'dist', 'report/report', run=git_ok())
        private, _ = static_release.prepare(self.env, 'dist', 'report/report', run=git_ok())
        extra = private / 'foreign'
        extra.write_bytes(b'preserve')
        with self.assertRaisesRegex(static_release.StaticReleaseError, '^private_output_invalid$'):
            static_release.cleanup(private, self.env)
        self.assertTrue((private / '.owned').is_file())
        self.assertEqual(extra.read_bytes(), b'preserve')
        extra.unlink()
        static_release.cleanup(private, self.env)

    @unittest.skipUnless(os.name == 'posix', 'POSIX private output protections required')
    def test_actual_action_phases_use_uploaded_bytes_id_and_always_cleanup(self):
        import yaml
        action = yaml.safe_load((ROOT / '.github/actions/publish-static-release/action.yml').read_text(encoding='utf-8'))
        steps = action['runs']['steps']
        self.assertEqual([s.get('id') for s in steps], ['prepare', 'bundle_upload', 'finalize', 'manifest_upload', None])
        workspace = self.root / 'workspace'
        dist = workspace / 'dist'
        dist.mkdir(parents=True)
        (dist / 'index.html').write_bytes(b'action harness bytes')
        os.chdir(workspace)
        original_identity = static_release._identity
        original_checkout = static_release._checkout_root
        for fail_at in (None, 'bundle_upload', 'finalize', 'manifest_upload'):
            with self.subTest(fail_at=fail_at):
                outputs = {}
                uploaded = {}
                def expand(value):
                    refs = {'inputs.release_contract': 'v1', 'inputs.release_workflow_ref': self.env['RELEASE_WORKFLOW_REF'],
                            'inputs.asset_path': 'dist', 'inputs.asset_root': 'report/report',
                            'github.action_path': str(ROOT / '.github/actions/publish-static-release')}
                    refs.update({f'steps.{key}.outputs.{name}': val for key, row in outputs.items() for name, val in row.items()})
                    if value.startswith('${{ ') and value.endswith(' }}'):
                        return refs.get(value[4:-3], '')
                    return value
                def execute(step):
                    env = dict(self.env)
                    env.update({key: expand(value) for key, value in step.get('env', {}).items()})
                    Path(env['GITHUB_OUTPUT']).write_text('', encoding='utf-8')
                    line = next(row for row in step['run'].splitlines() if row.startswith('python3 '))
                    argv = shlex.split(line)
                    self.assertEqual(argv[:2], ['python3', '-I'])
                    argv = [re.sub(r'\$([A-Z_]+)', lambda match: env[match[1]], arg) for arg in argv]
                    installed = Path(argv[2]).resolve()
                    self.assertEqual(installed, SCRIPT_DIR / 'static_release.py')
                    def fake_identity(env, run=None):
                        return original_identity(env, run=git_ok())
                    with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(static_release, '_identity', fake_identity), \
                            mock.patch.object(static_release, '_checkout_root', side_effect=lambda env, run: original_checkout(env, git_ok())):
                        code = static_release.main(argv[3:])
                    self.assertEqual(code, 0)
                    return dict(row.split('=', 1) for row in Path(env['GITHUB_OUTPUT']).read_text(encoding='utf-8').splitlines())
                try:
                    for step in steps[:-1]:
                        key = step['id']
                        if key == fail_at:
                            raise RuntimeError('synthetic action failure')
                        if 'uses' in step:
                            path = Path(expand(step['with']['path']))
                            uploaded[key] = path.read_bytes()
                            outputs[key] = {'artifact-id': '882211' if key == 'bundle_upload' else '991122'}
                        else:
                            outputs[key] = execute(step)
                    raw = uploaded['manifest_upload']
                    selected = release_manifest.StaticArtifactSelection('release-static-123456-2', 882211)
                    manifest = release_manifest.validate_manifest(raw, expected=IDENTITY, expected_static_artifact=selected)
                    self.assertEqual(release_manifest.verify_static_bundle(uploaded['bundle_upload'], manifest=manifest)[0], len(uploaded['bundle_upload']))
                    self.assertEqual(outputs['finalize']['artifact_id'], '882211')
                except RuntimeError as exc:
                    self.assertEqual(str(exc), 'synthetic action failure')
                finally:
                    self.assertEqual(steps[-1]['if'], '${{ always() }}')
                    execute(steps[-1])
                self.assertEqual(list((self.root / 'runner-temp').iterdir()), [])


if __name__ == "__main__":
    unittest.main()
