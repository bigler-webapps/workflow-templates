"""Behavioral tests for safe, offline static asset preparation."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import BytesIO, StringIO
from pathlib import Path
import stat
import sys
import tarfile
import tempfile
import unittest
import os
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parent))
import prebuilt_deploy as pd
import release_manifest as rm


IDENTITY = rm.ReleaseIdentity(
    repository="bigler-webapps/static-app",
    sha="0123456789abcdef0123456789abcdef01234567",
    ref="refs/heads/develop",
    workflow=".github/workflows/release.yml",
    run_id=123456,
    attempt=2,
)
SELECTION = rm.StaticArtifactSelection("release-static-123456-2", 998877)
ROOTS = ("report",)
SYNTHETIC_SECRET = "fixture-secret-static-do-not-print"


def make_tar(entries):
    """Build independent USTAR fixture bytes from (name, type, data, mode)."""
    output = BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, kind, data, mode in entries:
            info = tarfile.TarInfo(name)
            info.mode = mode
            if kind == "file":
                info.type = tarfile.REGTYPE
                info.size = len(data)
                archive.addfile(info, BytesIO(data))
            else:
                info.size = 0
                info.type = kind
                archive.addfile(info)
    return output.getvalue()


def manifest_for(bundle):
    return rm.produce_static_manifest(
        IDENTITY, artifact_id=SELECTION.artifact_id,
        bundle=bundle, bundle_length=len(bundle),
    )


class TrackingStream:
    def __init__(self, value):
        self.value = value
        self.offset = 0
        self.requests = []

    def read(self, size=-1):
        self.requests.append(size)
        if self.offset >= len(self.value):
            return b""
        end = min(len(self.value), self.offset + size)
        part = self.value[self.offset:end]
        self.offset = end
        return part


class PrebuiltDeployTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.parent = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, bundle, *, raw=None, expected=IDENTITY, selection=SELECTION, roots=ROOTS, stream=None):
        return pd.prepare_static_assets(
            manifest_for(bundle) if raw is None else raw,
            expected_identity=expected,
            selected_artifact=selection,
            bundle=bundle if stream is None else stream,
            allowed_asset_roots=roots,
            destination_parent=self.parent,
        )

    def assert_rejected(self, bundle, code=None, **kwargs):
        before = set(self.parent.iterdir())
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(pd.PreparationError) as caught:
                self.prepare(bundle, **kwargs)
        if code is not None:
            self.assertEqual(str(caught.exception), code)
        self.assertEqual(set(self.parent.iterdir()), before)
        diagnostic = str(caught.exception) + stdout.getvalue() + stderr.getvalue()
        self.assertNotIn(SYNTHETIC_SECRET, diagnostic)
        self.assertNotIn("report/", diagnostic)

    def test_positive_assets_are_exact_and_created_with_private_safe_modes(self):
        bundle = make_tar([
            ("report", tarfile.DIRTYPE, b"", 0o755),
            ("report/index.html", "file", b"<p>static</p>", 0o644),
            ("report/app.js", "file", b"const value = 7;", 0o644),
        ])
        stream = TrackingStream(bundle)
        result = self.prepare(bundle, stream=stream)
        self.assertEqual((result / "report" / "index.html").read_bytes(), b"<p>static</p>")
        self.assertEqual((result / "report" / "app.js").read_bytes(), b"const value = 7;")
        self.assertTrue(stat.S_ISDIR(result.lstat().st_mode))
        self.assertFalse(result.is_symlink())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(result.stat().st_mode) & 0o077, 0)
            self.assertEqual(stat.S_IMODE((result / "report" / "index.html").stat().st_mode) & 0o077, 0)
        self.assertLessEqual(max(stream.requests), pd.IO_CHUNK_BYTES)

    def test_source_sha_run_and_independent_artifact_mismatch_precede_writes(self):
        bundle = make_tar([("report/index.html", "file", b"hello", 0o644)])
        raw = manifest_for(bundle)
        with patch.object(pd, "_create_private_directory", side_effect=AssertionError("must not create")):
            self.assert_rejected(bundle, "release_identity_mismatch", raw=raw,
                                 expected=replace(IDENTITY, sha="f" * 40))
            self.assert_rejected(bundle, "static_artifact_mismatch", raw=raw,
                                 selection=rm.StaticArtifactSelection(SELECTION.name, SELECTION.artifact_id + 1))
            self.assert_rejected(bundle, "release_identity_mismatch", raw=raw,
                                 expected=replace(IDENTITY, run_id=IDENTITY.run_id + 1))

    def test_bundle_hash_mismatch_precedes_destination_creation(self):
        original = make_tar([("report/a.txt", "file", b"first", 0o644)])
        altered = make_tar([("report/a.txt", "file", b"other", 0o644)])
        self.assertEqual(len(original), len(altered))
        with patch.object(pd, "_create_private_directory", side_effect=AssertionError("must not create")):
            self.assert_rejected(altered, "static_bundle_hash_mismatch", raw=manifest_for(original))

    def test_rejects_traversal_absolute_backslash_drive_and_control_paths(self):
        for name in (
            "report/../outside.txt", "/report/index.html", "report\\index.html",
            "C:/report/index.html", "report/.env.production", "report/.git/config",
            "report/.github/workflows/evil.yml", "report/docker-compose.yml",
            "report/project.yaml", "report/secrets.yaml", "report/Dockerfile",
            "report/docker-compose.prod.yml", "report/project.json", "report/secrets.json", "report/action.yml",
            "report/Caddyfile", "report/run.sh", "report/setup.py",
            "REPORT/index.html",
        ):
            with self.subTest(name=name):
                bundle = make_tar([(name, "file", SYNTHETIC_SECRET.encode(), 0o644)])
                self.assert_rejected(bundle)

    def test_rejects_symlink_hardlink_fifo_sparse_and_executable_entries(self):
        rejected_types = (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.GNUTYPE_SPARSE)
        for entry_type in rejected_types:
            with self.subTest(entry_type=entry_type):
                bundle = make_tar([("report/link", entry_type, b"", 0o644)])
                self.assert_rejected(bundle, "archive_member_type_unsupported")
        executable = make_tar([("report/run.sh", "file", b"echo no", 0o755)])
        self.assert_rejected(executable, "archive_executable_file")

    def test_rejects_duplicate_casefolded_and_parent_child_collisions(self):
        fixtures = (
            [("report/a.txt", "file", b"one", 0o644), ("report/a.txt", "file", b"two", 0o644)],
            [("report/a.txt", "file", b"one", 0o644), ("report/A.TXT", "file", b"two", 0o644)],
            [("report/a", "file", b"one", 0o644), ("report/a/b.txt", "file", b"two", 0o644)],
            [("report/a/b.txt", "file", b"two", 0o644), ("report/a", "file", b"one", 0o644)],
            [("report", tarfile.DIRTYPE, b"", 0o755), ("report", tarfile.DIRTYPE, b"", 0o755)],
        )
        for entries in fixtures:
            with self.subTest(entries=entries):
                self.assert_rejected(make_tar(entries), "archive_path_collision")

    def test_rejects_header_file_member_total_and_actual_stream_limits(self):
        one = make_tar([("report/a", "file", b"12345", 0o644)])
        with patch.object(pd, "MAX_FILE_BYTES", 4):
            self.assert_rejected(one, "archive_file_limit")
        two = make_tar([
            ("report/a", "file", b"123", 0o644),
            ("report/b", "file", b"456", 0o644),
        ])
        with patch.object(pd, "MAX_EXTRACTED_BYTES", 5):
            self.assert_rejected(two, "archive_total_limit")
        three = make_tar([
            ("report/a", "file", b"1", 0o644), ("report/b", "file", b"2", 0o644),
            ("report/c", "file", b"3", 0o644),
        ])
        with patch.object(pd, "MAX_MEMBERS", 2):
            self.assert_rejected(three, "archive_member_limit")

        class OverlongStream:
            def __init__(self):
                self.requested = []

            def read(self, size):
                self.requested.append(size)
                return b"x" * (size + 1)

        stream = OverlongStream()
        with self.assertRaises(pd.PreparationError) as caught:
            self.prepare(one, stream=stream)
        self.assertEqual(str(caught.exception), "bundle_stream_invalid")
        self.assertEqual(len(stream.requested), 1)
        self.assertLessEqual(stream.requested[0], pd.IO_CHUNK_BYTES)
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_rejects_compressed_bad_and_truncated_archives(self):
        valid = make_tar([("report/a", "file", b"x", 0o644)])
        fixtures = (
            __import__("gzip").compress(valid),
            b"not a tar archive",
            valid[:1536],
        )
        expected_codes = ("archive_format_unsupported", "archive_format_unsupported", "archive_terminator_invalid")
        for bundle, code in zip(fixtures, expected_codes):
            with self.subTest(code=code):
                self.assert_rejected(bundle, code)

    def test_preexisting_parent_contents_and_symlink_are_preserved(self):
        existing = self.parent / "static-assets-existing"
        existing.mkdir()
        marker = existing / "trusted.txt"
        marker.write_text("keep", encoding="utf-8")
        bundle = make_tar([("report/a.txt", "file", b"ok", 0o644)])
        output = self.prepare(bundle)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        self.assertNotEqual(output, existing)

        link = self.parent / "linked-parent"
        try:
            link.symlink_to(existing, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlink creation unavailable")
        with self.assertRaises(pd.PreparationError) as caught:
            pd.prepare_static_assets(
                manifest_for(bundle), expected_identity=IDENTITY, selected_artifact=SELECTION,
                bundle=bundle, allowed_asset_roots=ROOTS, destination_parent=link,
            )
        self.assertEqual(str(caught.exception), "destination_parent_invalid")
        self.assertTrue(link.is_symlink())
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_late_write_error_removes_created_tree_and_hides_io_details(self):
        bundle = make_tar([
            ("report/first.txt", "file", b"one", 0o644),
            ("report/second.txt", "file", SYNTHETIC_SECRET.encode(), 0o644),
        ])
        created = []
        original_create = pd._create_private_directory
        original_write = pd._write_file

        def capture_create(parent):
            result = original_create(parent)
            created.append(result)
            return result

        calls = 0

        def fail_late(root, archive, member):
            nonlocal calls
            calls += 1
            original_write(root, archive, member)
            if calls == 2:
                raise OSError(SYNTHETIC_SECRET)

        stdout, stderr = StringIO(), StringIO()
        with patch.object(pd, "_create_private_directory", side_effect=capture_create), \
                patch.object(pd, "_write_file", side_effect=fail_late), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(pd.PreparationError) as caught:
                self.prepare(bundle)
        self.assertEqual(str(caught.exception), "asset_write_failed")
        self.assertTrue(created)
        self.assertFalse(created[0].exists())
        diagnostic = str(caught.exception) + stdout.getvalue() + stderr.getvalue()
        self.assertNotIn(SYNTHETIC_SECRET, diagnostic)
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_manifest_bytes_are_required_and_stream_errors_are_sanitized(self):
        bundle = make_tar([("report/a", "file", b"ok", 0o644)])
        with self.assertRaises(pd.PreparationError) as caught:
            pd.prepare_static_assets(
                {"kind": "static"}, expected_identity=IDENTITY, selected_artifact=SELECTION,
                bundle=bundle, allowed_asset_roots=ROOTS, destination_parent=self.parent,
            )
        self.assertEqual(str(caught.exception), "manifest_bytes_required")

        class BrokenStream:
            def read(self, size):
                raise OSError(SYNTHETIC_SECRET)

        with self.assertRaises(pd.PreparationError) as caught:
            self.prepare(bundle, stream=BrokenStream())
        self.assertEqual(str(caught.exception), "bundle_read_failed")
        self.assertNotIn(SYNTHETIC_SECRET, str(caught.exception))
        self.assertEqual(list(self.parent.iterdir()), [])


    def test_multichunk_assets_and_late_invalid_members_have_no_partial_publication(self):
        payload = b"static browser data " * 9000
        bundle = make_tar([("report/large.js", "file", payload, 0o644)])
        stream = TrackingStream(bundle)
        output = self.prepare(bundle, stream=stream)
        self.assertEqual((output/"report/large.js").read_bytes(), payload)
        self.assertGreater(len(stream.requests), 2)
        self.assertTrue(all(0 < n <= 65536 for n in stream.requests))
        bad = make_tar([("report/good.txt", "file", b"ok", 0o644), ("report/../bad", "file", SYNTHETIC_SECRET.encode(), 0o644)])
        with patch.object(pd, "_create_private_directory", side_effect=AssertionError("must preflight all members")):
            self.assert_rejected(bad)

    def test_cleanup_failure_is_explicit_and_never_a_success_or_raw_error(self):
        bundle = make_tar([("report/one.txt", "file", b"ok", 0o644)])
        with patch.object(pd, "_write_file", side_effect=OSError(SYNTHETIC_SECRET)), patch.object(pd.shutil,"rmtree", side_effect=OSError(SYNTHETIC_SECRET)):
            with self.assertRaisesRegex(pd.PreparationError, "asset_cleanup_failed") as caught:
                self.prepare(bundle)
        self.assertNotIn(SYNTHETIC_SECRET, str(caught.exception))
        self.assertEqual(len(list(self.parent.iterdir())), 1)


if __name__ == "__main__":
    unittest.main()
