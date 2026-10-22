#!/usr/bin/env python3
"""Fail-closed, read-only preflight for the pinned routine Claude CLI."""

from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import subprocess
import sys
import time
import stat
import shutil
import tempfile
import signal
from pathlib import Path

MAX_MANIFEST_BYTES = 4096
MAX_OUTPUT_BYTES = 4096
TIMEOUT_SECONDS = 5
EXPECTED_KEYS = {"schema_version", "package", "version", "node_major", "prefix"}
SEMVER = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")


class PreflightError(Exception):
    """An intentionally non-sensitive fixed diagnostic code."""

    def __init__(self, code: str):
        self.code = code


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PreflightError("manifest_invalid")
        result[key] = value
    return result


def load_contract(path: Path) -> dict:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MANIFEST_BYTES:
                raise PreflightError("manifest_invalid")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(MAX_MANIFEST_BYTES + 1)
        finally:
            os.close(fd)
    except OSError as exc:
        raise PreflightError("manifest_unavailable") from exc
    if len(raw) > MAX_MANIFEST_BYTES:
        raise PreflightError("manifest_invalid")
    try:
        data = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=_pairs,
                          parse_constant=lambda _value: (_ for _ in ()).throw(PreflightError("manifest_invalid")))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise PreflightError("manifest_invalid") from exc
    if (not isinstance(data, dict) or set(data) != EXPECTED_KEYS
            or type(data["schema_version"]) is not int or data["schema_version"] != 1
            or data["package"] != "@anthropic-ai/claude-code"
            or type(data["version"]) is not str or len(data["version"]) > 32 or not SEMVER.fullmatch(data["version"])
            or type(data["node_major"]) is not int or data["node_major"] < 22
            or data['prefix'] != '/usr/local'):
        raise PreflightError("manifest_invalid")
    return data


def _read_bounded(proc: subprocess.Popen, deadline: float) -> tuple[bytes, bytes]:
    selector = selectors.DefaultSelector()
    streams = {proc.stdout: bytearray(), proc.stderr: bytearray()}
    for stream in streams:
        selector.register(stream, selectors.EVENT_READ)
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PreflightError("cli_timeout")
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fileobj.fileno(), MAX_OUTPUT_BYTES + 1 - len(streams[key.fileobj]))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                streams[key.fileobj].extend(chunk)
                if len(streams[key.fileobj]) > MAX_OUTPUT_BYTES:
                    raise PreflightError("cli_output_invalid")
    finally:
        selector.close()
    return bytes(streams[proc.stdout]), bytes(streams[proc.stderr])


def provisioned_executable(contract: dict) -> str:
    """Fixed provisioned location; the caller's PATH cannot select this CLI."""
    return contract['prefix'] + '/bin/claude'


def check_cli(contract: dict, executable: str | None = None) -> None:
    # Explicit path overrides support isolated fixtures only; the action exposes
    # no override input. A version report is not a binary provenance attestation.
    resolved = (shutil.which(executable) if executable is not None
                else provisioned_executable(contract))
    if resolved is None:
        raise PreflightError("cli_unavailable")
    with tempfile.TemporaryDirectory(prefix="routine-cli-") as directory:
        _check_cli(contract, str(Path(resolved).absolute()), directory)


def _check_cli(contract: dict, executable: str, directory: str) -> None:
    env = {"PATH": os.defpath, "HOME": directory, "CLAUDE_CONFIG_DIR": directory,
           "LANG": "C.UTF-8", "DISABLE_AUTOUPDATER": "1",
           "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    try:
        proc = subprocess.Popen([executable, "--version"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                                cwd=directory, env=env, start_new_session=True)
    except (OSError, ValueError) as exc:
        raise PreflightError("cli_unavailable") from exc
    deadline = time.monotonic() + TIMEOUT_SECONDS
    try:
        stdout, stderr = _read_bounded(proc, deadline)
        if proc.wait(timeout=max(0, deadline - time.monotonic())) != 0:
            raise PreflightError("cli_failed")
    except subprocess.TimeoutExpired as exc:
        raise PreflightError("cli_timeout") from exc
    except OSError as exc:
        raise PreflightError("cli_failed") from exc
    finally:
        # Kill the private session's process group, including pipe-owning
        # descendants even if its original leader has already exited.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        proc.stdout.close()
        proc.stderr.close()
    if stderr:
        raise PreflightError("cli_output_invalid")
    try:
        reported = stdout.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise PreflightError("cli_output_invalid") from exc
    expected = f"{contract['version']} (Claude Code)"
    if reported not in (expected, expected + "\n", expected + "\r\n"):
        raise PreflightError("cli_version_mismatch")


def default_manifest() -> Path:
    root = Path(__file__).resolve().parents[2]
    return root / "ansible" / "roles" / "github_runner" / "files" / "routine-cli.json"


def main(argv=None) -> int:
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            raise PreflightError("arguments_invalid")
    parser = Parser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=default_manifest())
    parser.add_argument("--executable", help="explicit executable for private local fixtures")
    try:
        args = parser.parse_args(argv)
        contract = load_contract(args.manifest)
        check_cli(contract, args.executable)
    except PreflightError as exc:
        print(f"routine CLI preflight failed: {exc.code}", file=sys.stderr)
        return 1
    print("routine CLI preflight passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
