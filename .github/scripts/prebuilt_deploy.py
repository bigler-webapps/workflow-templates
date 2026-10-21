"""Prepare verified static release assets for a later trusted deployment.

The accepted bundle is an uncompressed, strict USTAR archive: only regular
files and directories, UTF-8 relative paths, no PAX/GNU extensions, and no
links or special entries. Bundles are limited to 64 MiB, 512 members, 16 MiB
per file, and 48 MiB total extracted file data. This module only validates
and prepares local asset bytes; it never authorizes a release or contacts a
deployment target.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import stat
import tempfile
from typing import BinaryIO, Iterable

import release_manifest as release


MAX_ARCHIVE_BYTES = release.MAX_BUNDLE_BYTES
MAX_MEMBERS = 512
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_EXTRACTED_BYTES = 48 * 1024 * 1024
IO_CHUNK_BYTES = 64 * 1024
TAR_BLOCK_BYTES = 512


class PreparationError(ValueError):
    """A stable safe failure code; input values and OS errors are never kept."""


@dataclass(frozen=True)
class _Member:
    path: tuple[str, ...]
    is_directory: bool
    data_offset: int
    size: int


def _fail(code: str) -> None:
    raise PreparationError(code) from None


def _read_bundle(source: bytes | bytearray | BinaryIO, expected_length: int) -> bytes:
    if type(expected_length) is not int or not 0 <= expected_length <= MAX_ARCHIVE_BYTES:
        _fail("bundle_length_invalid")
    if isinstance(source, (bytes, bytearray)):
        if len(source) != expected_length or len(source) > MAX_ARCHIVE_BYTES:
            _fail("bundle_length_mismatch")
        return bytes(source)
    if not callable(getattr(source, "read", None)):
        _fail("bundle_stream_invalid")
    result = bytearray()
    try:
        while len(result) <= expected_length:
            limit = min(IO_CHUNK_BYTES, expected_length - len(result) + 1)
            chunk = source.read(limit)
            if not isinstance(chunk, bytes) or len(chunk) > limit:
                _fail("bundle_stream_invalid")
            if not chunk:
                break
            result.extend(chunk)
            if len(result) > MAX_ARCHIVE_BYTES or len(result) > expected_length:
                _fail("bundle_length_mismatch")
    except PreparationError:
        raise
    except Exception:
        _fail("bundle_read_failed")
    if len(result) != expected_length:
        _fail("bundle_length_mismatch")
    return bytes(result)


def _octal(field: bytes, code: str) -> int:
    stripped = field.strip(b" \0")
    if not stripped:
        return 0
    if any(char < 48 or char > 55 for char in stripped):
        _fail(code)
    try:
        return int(stripped, 8)
    except ValueError:
        _fail(code)


def _field_text(field: bytes, code: str) -> str:
    value, separator, trailing = field.partition(b"\0")
    if separator and any(trailing):
        _fail(code)
    if not value:
        return ""
    try:
        return value.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _fail(code)


def _archive_path(header: bytes, is_directory: bool) -> tuple[str, ...]:
    name = _field_text(header[0:100], "archive_path_invalid")
    prefix = _field_text(header[345:500], "archive_path_invalid")
    raw_path = f"{prefix}/{name}" if prefix else name
    if is_directory and raw_path.endswith("/"):
        raw_path = raw_path[:-1]
    if (not raw_path or raw_path.startswith("/") or "\\" in raw_path
            or ":" in raw_path or any(ord(char) < 0x20 or ord(char) == 0x7F for char in raw_path)):
        _fail("archive_path_invalid")
    components = tuple(raw_path.split("/"))
    if any(part in ("", ".", "..") for part in components):
        _fail("archive_path_invalid")
    return components


def _is_control_path(path: tuple[str, ...]) -> bool:
    lowered = tuple(part.casefold() for part in path)
    for part in lowered:
        if part in (".git", ".github", "caddyfile", "dockerfile", ".gitignore", ".dockerignore"):
            return True
        if part.endswith((".sh", ".bash", ".py", ".ps1", ".bat", ".cmd", ".exe")):
            return True
        if part.startswith(".env"):
            return True
        if part in ("project.yml", "project.yaml", "secrets.yml", "secrets.yaml",
                    "project.json", "secrets.json", "action.yml", "action.yaml",
                    "compose.yml", "compose.yaml", "docker-compose.yml", "docker-compose.yaml"):
            return True
        if part.startswith(("compose.", "docker-compose.")) and part.endswith((".yml", ".yaml", ".json")):
            return True
    return False


def _under_roots(path: tuple[str, ...], roots: tuple[tuple[str, ...], ...]) -> bool:
    return any(
        len(path) >= len(root) and path[:len(root)] == root
        for root in roots
    )


def _roots(values: Iterable[str]) -> tuple[tuple[str, ...], ...]:
    if isinstance(values, (str, bytes)):
        _fail("asset_roots_invalid")
    try:
        supplied = tuple(values)
    except Exception:
        _fail("asset_roots_invalid")
    if not supplied or len(supplied) > 16:
        _fail("asset_roots_invalid")
    result: list[tuple[str, ...]] = []
    for value in supplied:
        if not isinstance(value, str):
            _fail("asset_roots_invalid")
        root = tuple(value.split("/"))
        if (not value or "\\" in value or ":" in value or value.startswith("/")
                or any(part in ("", ".", "..") for part in root)
                or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
                or _is_control_path(root)):
            _fail("asset_roots_invalid")
        result.append(root)
    folded = [tuple(part.casefold() for part in root) for root in result]
    if len(set(folded)) != len(folded):
        _fail("asset_roots_invalid")
    return tuple(result)


def _parse_tar(archive: bytes, roots: tuple[tuple[str, ...], ...]) -> tuple[_Member, ...]:
    if not archive or len(archive) > MAX_ARCHIVE_BYTES:
        _fail("archive_size_invalid")
    if len(archive) < 1024 or len(archive) % TAR_BLOCK_BYTES:
        _fail("archive_format_unsupported")
    members: list[_Member] = []
    kinds: dict[tuple[str, ...], str] = {}
    explicit_directories: set[tuple[str, ...]] = set()
    total_bytes = 0
    offset = 0
    ended = False
    while offset + TAR_BLOCK_BYTES <= len(archive):
        header = archive[offset:offset + TAR_BLOCK_BYTES]
        if header == b"\0" * TAR_BLOCK_BYTES:
            if offset + 2 * TAR_BLOCK_BYTES > len(archive):
                _fail("archive_terminator_invalid")
            second = archive[offset + TAR_BLOCK_BYTES:offset + 2 * TAR_BLOCK_BYTES]
            if second != b"\0" * TAR_BLOCK_BYTES or any(archive[offset + 2 * TAR_BLOCK_BYTES:]):
                _fail("archive_terminator_invalid")
            ended = True
            break
        if len(members) >= MAX_MEMBERS:
            _fail("archive_member_limit")
        if header[257:263] != b"ustar\0" or header[263:265] != b"00":
            _fail("archive_format_unsupported")
        stored_checksum = _octal(header[148:156], "archive_header_invalid")
        checksum = sum(header[:148]) + (8 * 32) + sum(header[156:])
        if stored_checksum != checksum:
            _fail("archive_header_invalid")
        if any(header[157:257]) or any(header[265:329]):
            _fail("archive_extension_unsupported")
        if (_octal(header[108:116], "archive_header_invalid") != 0
                or _octal(header[116:124], "archive_header_invalid") != 0
                or _octal(header[329:337], "archive_header_invalid") != 0
                or _octal(header[337:345], "archive_header_invalid") != 0):
            _fail("archive_extension_unsupported")
        mode = _octal(header[100:108], "archive_mode_invalid")
        size = _octal(header[124:136], "archive_size_invalid")
        typeflag = header[156:157]
        if typeflag in (b"\0", b"0"):
            is_directory = False
        elif typeflag == b"5":
            is_directory = True
            if size != 0:
                _fail("archive_directory_invalid")
        else:
            _fail("archive_member_type_unsupported")
        if mode & ~0o777 or mode & 0o022 or mode & 0o6000:
            _fail("archive_mode_invalid")
        if not is_directory and mode & 0o111:
            _fail("archive_executable_file")
        if is_directory and mode & 0o100 == 0:
            _fail("archive_mode_invalid")
        path = _archive_path(header, is_directory)
        if not _under_roots(path, roots):
            _fail("archive_path_outside_roots")
        if _is_control_path(path):
            _fail("archive_control_path")
        if not is_directory and any(
            tuple(part.casefold() for part in path) == tuple(part.casefold() for part in root)
            for root in roots
        ):
            _fail("archive_root_must_be_directory")
        if size > MAX_FILE_BYTES:
            _fail("archive_file_limit")
        if not is_directory:
            total_bytes += size
            if total_bytes > MAX_EXTRACTED_BYTES:
                _fail("archive_total_limit")

        folded_path = tuple(part.casefold() for part in path)
        for index in range(1, len(path)):
            parent = folded_path[:index]
            if kinds.get(parent) == "file":
                _fail("archive_path_collision")
            kinds.setdefault(parent, "directory")
        prior = kinds.get(folded_path)
        if is_directory:
            if prior == "file" or folded_path in explicit_directories:
                _fail("archive_path_collision")
            kinds[folded_path] = "directory"
            explicit_directories.add(folded_path)
        else:
            if prior is not None:
                _fail("archive_path_collision")
            kinds[folded_path] = "file"

        data_offset = offset + TAR_BLOCK_BYTES
        padded_size = ((size + TAR_BLOCK_BYTES - 1) // TAR_BLOCK_BYTES) * TAR_BLOCK_BYTES
        end = data_offset + padded_size
        if end > len(archive):
            _fail("archive_truncated")
        if any(archive[data_offset + size:end]):
            _fail("archive_padding_invalid")
        members.append(_Member(path, is_directory, data_offset, size))
        offset = end
    if not ended:
        _fail("archive_terminator_invalid")
    if not any(not member.is_directory for member in members):
        _fail("archive_assets_missing")
    return tuple(members)


def _validate_destination_parent(parent: Path) -> Path:
    try:
        candidate = Path(parent)
        if not candidate.is_absolute():
            _fail("destination_parent_invalid")
        cursor = candidate
        while True:
            info = cursor.lstat()
            if stat.S_ISLNK(info.st_mode):
                _fail("destination_parent_invalid")
            if cursor.parent == cursor:
                break
            cursor = cursor.parent
        if not candidate.is_dir():
            _fail("destination_parent_invalid")
        return candidate
    except PreparationError:
        raise
    except Exception:
        _fail("destination_parent_invalid")


def _create_private_directory(parent: Path) -> Path:
    path: Path | None = None
    try:
        path = Path(tempfile.mkdtemp(prefix="static-assets-", dir=parent))
        os.chmod(path, 0o700)
        return path
    except Exception:
        if path is not None:
            _remove_created_directory(path)
        _fail("asset_directory_create_failed")


def _ensure_directory(root: Path, relative: tuple[str, ...]) -> None:
    current = root
    for component in relative:
        current = current / component
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            info = current.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                _fail("asset_write_failed")


def _write_file(root: Path, archive: bytes, member: _Member) -> None:
    parent = member.path[:-1]
    _ensure_directory(root, parent)
    target = root.joinpath(*member.path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags, 0o600)
        try:
            cursor = member.data_offset
            remaining = member.size
            while remaining:
                chunk = memoryview(archive)[cursor:cursor + min(IO_CHUNK_BYTES, remaining)]
                written = os.write(descriptor, chunk)
                if written <= 0:
                    _fail("asset_write_failed")
                cursor += written
                remaining -= written
        finally:
            os.close(descriptor)
    except PreparationError:
        raise
    except Exception:
        _fail("asset_write_failed")


def _remove_created_directory(path: Path) -> None:
    try:
        if path.is_symlink():
            path.unlink()
        else:
            shutil.rmtree(path)
    except Exception:
        _fail("asset_cleanup_failed")


def prepare_static_assets(
    raw_manifest: bytes | bytearray,
    *,
    expected_identity: release.ReleaseIdentity,
    selected_artifact: release.StaticArtifactSelection,
    bundle: bytes | bytearray | BinaryIO,
    allowed_asset_roots: Iterable[str],
    destination_parent: str | os.PathLike[str],
) -> Path:
    """Validate the original release evidence, then safely prepare local assets.

    ``allowed_asset_roots`` and ``destination_parent`` are trusted caller
    policy. The manifest cannot select either value. On success, the returned
    new directory contains only regular asset files with private safe modes.
    On failure no created filesystem path is retained. Release authorization,
    deployment configuration, target transfer, and post-deployment cleanup
    belong to the trusted deployment integration.
    """
    try:
        roots = _roots(allowed_asset_roots)
        manifest = release.validate_manifest(
            raw_manifest,
            expected=expected_identity,
            expected_static_artifact=selected_artifact,
        )
    except PreparationError:
        raise
    except release.ManifestError as exc:
        _fail(str(exc))
    except Exception:
        _fail("manifest_validation_failed")

    archive = _read_bundle(bundle, manifest["output"]["byte_length"])
    try:
        release.verify_static_bundle(archive, manifest=manifest)
    except release.ManifestError as exc:
        _fail(str(exc))
    except Exception:
        _fail("bundle_validation_failed")
    members = _parse_tar(archive, roots)
    parent = _validate_destination_parent(Path(destination_parent))
    prepared: Path | None = None
    try:
        prepared = _create_private_directory(parent)
        for member in members:
            if member.is_directory:
                _ensure_directory(prepared, member.path)
            else:
                _write_file(prepared, archive, member)
        return prepared
    except PreparationError:
        if prepared is not None:
            _remove_created_directory(prepared)
        raise
    except Exception:
        if prepared is not None:
            _remove_created_directory(prepared)
        _fail("asset_write_failed")
