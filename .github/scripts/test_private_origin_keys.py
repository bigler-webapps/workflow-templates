"""Synthetic custody, receiver, and action-binding tests for S456."""

from __future__ import annotations

import importlib.util
import errno
import selectors
import shlex
import time
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).with_name("private_origin_keys.py")
ACTION = SCRIPT.parents[1] / "actions" / "deploy-traefik" / "action.yml"
SPEC = importlib.util.spec_from_file_location("private_origin_keys_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
private_origin_keys = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(private_origin_keys)


def _acl(entries: list[tuple[int, int, int]]) -> bytes:
    return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *item) for item in entries)


class PrivateOriginKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="origin-key-test-")
        self.root = Path(self.temp.name)
        os.chmod(self.root, 0o700)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(mode=0o700)
        self.keys = self.root / "private-keys"
        self.keys.mkdir(mode=0o700)
        self.certs = self.workspace / "certs"
        self.certs.mkdir(mode=0o700)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _key(self, name: str = "example-test.key", payload: bytes = b"synthetic-invalid-key") -> Path:
        path = self.keys / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
        os.chmod(path, 0o600)
        return path

    def _run_receiver(self, *, keys: list[tuple[str, bytes]], destination: Path,
                      truncate: bool = False) -> tuple[subprocess.CompletedProcess[bytes], str]:
        header = private_origin_keys._header(
            str(destination), [(name, payload) for name, payload in keys])
        command = [sys.executable, "-I", "-c", private_origin_keys._REMOTE_RECEIVER,
                   "--", str(destination)]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(header)
        process.stdin.flush()
        with selectors.DefaultSelector() as selected:
            selected.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(selected.select(3), "local receiver must respond")
            ready = process.stdout.readline(1025)
        ready_value = json.loads(ready)
        if ready_value.get("status") == "ready" and not truncate:
            for _, payload in keys:
                process.stdin.write(payload)
        process.stdin.close()
        process.stdin = None
        rest, stderr = process.communicate(timeout=3)
        code = process.returncode
        return subprocess.CompletedProcess(command, code, ready + rest, stderr), ready.decode()

    def test_empty_source_is_a_noop_but_legacy_key_blocks_before_any_transfer(self) -> None:
        private_origin_keys.preflight("", str(self.workspace))
        self.assertEqual(private_origin_keys._validate_request("", str(self.workspace)), [])
        legacy = self.certs / "legacy.test.key"
        legacy.write_bytes(b"synthetic-marker")
        with self.assertRaises(private_origin_keys.OriginKeyError) as caught:
            private_origin_keys.preflight("", str(self.workspace))
        self.assertEqual(caught.exception.code, "legacy_key_present")
        self.assertNotIn(b"synthetic-marker", str(caught.exception).encode())

    def test_source_requires_private_owner_only_mode_and_valid_names(self) -> None:
        self._key()
        private_origin_keys.preflight(str(self.keys), str(self.workspace))
        os.chmod(self.keys, 0o750)
        with self.assertRaises(private_origin_keys.OriginKeyError) as caught:
            private_origin_keys.preflight(str(self.keys), str(self.workspace))
        self.assertEqual(caught.exception.code, "directory_invalid")
        os.chmod(self.keys, 0o700)
        invalid = self.keys / "Upper.key"
        invalid.write_bytes(b"synthetic-invalid-key")
        os.chmod(invalid, 0o600)
        with self.assertRaises(private_origin_keys.OriginKeyError) as caught:
            private_origin_keys.preflight(str(self.keys), str(self.workspace))
        self.assertEqual(caught.exception.code, "source_names_invalid")
        invalid.unlink()

    def test_nonempty_source_directory_must_contain_a_key(self) -> None:
        with self.assertRaises(private_origin_keys.OriginKeyError) as caught:
            private_origin_keys.preflight(str(self.keys), str(self.workspace))
        self.assertEqual(caught.exception.code, "source_empty")

    def test_empty_private_input_allows_public_cert_directory_with_broad_acls(self):
        (self.certs / "public.crt").write_bytes(b"synthetic-public-certificate")
        broad = _acl([(1, 7, 0), (2, 4, 12345), (4, 5, 0),
                      (16, 7, 0), (32, 5, 0)])
        os.setxattr(self.certs, "system.posix_acl_access", broad)
        os.setxattr(self.certs, "system.posix_acl_default", broad)
        with patch.object(private_origin_keys.os, "read") as read, \
                patch.object(private_origin_keys.subprocess, "Popen") as process:
            private_origin_keys.preflight("", str(self.workspace))
            private_origin_keys.transfer("", "target.example", "deploy",
                                         "/srv/example", str(self.workspace))
        read.assert_not_called()
        process.assert_not_called()
        # The same ACLs must never allow a loose private filename into rsync.
        (self.certs / "legacy.key").write_bytes(b"synthetic-invalid-key")
        with self.assertRaises(private_origin_keys.OriginKeyError) as caught:
            private_origin_keys.preflight("", str(self.workspace))
        self.assertEqual(caught.exception.code, "legacy_key_present")

    def test_symlink_hardlink_and_oversize_inputs_refuse(self) -> None:
        source = self._key()
        os.link(source, self.keys / "linked.key")
        with self.assertRaises(private_origin_keys.OriginKeyError):
            private_origin_keys.preflight(str(self.keys), str(self.workspace))
        (self.keys / "linked.key").unlink()
        source.unlink()
        os.symlink(self.root / "outside", self.keys / "example-test.key")
        with self.assertRaises(private_origin_keys.OriginKeyError):
            private_origin_keys.preflight(str(self.keys), str(self.workspace))
        (self.keys / "example-test.key").unlink()
        self._key(payload=b"x" * (private_origin_keys.MAX_KEY_BYTES + 1))
        with self.assertRaises(private_origin_keys.OriginKeyError):
            private_origin_keys.preflight(str(self.keys), str(self.workspace))

    def test_effective_named_user_access_acl_is_rejected_before_transport(self) -> None:
        source = self._key()
        # Linux POSIX ACL xattrs are required evidence; unsupported ACL storage
        # fails this test and cannot be treated as a privacy result.
        os.setxattr(source, "system.posix_acl_access", _acl([
            (1, 6, 0), (2, 4, 12345), (4, 0, 0), (16, 4, 0), (32, 0, 0),
        ]))
        with self.assertRaises(private_origin_keys.OriginKeyError) as caught:
            private_origin_keys.preflight(str(self.keys), str(self.workspace))
        self.assertIn(caught.exception.code, {"source_file_invalid", "acl_broad"})

    def test_default_acl_is_rejected_before_source_bytes_are_read(self) -> None:
        self._key()
        os.setxattr(self.keys, "system.posix_acl_default", _acl([
            (1, 7, 0), (2, 4, 12345), (4, 0, 0), (16, 4, 0), (32, 0, 0),
        ]))
        with self.assertRaises(private_origin_keys.OriginKeyError) as caught:
            private_origin_keys.preflight(str(self.keys), str(self.workspace))
        self.assertEqual(caught.exception.code, "acl_default_present")

    def test_receiver_installs_atomically_with_private_owner_only_access(self) -> None:
        keys = [("example-test.key", b"synthetic-invalid-key")]
        result, ready = self._run_receiver(keys=keys, destination=self.certs)
        self.assertEqual({key: value for key, value in json.loads(ready).items()
                          if key != "nonce"}, {"status": "ready", "count": 1})
        lines = result.stdout.splitlines()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(lines[-1]), {
            "status": "complete", "count": 1, "names": ["example-test.key"],
            "nonce": json.loads(ready)["nonce"]})
        target = self.certs / "example-test.key"
        self.assertEqual(target.read_bytes(), keys[0][1])
        info = target.stat(follow_symlinks=False)
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(info.st_uid, os.geteuid())
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        self.assertEqual(sorted(item.name for item in self.certs.iterdir()), [target.name])

    def test_receiver_rejects_effective_default_acl_before_ready(self) -> None:
        os.setxattr(self.certs, "system.posix_acl_default", _acl([
            (1, 7, 0), (2, 4, 12345), (4, 0, 0), (16, 4, 0), (32, 0, 0),
        ]))
        result, ready = self._run_receiver(
            keys=[("example-test.key", b"synthetic-invalid-key")], destination=self.certs)
        self.assertEqual(result.returncode, 2)
        self.assertNotEqual(json.loads(ready).get("status"), "ready")
        self.assertFalse((self.certs / "example-test.key").exists())

    def test_receiver_rejects_effective_named_group_acl_before_ready(self) -> None:
        os.setxattr(self.certs, "system.posix_acl_access", _acl([
            (1, 7, 0), (4, 0, 0), (8, 4, 12345), (16, 4, 0), (32, 0, 0),
        ]))
        result, ready = self._run_receiver(
            keys=[("example-test.key", b"synthetic-invalid-key")], destination=self.certs)
        self.assertEqual(result.returncode, 2)
        self.assertNotEqual(json.loads(ready).get("status"), "ready")
        self.assertFalse((self.certs / "example-test.key").exists())

    def test_receiver_truncation_cleans_only_its_temp_file(self) -> None:
        preexisting = self.certs / "other.key"
        preexisting.write_bytes(b"keep-me")
        os.chmod(preexisting, 0o600)
        result, ready = self._run_receiver(
            keys=[("example-test.key", b"synthetic-invalid-key")],
            destination=self.certs, truncate=True)
        self.assertEqual(json.loads(ready).get("status"), "ready")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.certs / "example-test.key").exists())
        self.assertEqual(preexisting.read_bytes(), b"keep-me")
        self.assertEqual(sorted(path.name for path in self.certs.iterdir()), ["other.key"])

    def test_actual_transfer_flow_uses_fixed_ssh_argv_and_local_receiver(self) -> None:
        self._key()
        observed: list[list[str]] = []
        real_popen = subprocess.Popen

        def local_ssh(argv: list[str], **kwargs):
            observed.append(argv)
            self.assertEqual(argv[0], "/usr/bin/ssh")
            self.assertEqual(argv[4], "deploy@target.example")
            self.assertIn("python3 -I -c", argv[-1])
            self.assertNotIn(b"synthetic-invalid-key", " ".join(argv).encode())
            destination = argv[-1].rsplit(" -- ", 1)[1].strip("'")
            return real_popen(
                [sys.executable, "-I", "-c", private_origin_keys._REMOTE_RECEIVER,
                 "--", destination], **kwargs)

        with patch.object(private_origin_keys.subprocess, "Popen", side_effect=local_ssh):
            private_origin_keys.transfer(str(self.keys), "target.example", "deploy",
                                         str(self.workspace), str(self.workspace), timeout=3)
        self.assertEqual(len(observed), 1)
        self.assertTrue((self.certs / "example-test.key").exists())

    def test_route_rejects_shell_metacharacters_before_process_creation(self) -> None:
        self._key()
        with patch.object(private_origin_keys.subprocess, "Popen") as process:
            with self.assertRaises(private_origin_keys.OriginKeyError):
                private_origin_keys.transfer(str(self.keys), "target.example;id", "deploy",
                                             "/srv/example", str(self.workspace))
        process.assert_not_called()

    def test_effective_named_group_acl_is_rejected(self) -> None:
        source = self._key()
        os.setxattr(source, "system.posix_acl_access", _acl([
            (1, 6, 0), (4, 0, 0), (8, 4, 12345), (16, 4, 0), (32, 0, 0),
        ]))
        fd = os.open(source, os.O_RDONLY)
        try:
            with self.assertRaises(private_origin_keys.OriginKeyError) as caught:
                private_origin_keys._check_private_acl(
                    fd, stat.S_IMODE(os.fstat(fd).st_mode), directory=False)
            self.assertEqual(caught.exception.code, "acl_broad")
        finally:
            os.close(fd)

    def test_owner_only_posix_acl_is_accepted(self) -> None:
        source = self._key()
        os.setxattr(source, "system.posix_acl_access", _acl([
            (1, 6, 0), (4, 0, 0), (16, 0, 0), (32, 0, 0),
        ]))
        private_origin_keys.preflight(str(self.keys), str(self.workspace))

    def test_maximum_batch_boundary_and_over_count_refusal(self) -> None:
        for index in range(private_origin_keys.MAX_KEYS):
            self._key(f"site-{index:02d}.key",
                      b"x" * private_origin_keys.MAX_KEY_BYTES)
        keys, _ = private_origin_keys._source_payload(str(self.keys))
        self.assertEqual(len(keys), private_origin_keys.MAX_KEYS)
        self.assertEqual(sum(len(data) for _, data in keys),
                         private_origin_keys.MAX_TOTAL_BYTES)
        self._key("site-overflow.key", b"x")
        with self.assertRaises(private_origin_keys.OriginKeyError) as caught:
            private_origin_keys.preflight(str(self.keys), str(self.workspace))
        self.assertEqual(caught.exception.code, "source_names_invalid")

    def test_action_binds_trusted_helper_and_excludes_key_payloads_from_rsync(self) -> None:
        source = ACTION.read_text(encoding="utf-8")
        self.assertIn("origin_keys_path:", source)
        self.assertEqual(source.count("$GITHUB_ACTION_PATH/../../scripts/private_origin_keys.py"), 2)
        self.assertIn("--exclude '/certs/*.key'", source)
        self.assertLess(source.index("Validate private origin-key custody"),
                        source.index("Connect to tailnet"))
        self.assertLess(source.index("Rsync project to server"),
                        source.index("Transfer private origin keys"))
        self.assertLess(source.index("Transfer private origin keys"),
                        source.index("Start containers"))
        self.assertIn("--legacy-root \"$GITHUB_WORKSPACE\"", source)
        self.assertNotIn("secrets_context_json", source[source.index("Transfer private origin keys"):])

    def _receiver(self, keys, *, timeout_ms=2000):
        header = private_origin_keys._header(str(self.certs), keys,
                                            timeout_ms=timeout_ms)
        process = subprocess.Popen(
            [sys.executable, "-I", "-c", private_origin_keys._REMOTE_RECEIVER,
             "--", str(self.certs)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self._stop, process)
        process.stdin.write(header)
        process.stdin.flush()
        with selectors.DefaultSelector() as selected:
            selected.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(selected.select(3))
            ready = json.loads(process.stdout.readline(1025))
        return process, ready

    @staticmethod
    def _stop(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()

    def test_real_source_fifo_refuses_without_blocking(self):
        os.mkfifo(self.keys / "fifo.key", 0o600)
        started = time.monotonic()
        with self.assertRaises(private_origin_keys.OriginKeyError):
            private_origin_keys.preflight(str(self.keys), str(self.workspace))
        self.assertLess(time.monotonic() - started, 2)

    def test_unsupported_acl_facility_refuses_before_file_bytes(self):
        self._key()
        with patch.object(private_origin_keys.os, "getxattr",
                          side_effect=OSError(errno.ENOTSUP, "not supported")), \
                patch.object(private_origin_keys.os, "read") as read:
            with self.assertRaises(private_origin_keys.OriginKeyError) as caught:
                private_origin_keys.preflight(str(self.keys), str(self.workspace))
        self.assertEqual(caught.exception.code, "acl_unverifiable")
        read.assert_not_called()

    def test_masked_named_acl_is_refused_instead_of_assuming_mode_is_complete(self):
        source = self._key()
        os.setxattr(source, "system.posix_acl_access", _acl([
            (1, 6, 0), (2, 4, 12345), (4, 0, 0), (16, 0, 0), (32, 0, 0)]))
        self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o600)
        with self.assertRaises(private_origin_keys.OriginKeyError) as caught:
            private_origin_keys.preflight(str(self.keys), str(self.workspace))
        self.assertEqual(caught.exception.code, "acl_broad")

    def test_source_in_checkout_and_parent_symlinks_refuse(self):
        nested = self.workspace / "nested"
        nested.mkdir(mode=0o700)
        with self.assertRaises(private_origin_keys.OriginKeyError) as caught:
            private_origin_keys.preflight(str(nested), str(self.workspace))
        self.assertEqual(caught.exception.code, "source_must_be_separate")
        alias = self.root / "alias"
        alias.symlink_to(self.keys, target_is_directory=True)
        with self.assertRaises(private_origin_keys.OriginKeyError):
            private_origin_keys.preflight(str(alias), str(self.workspace))

    def test_dot_domain_key_name_remains_supported(self):
        self._key("example.test.key")
        private_origin_keys.preflight(str(self.keys), str(self.workspace))

    def test_target_whole_batch_preflight_precedes_any_key_payload(self):
        existing = self.certs / "z.key"
        existing.write_bytes(b"preserve-synthetic-original")
        existing.chmod(0o644)
        process, ready = self._receiver([("a.key", b"invalid-a"), ("z.key", b"invalid-z")])
        self.assertEqual(ready["status"], "refused")
        process.stdin.close()
        process.stdin = None
        process.communicate(timeout=3)
        self.assertEqual(process.returncode, 2)
        self.assertFalse((self.certs / "a.key").exists())
        self.assertEqual(existing.read_bytes(), b"preserve-synthetic-original")

    def test_target_links_and_special_files_are_never_followed_or_overwritten(self):
        original = self.root / "outside.key"
        original.write_bytes(b"synthetic-outside")
        original.chmod(0o600)
        for kind in ("symlink", "hardlink", "fifo"):
            with self.subTest(kind=kind):
                target = self.certs / "a.key"
                if kind == "symlink":
                    target.symlink_to(original)
                elif kind == "hardlink":
                    os.link(original, target)
                else:
                    os.mkfifo(target, 0o600)
                process, ready = self._receiver([("a.key", b"invalid")])
                self.assertEqual(ready["status"], "refused")
                process.stdin.close()
                process.stdin = None
                process.communicate(timeout=3)
                self.assertEqual(original.read_bytes(), b"synthetic-outside")
                target.unlink()

    def test_corrupt_second_payload_commits_none_and_removes_only_owned_staging(self):
        original = self.certs / "a.key"
        original.write_bytes(b"keep-original")
        original.chmod(0o600)
        unrelated = self.certs / "unrelated.key"
        unrelated.write_bytes(b"keep-unrelated")
        unrelated.chmod(0o600)
        process, ready = self._receiver([("a.key", b"new-invalid-a"),
                                         ("z.key", b"new-invalid-z")])
        self.assertEqual(ready["status"], "ready")
        output, error = process.communicate(b"new-invalid-a" + b"bad-invalid-z", timeout=3)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(original.read_bytes(), b"keep-original")
        self.assertEqual(unrelated.read_bytes(), b"keep-unrelated")
        self.assertEqual(sorted(path.name for path in self.certs.iterdir()),
                         ["a.key", "unrelated.key"])
        self.assertNotIn(b"new-invalid-a", output + error)

    def test_extra_payload_after_complete_frame_prevents_any_commit(self):
        process, ready = self._receiver([("a.key", b"invalid")])
        output, error = process.communicate(b"invalid-extra", timeout=3)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(list(self.certs.iterdir()), [])
        self.assertNotIn(b"invalid-extra", output + error)

    def _wait_for_staging(self, process):
        end = time.monotonic() + 2
        while time.monotonic() < end:
            names = list(self.certs.glob(".origin-key-*.tmp"))
            if names:
                return names[0]
            self.assertIsNone(process.poll())
            time.sleep(0.01)
        self.fail("receiver did not stage the first synthetic item")

    def test_catchable_cancel_cleans_owned_temp_and_preserves_original(self):
        original = self.certs / "a.key"
        original.write_bytes(b"keep-original")
        original.chmod(0o600)
        process, ready = self._receiver([("a.key", b"invalid-a"), ("z.key", b"invalid-z")])
        process.stdin.write(b"invalid-a")
        process.stdin.flush()
        self._wait_for_staging(process)
        process.terminate()
        process.wait(timeout=3)
        self.assertEqual(sorted(path.name for path in self.certs.iterdir()), ["a.key"])
        self.assertEqual(original.read_bytes(), b"keep-original")

    def test_cancel_cleanup_does_not_unlink_another_inode_at_the_temp_name(self):
        process, ready = self._receiver([("a.key", b"invalid-a"), ("z.key", b"invalid-z")])
        process.stdin.write(b"invalid-a")
        process.stdin.flush()
        owned = self._wait_for_staging(process)
        moved = self.root / "moved-owned-temp"
        owned.rename(moved)
        owned.write_bytes(b"different-owned-synthetic-file")
        process.terminate()
        process.wait(timeout=3)
        self.assertEqual(owned.read_bytes(), b"different-owned-synthetic-file")
        self.assertTrue(moved.exists())
        self.assertFalse((self.certs / "a.key").exists())

    def test_receiver_deadline_without_payload_exits_and_leaves_no_temp(self):
        process, ready = self._receiver([("a.key", b"invalid")], timeout_ms=50)
        process.wait(timeout=3)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(list(self.certs.iterdir()), [])

    def test_closed_receipt_rejects_duplicates_bool_extra_and_stale_nonce(self):
        expected = {"status": "ready", "count": 1, "nonce": "a" * 32}
        for raw in (b'{"status":"ready","count":true,"nonce":"' + b"a"*32 + b'"}\n',
                    b'{"status":"ready","count":0,"count":1,"nonce":"' + b"a"*32 + b'"}\n',
                    json.dumps(dict(expected, nonce="b"*32)).encode()+b"\n",
                    json.dumps(dict(expected, extra=True)).encode()+b"\n"):
            with self.subTest(raw=raw):
                with self.assertRaises(private_origin_keys.OriginKeyError) as caught:
                    private_origin_keys._parse_line(raw, expected, "ready_invalid")
                self.assertTrue(caught.exception.ambiguous)

    def test_deadline_includes_preflight_and_prevents_process_after_local_expiry(self):
        self._key()
        original = private_origin_keys._validate_request
        def slow(*args, **kwargs):
            result = original(*args, **kwargs)
            time.sleep(0.03)
            return result
        with patch.object(private_origin_keys, "_validate_request", side_effect=slow), \
                patch.object(private_origin_keys.subprocess, "Popen") as process:
            with self.assertRaises(private_origin_keys.OriginKeyError) as caught:
                private_origin_keys.transfer(str(self.keys), "target.example", "deploy",
                                             str(self.workspace), str(self.workspace),
                                             timeout=0.01)
        self.assertEqual(caught.exception.code, "deadline_exceeded")
        process.assert_not_called()

    def test_action_helper_resolves_from_actual_composite_action_directory(self):
        import yaml
        action = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
        selected = [step for step in action["runs"]["steps"]
                    if step["name"] in ("Validate private origin-key custody",
                                        "Transfer private origin keys")]
        self.assertEqual(len(selected), 2)
        for step in selected:
            invocation = next(line for line in step["run"].splitlines()
                              if "private_origin_keys.py" in line)
            arguments = shlex.split(invocation.rstrip().removesuffix("\\"))
            self.assertEqual(arguments[:2], ["python3", "-I"])
            argument = arguments[2]
            resolved = Path(argument.replace("$GITHUB_ACTION_PATH", str(ACTION.parent)))
            self.assertEqual(resolved.resolve(), SCRIPT.resolve())
        self.assertEqual(selected[0]["env"],
                         {"INPUT_ORIGIN_KEYS_PATH": "$" + "{{ inputs.origin_keys_path }}"})

    def _client_exchange(self, *, corruption=None, timeout=3):
        self._key("a.key", b"synthetic-invalid-a")
        self._key("z.test.key", b"synthetic-invalid-z")
        caller_checkout = self.root / "client-checkout"
        caller_checkout.mkdir(mode=0o700, exist_ok=True)
        real_popen = subprocess.Popen
        calls = []
        def local(argv, **kwargs):
            calls.append(argv)
            self.assertEqual(kwargs["env"], {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
            self.assertIs(kwargs["shell"], False)
            self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
            code = private_origin_keys._REMOTE_RECEIVER
            if corruption == "lost":
                code = code.replace('emit({"status":"complete",', 'raise SystemExit(0)\n    emit({"status":"complete",')
            elif corruption == "nonce":
                code = code.replace('"names":names,"nonce":header["nonce"]',
                                    '"names":names,"nonce":"0"*32')
            elif corruption == "late":
                code = code.replace('emit({"status":"complete",',
                                    'time.sleep(2)\n    emit({"status":"complete",')
            return real_popen([sys.executable, "-I", "-c", code,
                               "--", str(self.certs)], **kwargs)
        with patch.object(private_origin_keys.subprocess, "Popen", side_effect=local):
            try:
                private_origin_keys.transfer(str(self.keys), "target.example", "deploy",
                                             str(self.workspace), str(caller_checkout),
                                             timeout=timeout)
            finally:
                self.assertEqual(len(calls), 1, "no automatic retry")

    def test_actual_multi_key_transport_replaces_only_named_files(self):
        original = self.certs / "a.key"
        original.write_bytes(b"previous-synthetic-key")
        original.chmod(0o600)
        unrelated = self.certs / "keep.key"
        unrelated.write_bytes(b"preserve-unrelated")
        unrelated.chmod(0o600)
        self._client_exchange()
        self.assertEqual(original.read_bytes(), b"synthetic-invalid-a")
        self.assertEqual((self.certs / "z.test.key").read_bytes(), b"synthetic-invalid-z")
        self.assertEqual(unrelated.read_bytes(), b"preserve-unrelated")
        self.assertEqual(stat.S_IMODE(original.stat().st_mode), 0o600)
        self.assertEqual(sorted(path.name for path in self.certs.iterdir()),
                         ["a.key", "keep.key", "z.test.key"])

    def test_maximum_name_count_size_batch_has_accepted_completion_receipt(self):
        names = ["k%02d-" % index + "x"*120 + ".key" for index in range(32)]
        self.assertTrue(all(len(name) == 128 for name in names))
        for name in names:
            self._key(name, b"x" * 65536)
        caller_checkout = self.root / "max-caller"
        caller_checkout.mkdir(mode=0o700)
        real_popen = subprocess.Popen
        calls = []
        def local(argv, **kwargs):
            calls.append(argv)
            return real_popen([sys.executable, "-I", "-c",
                               private_origin_keys._REMOTE_RECEIVER,
                               "--", str(self.certs)], **kwargs)
        with patch.object(private_origin_keys.subprocess, "Popen", side_effect=local):
            private_origin_keys.transfer(str(self.keys), "target.example", "deploy",
                                         str(self.workspace), str(caller_checkout), timeout=3)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sorted(path.name for path in self.certs.iterdir()), names)
        self.assertEqual(sum(path.stat().st_size for path in self.certs.iterdir()),
                         private_origin_keys.MAX_TOTAL_BYTES)
        for path in self.certs.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.read_bytes(), b"x" * 65536)

    def test_installed_mtime_drift_after_replace_does_not_get_complete_receipt(self):
        keys = [("a.key", b"synthetic-invalid")]
        code = private_origin_keys._REMOTE_RECEIVER.replace(
            'temporary.pop(temp)',
            'temporary.pop(temp)\n'
            '        os.utime(name, ns=(0,0), dir_fd=directory, follow_symlinks=False)')
        result = subprocess.run([sys.executable, "-I", "-c", code,
                                 "--", str(self.certs)],
                                input=private_origin_keys._header(str(self.certs), keys)
                                      + keys[0][1],
                                capture_output=True, timeout=3, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual((self.certs / "a.key").read_bytes(), keys[0][1])
        self.assertNotIn(b'"status":"complete"', result.stdout)
        self.assertEqual(result.stderr, b"")

    def test_action_preflight_isolated_from_caller_pythonpath_shadowing(self):
        import yaml
        self._key()
        shadow = self.root / "python-shadow"
        shadow.mkdir(mode=0o700)
        (shadow / "json.py").write_text(
            'raise RuntimeError("synthetic forbidden project import")\n', encoding="utf-8")
        action = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
        step = next(step for step in action["runs"]["steps"]
                    if step["name"] == "Validate private origin-key custody")
        command = shlex.split(step["run"].split("set -euo pipefail", 1)[1]
                              .replace("\\\n", " "))
        replacements = {"$GITHUB_ACTION_PATH": str(ACTION.parent),
                        "$INPUT_ORIGIN_KEYS_PATH": str(self.keys),
                        "$GITHUB_WORKSPACE": str(self.workspace)}
        for index, argument in enumerate(command):
            for old, new in replacements.items():
                argument = argument.replace(old, new)
            command[index] = argument
        command[0] = sys.executable
        result = subprocess.run(command, cwd=self.workspace,
                                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                                     "PYTHONPATH": str(shadow)},
                                capture_output=True, timeout=3, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout + result.stderr, b"")

    def test_committed_but_lost_or_mismatched_ack_is_ambiguous_without_retry(self):
        for corruption in ("lost", "nonce"):
            with self.subTest(corruption=corruption):
                # A fresh independent source/destination per attempt; no retry.
                for path in self.keys.iterdir():
                    path.unlink()
                with self.assertRaises(private_origin_keys.OriginKeyError) as caught:
                    self._client_exchange(corruption=corruption)
                self.assertTrue(caught.exception.ambiguous)
                self.assertEqual((self.certs / "a.key").read_bytes(), b"synthetic-invalid-a")
                self.assertNotIn("synthetic-invalid", str(caught.exception))

    def test_late_ack_after_commit_times_out_without_success_claim(self):
        started = time.monotonic()
        with self.assertRaises(private_origin_keys.OriginKeyError) as caught:
            self._client_exchange(corruption="late", timeout=0.3)
        self.assertTrue(caught.exception.ambiguous)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual((self.certs / "a.key").read_bytes(), b"synthetic-invalid-a")

    def test_route_all_scalars_refuse_before_process_creation(self):
        self._key()
        for host, user, path in (("-oProxyCommand=id", "deploy", "/srv/example"),
                                ("host\nid", "deploy", "/srv/example"),
                                ("host", "deploy;id", "/srv/example"),
                                ("host", "deploy", "/srv/../example"),
                                ("host", "deploy", "/srv/x;id"),
                                ("host", "deploy", "/srv/x\nid")):
            with self.subTest(host=host, user=user, path=path):
                with patch.object(private_origin_keys.subprocess, "Popen") as process:
                    with self.assertRaises(private_origin_keys.OriginKeyError):
                        private_origin_keys.transfer(str(self.keys), host, user, path,
                                                     str(self.workspace))
                process.assert_not_called()

    def test_real_local_rsync_keeps_existing_private_keys_and_modes(self):
        import yaml
        action = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
        step = next(step for step in action["runs"]["steps"]
                    if "Rsync project to server" in step["name"])
        self.assertIn("--no-perms", step["run"])
        self.assertNotIn("--delete-excluded", step["run"])
        target = self.root / "receiver"
        target.mkdir(mode=0o700)
        certs = target / "certs"
        certs.mkdir(mode=0o700)
        existing = certs / "retained.key"
        existing.write_bytes(b"synthetic-receiver-key")
        existing.chmod(0o600)
        (self.certs / "retained.key").write_bytes(b"synthetic-wrong-source-key")
        (self.certs / "public.crt").write_bytes(b"synthetic-public-certificate")
        self.certs.chmod(0o755)
        options = []
        for line in step["run"].splitlines():
            value = line.strip().removesuffix("\\").strip()
            if value.startswith(("--delete ", "--partial ", "--no-owner ",
                                 "--chmod=", "--exclude ")):
                options.extend(shlex.split(value))
        result = subprocess.run(["/usr/bin/rsync", "-rl", *options,
                                 str(self.workspace)+"/", str(target)],
                                capture_output=True, timeout=3, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(existing.read_bytes(), b"synthetic-receiver-key")
        self.assertEqual(stat.S_IMODE(existing.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(certs.stat().st_mode), 0o700)
        self.assertEqual((certs / "public.crt").read_bytes(),
                         b"synthetic-public-certificate")


if __name__ == "__main__":
    unittest.main()
