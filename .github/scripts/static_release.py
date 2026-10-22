"""CI-only bounded producer for release-contract-v1 static assets.

The producer treats the checked-out project as data. It never runs app code,
builds assets, contacts a service, or chooses deployment routing.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import subprocess
import sys
import tarfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_manifest import ReleaseIdentity, produce_static_manifest, static_artifact_name
from prebuilt_deploy import _is_control_path

MAX_ARCHIVE = 64 * 1024 * 1024
MAX_MEMBERS = 512
MAX_FILE = 16 * 1024 * 1024
MAX_TOTAL = 48 * 1024 * 1024
CHUNK = 64 * 1024
GIT_TIMEOUT = 10
_REPO = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_.-]{1,100}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_WORKFLOW = re.compile(r"\.github/workflows/[A-Za-z0-9][A-Za-z0-9._-]*\.yml\Z")
_PATH = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\Z")


class StaticReleaseError(ValueError):
    """Stable safe diagnostic code."""


def _fail(code: str) -> None:
    raise StaticReleaseError(code) from None


def _scalar(value: str, pattern: re.Pattern[str]) -> bool:
    return (isinstance(value, str) and len(value) <= 512 and bool(pattern.fullmatch(value))
            and all(ord(c) >= 0x20 and ord(c) != 0x7f for c in value))


def _command_env(env: dict[str, str]) -> dict[str, str]:
    allowed = ("PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
               "HOME", "USERPROFILE", "TMP", "TEMP", "TMPDIR", "LANG", "LC_ALL")
    return {key: env[key] for key in allowed if key in env}


def _run_git(argv: list[str], *, env: dict[str, str], timeout: float = GIT_TIMEOUT) -> bytes:
    """Run a fixed Git argv with bounded time/output and no job credentials."""
    process = None
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env=env)
        result: list[bytes] = []
        overflow = threading.Event()
        def read():
            data = process.stdout.read(257)
            result.append(data)
            if len(data) > 256:
                overflow.set()
                process.kill()
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        process.wait(timeout=timeout)
        reader.join(timeout=1)
        if reader.is_alive() or overflow.is_set() or process.returncode:
            _fail("source_lookup_failed")
        return result[0] if result else b""
    except StaticReleaseError:
        raise
    except Exception:
        _fail("source_lookup_failed")
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=2)
            except Exception:
                pass
            if process.stdout:
                process.stdout.close()


def _identity(env: dict[str, str], run=_run_git) -> ReleaseIdentity:
    repo, sha, ref = (env.get(k, "") for k in ("GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_REF"))
    run_id, attempt = env.get("GITHUB_RUN_ID", ""), env.get("GITHUB_RUN_ATTEMPT", "")
    wf_ref, supplied = env.get("GITHUB_WORKFLOW_REF", ""), env.get("RELEASE_WORKFLOW_REF", "")
    if not _scalar(repo, _REPO) or not _scalar(sha, _SHA):
        _fail("source_identity_invalid")
    if ref not in ("refs/heads/develop", "refs/heads/main"):
        _fail("source_ref_unsupported")
    if (not re.fullmatch(r"[1-9][0-9]{0,19}", run_id)
            or not re.fullmatch(r"[1-9][0-9]{0,19}", attempt)):
        _fail("run_identity_invalid")
    suffix = "@" + ref
    prefix = repo + "/.github/workflows/"
    if not wf_ref.startswith(prefix) or not wf_ref.endswith(suffix) or wf_ref != supplied:
        _fail("workflow_identity_invalid")
    workflow = wf_ref[len(repo) + 1:-len(suffix)]
    if not _scalar(workflow, _WORKFLOW):
        _fail("workflow_identity_invalid")
    child_env = dict(_command_env(env), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    try:
        head = run(["git", "rev-parse", "HEAD"], env=child_env, timeout=GIT_TIMEOUT).decode("ascii", "strict").strip()
    except Exception:
        _fail("source_lookup_failed")
    if head != sha:
        _fail("source_identity_invalid")
    return ReleaseIdentity(repo, sha, ref, workflow, int(run_id), int(attempt))


def _asset_path(value: str) -> tuple[str, ...]:
    if not _scalar(value, _PATH) or value.startswith("/") or "\\" in value or ":" in value:
        _fail("asset_path_invalid")
    parts = tuple(value.split("/"))
    if any(p in ("", ".", "..") or p.startswith("-") for p in parts) or _forbidden(parts):
        _fail("asset_path_invalid")
    return parts


def _checkout_root(env: dict[str, str], run) -> Path:
    child_env = dict(_command_env(env), GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
    try:
        raw = run(['git', 'rev-parse', '--show-toplevel'], env=child_env, timeout=GIT_TIMEOUT)
        value = raw.decode('utf-8', 'strict').removesuffix('\n')
        root = Path(value)
        if (not root.is_absolute() or not root.is_dir() or root.resolve() != root
                or any(ord(c) < 32 or ord(c) == 127 for c in value)
                or not Path.cwd().resolve().is_relative_to(root)):
            _fail('checkout_root_invalid')
        return root
    except StaticReleaseError:
        raise
    except Exception:
        _fail('checkout_root_invalid')


def _root_path(value: str) -> tuple[str, ...]:
    parts = _asset_path(value)
    if len(parts) > 16:
        _fail("asset_root_invalid")
    return parts


def _forbidden(parts: tuple[str, ...]) -> bool:
    if _is_control_path(parts):
        return True
    for part in parts:
        low = part.casefold()
        if low in (".ssh", ".secrets", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa") or low.endswith((".pem", ".key", ".p12", ".pfx")):
            return True
        if low in (".git", ".github", "caddyfile", "dockerfile", ".gitignore", ".dockerignore",
                   "project.yml", "project.yaml", "project.json", "secrets.yml", "secrets.yaml",
                   "secrets.json", "action.yml", "action.yaml", "compose.yml", "compose.yaml",
                   "docker-compose.yml", "docker-compose.yaml"):
            return True
        if low.startswith(".env") or low.endswith((".sh", ".bash", ".py", ".ps1", ".bat", ".cmd", ".exe")):
            return True
        if low.startswith(("compose.", "docker-compose.")) and low.endswith((".yml", ".yaml", ".json")):
            return True
    return False


def _require_posix_safety() -> None:
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "open") or not hasattr(os, "O_DIRECTORY"):
        _fail("platform_protections_unsupported")


def _open_directory(root_fd: int, parts: tuple[str, ...]) -> int:
    fd = os.dup(root_fd)
    try:
        for part in parts:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        _fail("asset_tree_invalid")


def _collect(root_fd: int, rel: tuple[str, ...], out_root: tuple[str, ...]):
    found = []
    seen = set()
    total = 0
    count = 0
    def walk(fd: int, prefix: tuple[str, ...]):
        nonlocal total, count
        try:
            with os.scandir(fd) as iterator:
                names = []
                for entry in iterator:
                    names.append(entry.name)
                    if len(names) + count > MAX_MEMBERS:
                        _fail("asset_member_limit")
        except StaticReleaseError:
            raise
        except Exception:
            _fail("asset_tree_invalid")
        for name in names:
            if not isinstance(name, str) or not name or name in (".", "..") or "/" in name or "\\" in name:
                _fail("asset_path_invalid")
            parts = prefix + (name,)
            if (len(parts) > 32 or len('/'.join(parts).encode('utf-8')) > 255
                    or ':' in name or any(ord(c) < 32 or ord(c) == 127 for c in name)):
                _fail("asset_path_invalid")
            if _forbidden(parts):
                _fail("asset_control_file_rejected")
            folded = "/".join(parts).casefold()
            if folded in seen:
                _fail("asset_path_conflict")
            seen.add(folded)
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except Exception:
                _fail("asset_tree_invalid")
            mode = info.st_mode
            if stat.S_ISDIR(mode):
                count += 1
                if count > MAX_MEMBERS:
                    _fail("asset_member_limit")
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    after = os.fstat(child)
                    if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
                        _fail("asset_changed")
                    walk(child, parts)
                finally:
                    os.close(child)
            elif stat.S_ISREG(mode) and not mode & 0o7111 and info.st_nlink == 1:
                count += 1
                if count > MAX_MEMBERS:
                    _fail("asset_member_limit")
                if info.st_size > MAX_FILE:
                    _fail("asset_file_limit")
                total += info.st_size
                if total > MAX_TOTAL:
                    _fail("asset_total_limit")
                found.append((parts, info))
            else:
                _fail("asset_file_type_rejected")
    walk(root_fd, rel)
    if not found:
        _fail("asset_tree_empty")
    return found


def _write_bundle(asset_path: Path, parts: tuple[str, ...], out_root: tuple[str, ...], bundle_path: Path) -> tuple[int, str]:
    _require_posix_safety()
    try:
        workspace = asset_path.resolve(strict=True)
        workspace_fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = _open_directory(workspace_fd, parts)
        finally:
            os.close(workspace_fd)
    except StaticReleaseError:
        raise
    except Exception:
        _fail("asset_tree_invalid")
    try:
        entries = _collect(fd, (), parts)
        bundle_path.parent.mkdir(mode=0o700, parents=False, exist_ok=True)
        with bundle_path.open("xb") as output:
            os.chmod(bundle_path, 0o600)
            with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                for rel, original in sorted(entries, key=lambda row: "/".join(row[0]).encode("utf-8")):
                    name = "/".join(out_root + rel)
                    name_bytes = name.encode("utf-8")
                    name_parts = name.split("/")
                    if (len(name_bytes) > 255 or len(name_parts[-1].encode("utf-8")) > 100
                            or any(len(part.encode("utf-8")) > 100 for part in name_parts[:-1])
                            or len("/".join(name_parts[:-1]).encode("utf-8")) > 155):
                        _fail("asset_name_limit")
                    parent = _open_directory(fd, rel[:-1]) if len(rel) > 1 else os.dup(fd)
                    try:
                        source_fd = os.open(rel[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                        try:
                            current = os.fstat(source_fd)
                            if not stat.S_ISREG(current.st_mode) or current.st_mode & 0o7111 or current.st_nlink != 1:
                                _fail("asset_changed")
                            if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (original.st_dev, original.st_ino, original.st_size, original.st_mtime_ns):
                                _fail("asset_changed")
                            info = tarfile.TarInfo(name)
                            info.type = tarfile.REGTYPE
                            info.size = original.st_size
                            info.mode = 0o644
                            info.uid = info.gid = info.devmajor = info.devminor = 0
                            info.uname = info.gname = info.linkname = ""
                            info.mtime = 0
                            class BoundedSource:
                                def __init__(self, stream):
                                    self.stream = stream
                                    self.remaining = original.st_size
                                def read(self, amount=-1):
                                    if self.remaining == 0:
                                        return b""
                                    limit = min(CHUNK, self.remaining, amount if amount >= 0 else CHUNK)
                                    data = self.stream.read(limit)
                                    if not data or len(data) > limit:
                                        _fail("asset_changed")
                                    self.remaining -= len(data)
                                    return data
                            with os.fdopen(os.dup(source_fd), "rb") as raw_source:
                                source = BoundedSource(raw_source)
                                archive.addfile(info, source)
                                if source.remaining:
                                    _fail("asset_changed")
                            after = os.fstat(source_fd)
                            if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (original.st_dev, original.st_ino, original.st_size, original.st_mtime_ns):
                                _fail("asset_changed")
                        finally:
                            os.close(source_fd)
                    finally:
                        os.close(parent)
            output.flush()
            os.fsync(output.fileno())
        if bundle_path.stat().st_size > MAX_ARCHIVE:
            _fail("asset_archive_limit")
        return bundle_path.stat().st_size, _sha256(bundle_path)
    except StaticReleaseError:
        bundle_path.unlink(missing_ok=True)
        raise
    except Exception:
        bundle_path.unlink(missing_ok=True)
        _fail("asset_pack_failed")
    finally:
        os.close(fd)


def _sha256(path: Path) -> str:
    import hashlib
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while True:
                block = stream.read(CHUNK)
                if not block:
                    break
                digest.update(block)
    except Exception:
        _fail("bundle_read_failed")
    return digest.hexdigest()


def _private_dir(env: dict[str, str], workspace: Path) -> Path:
    temp = Path(env.get("RUNNER_TEMP", ""))
    if (not temp.is_absolute() or not temp.is_dir() or temp.is_symlink()
            or temp.resolve() != temp or any(ord(c) < 32 or ord(c) == 127 for c in str(temp))
            or temp.is_relative_to(workspace)):
        _fail("runner_temp_invalid")
    _require_posix_safety()
    path = temp / ("release-static-" + secrets.token_hex(12))
    path.mkdir(mode=0o700)
    try:
        os.chmod(path, 0o700)
        (path / ".owned").write_text("release-static-v1", encoding="utf-8")
        os.chmod(path / ".owned", 0o600)
    except BaseException:
        (path / '.owned').unlink(missing_ok=True)
        path.rmdir()
        raise
    return path


def _write_output(env: dict[str, str], values: dict[str, str]) -> None:
    path = Path(env.get("GITHUB_OUTPUT", ""))
    if (not path.is_absolute() or path.is_symlink()
            or any(any(ord(c) < 32 or ord(c) == 127 for c in v) for v in values.values())
            or any(not re.fullmatch(r"[a-z_]+", k) for k in values)):
        _fail("github_output_invalid")
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for key, value in values.items():
            if not re.fullmatch(r"[a-z_]+", key):
                _fail("github_output_invalid")
            handle.write(f"{key}={value}\n")


def prepare(env: dict[str, str], asset_path: str, asset_root: str, run=_run_git, on_private=None) -> tuple[Path, str]:
    if env.get("RELEASE_CONTRACT") != "v1":
        _fail("release_contract_unsupported")
    identity = _identity(env, run)
    workspace = _checkout_root(env, run)
    rel = _asset_path(asset_path)
    out_root = _root_path(asset_root)
    _require_posix_safety()
    private = _private_dir(env, workspace)
    try:
        if on_private is not None:
            on_private(private)
        size, digest = _write_bundle(workspace, rel, out_root, private / "bundle.tar")
        data = {"identity": identity.__dict__, "length": size, "sha256": digest}
        metadata = private / "prepared.json"
        with metadata.open('x', encoding='utf-8') as output:
            os.chmod(metadata, 0o600)
            json.dump(data, output, sort_keys=True)
        name = static_artifact_name(identity)
        return private, name
    except BaseException:
        cleanup(private, env)
        raise


def finalize(env: dict[str, str], private_text: str, artifact_id_text: str, run=_run_git) -> None:
    private = _owned_private(env, private_text)
    if not re.fullmatch(r"[1-9][0-9]{0,19}", artifact_id_text):
        _fail("artifact_id_invalid")
    identity = _identity(env, run)
    bundle = private / "bundle.tar"
    try:
        metadata_path = private / 'prepared.json'
        metadata_info = metadata_path.lstat()
        if (not stat.S_ISREG(metadata_info.st_mode) or metadata_info.st_nlink != 1
                or stat.S_IMODE(metadata_info.st_mode) != 0o600 or metadata_info.st_size > 2048):
            _fail('prepared_state_invalid')
        data = json.loads(metadata_path.read_text(encoding='utf-8'))
        if data.get('identity') != identity.__dict__:
            _fail('source_identity_changed')
        info = bundle.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
            _fail("bundle_file_invalid")
        size = info.st_size
        if not 0 <= size <= MAX_ARCHIVE:
            _fail("bundle_length_invalid")
        if data.get('length') != size or data.get('sha256') != _sha256(bundle):
            _fail('bundle_changed')
        with bundle.open("rb") as stream:
            manifest = produce_static_manifest(identity, artifact_id=int(artifact_id_text), bundle=stream, bundle_length=size)
        path = private / "release.json"
        with path.open("xb") as stream:
            os.chmod(path, 0o600)
            stream.write(manifest)
        _write_output(env, {"manifest_path": str(path), "manifest_name": "release-manifest-" + str(identity.run_id) + "-" + str(identity.attempt), "artifact_id": artifact_id_text})
    except StaticReleaseError:
        raise
    except Exception:
        _fail("manifest_finalize_failed")


def _owned_private(env: dict[str, str], value: str) -> Path:
    temp = Path(env.get("RUNNER_TEMP", ""))
    candidate = Path(value)
    if not candidate.is_absolute() or candidate.parent != temp or not re.fullmatch(r"release-static-[0-9a-f]{24}", candidate.name):
        _fail("private_output_invalid")
    try:
        if (not temp.is_absolute() or temp.resolve() != temp or candidate.is_symlink()
                or not candidate.is_dir() or candidate.stat().st_uid != os.getuid()
                or stat.S_IMODE(candidate.stat().st_mode) != 0o700):
            _fail("private_output_invalid")
        marker = candidate / ".owned"
        info = marker.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_size != len('release-static-v1') or stat.S_IMODE(info.st_mode) != 0o600
                or marker.read_text(encoding="utf-8") != "release-static-v1"):
            _fail("private_output_invalid")
        return candidate
    except StaticReleaseError:
        raise
    except Exception:
        _fail("private_output_invalid")


def cleanup(private_text: str | Path, env: dict[str, str]) -> None:
    candidate = Path(private_text)
    temp = Path(env.get('RUNNER_TEMP', ''))
    if (temp.is_absolute() and temp.resolve() == temp and candidate.is_absolute()
            and candidate.parent == temp and re.fullmatch(r'release-static-[0-9a-f]{24}', candidate.name)
            and not candidate.exists() and not candidate.is_symlink()):
        return
    private = _owned_private(env, str(private_text))
    children = list(private.iterdir())
    for child in children:
        if (child.name not in ('.owned', 'prepared.json', 'bundle.tar', 'release.json')
                or child.is_symlink() or not child.is_file()):
            _fail("private_output_invalid")
    for child in children:
        child.unlink()
    private.rmdir()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="phase", required=True)
    p = subs.add_parser("prepare")
    p.add_argument("--asset-path", required=True)
    p.add_argument("--asset-root", default="report/report")
    f = subs.add_parser("finalize")
    f.add_argument("--private-dir", required=True)
    f.add_argument("--artifact-id", required=True)
    c = subs.add_parser("cleanup")
    c.add_argument("--private-dir", required=True)
    args = parser.parse_args(argv)
    env = dict(os.environ)
    previous = signal.getsignal(signal.SIGTERM)
    def cancel(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, cancel)
    try:
        if args.phase == "prepare":
            private, name = prepare(env, args.asset_path, args.asset_root,
                                    on_private=lambda path: _write_output(env, {'private_dir': str(path)}))
            try:
                _write_output(env, {"private_dir": str(private), "bundle_path": str(private / "bundle.tar"), "bundle_name": name})
            except BaseException:
                cleanup(private, env)
                raise
        elif args.phase == "finalize":
            finalize(env, args.private_dir, args.artifact_id)
        else:
            cleanup(args.private_dir, env)
    except (Exception, KeyboardInterrupt) as exc:
        code = str(exc) if isinstance(exc, StaticReleaseError) else "static_release_failed"
        print(f"static release failed: {code}", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
