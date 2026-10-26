"""Bounded private origin-key custody and SSH transport for deploy-traefik.

Importing this module performs no filesystem, process, or network work. The
only remote operation is a fixed receiver program embedded in this trusted
action source; key bytes travel on its stdin after a privacy handshake.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import math
import os
import re
import selectors
import shlex
import stat
import struct
import subprocess
import sys
import time


MAX_KEYS = 32
MAX_KEY_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024
MAX_HEADER_BYTES = 8 * 1024
MAX_RECEIPT_BYTES = 8 * 1024
MAX_OPERATION_SECONDS = 120.0
SSH = "/usr/bin/ssh"
PYTHON = "/usr/bin/python3"
_KEY_NAME = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)*\.key\Z")
_SAFE_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}\Z")
_SAFE_USER = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,31}\Z")
_SAFE_REMOTE_PATH = re.compile(r"/[A-Za-z0-9._/-]{1,1023}\Z")
_ACL_ACCESS = "system.posix_acl_access"
_ACL_DEFAULT = "system.posix_acl_default"
_NO_XATTR = frozenset({errno.ENODATA, getattr(errno, "ENOATTR", errno.ENODATA)})


class OriginKeyError(RuntimeError):
    """Stable refusal code with no path, key bytes, or operating-system text."""

    def __init__(self, code: str, *, ambiguous: bool = False):
        self.code = code
        self.ambiguous = ambiguous
        super().__init__(code)


def _fail(code: str, *, ambiguous: bool = False) -> None:
    raise OriginKeyError(code, ambiguous=ambiguous) from None


def _read_acl(fd: int, name: str) -> list[tuple[int, int, int]] | None:
    try:
        value = os.getxattr(fd, name)
    except OSError as exc:
        if exc.errno in _NO_XATTR:
            return None
        _fail("acl_unverifiable")
    if type(value) is not bytes or len(value) < 4 or (len(value) - 4) % 8:
        _fail("acl_invalid")
    try:
        version = struct.unpack_from("<I", value)[0]
        if version != 2:
            _fail("acl_invalid")
        entries = [struct.unpack_from("<HHI", value, offset)
                   for offset in range(4, len(value), 8)]
        if not entries or len(entries) > 256:
            _fail("acl_invalid")
        return entries
    except (OverflowError, struct.error):
        _fail("acl_invalid")


def _check_private_acl(fd: int, mode: int, *, directory: bool) -> None:
    entries = _read_acl(fd, _ACL_ACCESS)
    if entries is not None:
        by_tag: dict[int, list[tuple[int, int]]] = {}
        for tag, permissions, ident in entries:
            if permissions & ~7:
                _fail("acl_invalid")
            by_tag.setdefault(tag, []).append((permissions, ident))
        if any(tag in by_tag for tag in (2, 8)):
            _fail("acl_broad")
        if any(len(by_tag.get(tag, [])) != 1 for tag in (1, 4, 32)):
            _fail("acl_invalid")
        owner_permissions = by_tag[1][0][0]
        group_permissions = by_tag[4][0][0]
        other_permissions = by_tag[32][0][0]
        mask_permissions = by_tag.get(16, [(group_permissions, 0)])[0][0]
        if (owner_permissions != ((mode >> 6) & 7) or group_permissions != 0
                or other_permissions != 0 or mask_permissions != 0):
            _fail("acl_broad")
        if set(by_tag) - {1, 4, 16, 32} or len(by_tag.get(16, [])) > 1:
            _fail("acl_invalid")
    if directory and _read_acl(fd, _ACL_DEFAULT) is not None:
        _fail("acl_default_present")


def _check_ancestor_acl(fd: int, mode: int, *, sticky_root: bool) -> None:
    entries = _read_acl(fd, _ACL_ACCESS)
    if entries is not None:
        by_tag: dict[int, list[tuple[int, int]]] = {}
        for tag, permissions, ident in entries:
            if permissions & ~7:
                _fail("acl_invalid")
            by_tag.setdefault(tag, []).append((permissions, ident))
        if (any(tag in by_tag for tag in (2, 8))
                or any(len(by_tag.get(tag, [])) != 1 for tag in (1, 4, 32))
                or set(by_tag) - {1, 4, 16, 32}):
            _fail("acl_broad")
        group = by_tag[4][0][0]
        other = by_tag[32][0][0]
        mask = by_tag.get(16, [(group, 0)])[0][0]
        if (by_tag[1][0][0] != ((mode >> 6) & 7)
                or (group & mask) != ((mode >> 3) & 7)
                or other != (mode & 7)):
            _fail("acl_invalid")
        if not sticky_root and ((group | other) & 2 or (mask & 2)):
            _fail("acl_broad")


def _check_directory_fd(fd: int, *, owner: int, exact_mode: int | None = None,
                        private_leaf: bool = False) -> os.stat_result:
    try:
        info = os.fstat(fd)
    except OSError:
        _fail("directory_unverifiable")
    mode = stat.S_IMODE(info.st_mode)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != owner
            or (exact_mode is not None and mode != exact_mode)
            or (exact_mode is None and mode & 0o022)):
        _fail("directory_invalid")
    _check_private_acl(fd, mode, directory=True)
    return info


def _open_absolute_directory(path: str, *, leaf_mode: int | None,
                             leaf_owner: int | None) -> tuple[int, os.stat_result]:
    if type(path) is not str or not path.startswith("/") or len(path) > 4096:
        _fail("source_path_invalid")
    components = path.split("/")[1:]
    if not components or any(not part or part in (".", "..") for part in components):
        _fail("source_path_invalid")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        current = os.open("/", flags)
    except OSError:
        _fail("source_path_invalid")
    try:
        root_info = os.fstat(current)
        if not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != 0:
            _fail("source_path_invalid")
        for index, component in enumerate(components):
            try:
                child = os.open(component, flags, dir_fd=current)
            except OSError:
                _fail("source_path_invalid")
            os.close(current)
            current = child
            info = os.fstat(current)
            is_leaf = index == len(components) - 1
            mode = stat.S_IMODE(info.st_mode)
            if (not stat.S_ISDIR(info.st_mode)
                    or info.st_uid not in (0, os.geteuid())
                    or (not is_leaf and mode & 0o022
                        and not (info.st_uid == 0 and mode & stat.S_ISVTX))):
                _fail("source_path_invalid")
            if is_leaf:
                if leaf_owner is not None and info.st_uid != leaf_owner:
                    _fail("directory_invalid")
                if leaf_mode is None:
                    if mode & 0o022:
                        _fail("directory_invalid")
                    _check_ancestor_acl(
                        current, mode,
                        sticky_root=info.st_uid == 0 and bool(mode & stat.S_ISVTX),
                    )
                else:
                    _check_directory_fd(current, owner=info.st_uid, exact_mode=leaf_mode)
            else:
                _check_ancestor_acl(
                    current, mode,
                    sticky_root=info.st_uid == 0 and bool(mode & stat.S_ISVTX),
                )
        return current, os.fstat(current)
    except BaseException:
        try:
            os.close(current)
        except OSError:
            pass
        raise


def _snapshot(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _same_node(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino, left.st_uid, left.st_mode, left.st_nlink) == (
        right.st_dev, right.st_ino, right.st_uid, right.st_mode, right.st_nlink)


def _source_payload(source: str, *, deadline: float | None = None) -> tuple[list[tuple[str, bytes]], tuple[int, ...] | None]:
    if deadline is not None:
        _remaining(deadline)
    if source == "":
        return [], None
    owner = os.geteuid()
    directory_fd, directory_info = _open_absolute_directory(
        source, leaf_mode=0o700, leaf_owner=owner)
    directory_snapshot = _snapshot(directory_info)
    try:
        try:
            names = os.listdir(directory_fd)
        except OSError:
            _fail("source_directory_invalid")
        if (len(names) > MAX_KEYS or any(type(name) is not str or (len(name)>128 or not _KEY_NAME.fullmatch(name))
                                         for name in names)):
            _fail("source_names_invalid")
        if not names:
            _fail("source_empty")
        ordered = sorted(names)
        result: list[tuple[str, bytes]] = []
        total = 0
        for name in ordered:
            if deadline is not None:
                _remaining(deadline)
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
                             dir_fd=directory_fd)
            except OSError:
                _fail("source_file_invalid")
            try:
                before = os.fstat(fd)
                mode = stat.S_IMODE(before.st_mode)
                if (not stat.S_ISREG(before.st_mode) or before.st_uid != owner
                        or before.st_nlink != 1 or mode != 0o600
                        or before.st_size <= 0 or before.st_size > MAX_KEY_BYTES):
                    _fail("source_file_invalid")
                _check_private_acl(fd, mode, directory=False)
                chunks = bytearray()
                while len(chunks) <= MAX_KEY_BYTES:
                    if deadline is not None:
                        _remaining(deadline)
                    chunk = os.read(fd, min(8192, MAX_KEY_BYTES + 1 - len(chunks)))
                    if not chunk:
                        break
                    chunks.extend(chunk)
                after = os.fstat(fd)
                if (_snapshot(before) != _snapshot(after) or len(chunks) != before.st_size
                        or not chunks or len(chunks) > MAX_KEY_BYTES):
                    _fail("source_file_changed")
                _check_private_acl(fd, stat.S_IMODE(after.st_mode), directory=False)
                total += len(chunks)
                if total > MAX_TOTAL_BYTES:
                    _fail("source_batch_too_large")
                result.append((name, bytes(chunks)))
            finally:
                os.close(fd)
        if (_snapshot(os.fstat(directory_fd)) != _snapshot(directory_info)
                or sorted(os.listdir(directory_fd)) != ordered):
            _fail("source_directory_changed")
        return result, directory_snapshot
    finally:
        os.close(directory_fd)


def _legacy_keys(legacy_root: str) -> None:
    root_fd, _ = _open_absolute_directory(legacy_root, leaf_mode=None, leaf_owner=None)
    try:
        try:
            certs_fd = os.open("certs", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                               | getattr(os, "O_NOFOLLOW", 0), dir_fd=root_fd)
        except FileNotFoundError:
            return
        except OSError:
            _fail("legacy_directory_invalid")
        try:
            # This is a filename-only scan of the caller's public certificate
            # directory. It never reads key bytes or imposes custody ACLs on
            # deployments which do not supply any private-key source.
            if not stat.S_ISDIR(os.fstat(certs_fd).st_mode):
                _fail("legacy_directory_invalid")
            try:
                names = os.listdir(certs_fd)
            except OSError:
                _fail("legacy_directory_invalid")
            if any(type(name) is str and name.endswith(".key") for name in names):
                _fail("legacy_key_present")
        finally:
            os.close(certs_fd)
    finally:
        os.close(root_fd)


def _validate_request(source: str, legacy_root: str, *, deadline: float | None = None) -> list[tuple[str, bytes]]:
    _legacy_keys(legacy_root)
    if source:
        try:
            source_abs = os.path.normpath(source)
            root_abs = os.path.normpath(legacy_root)
            if os.path.commonpath((source_abs, root_abs)) == root_abs:
                _fail("source_must_be_separate")
        except OriginKeyError:
            raise
        except BaseException:
            _fail("source_path_invalid")
    keys, _ = _source_payload(source, deadline=deadline)
    return keys


def preflight(source: str, legacy_root: str) -> None:
    """Validate all local custody and bounds before any deployment network step."""
    try:
        _validate_request(source, legacy_root, deadline=_deadline(MAX_OPERATION_SECONDS))
    except OriginKeyError:
        raise
    except BaseException:
        _fail("source_unverifiable")


def _valid_route(host: object, user: object, remote_path: object) -> tuple[str, str, str]:
    if type(host) is not str or not _SAFE_HOST.fullmatch(host) or ".." in host:
        _fail("route_invalid")
    if type(user) is not str or not _SAFE_USER.fullmatch(user):
        _fail("route_invalid")
    if (type(remote_path) is not str or not _SAFE_REMOTE_PATH.fullmatch(remote_path)
            or ".." in remote_path or remote_path.endswith("/")):
        _fail("route_invalid")
    destination = remote_path + "/certs"
    if len(destination) > 1028:
        _fail("route_invalid")
    return host, user, destination


_REMOTE_RECEIVER = r'''import errno,hashlib,json,os,re,selectors,signal,stat,struct,sys,time

deadline=time.monotonic()+120
directory=None
temporary={}

def remaining():
    value=deadline-time.monotonic()
    if value<=0:
        raise ValueError
    return value

def cancelled(signum,frame):
    raise SystemExit(2)

signal.signal(signal.SIGTERM,cancelled)
signal.signal(signal.SIGINT,cancelled)

def emit(value):
    raw=json.dumps(value,separators=(",",":")).encode("ascii")+b"\n"
    if len(raw)>8192:
        raise ValueError
    remaining()
    os.write(1,raw)

def readn(size):
    result=bytearray()
    with selectors.DefaultSelector() as selected:
        selected.register(0,selectors.EVENT_READ)
        while len(result)<size:
            if not selected.select(remaining()):
                continue
            chunk=os.read(0,min(65536,size-len(result)))
            if not chunk:
                raise ValueError
            result.extend(chunk)
    return bytes(result)

def line():
    result=bytearray()
    while len(result)<=8192:
        chunk=readn(1)
        result.extend(chunk)
        if chunk==b"\n":
            return bytes(result)
    raise ValueError

def eof():
    with selectors.DefaultSelector() as selected:
        selected.register(0,selectors.EVENT_READ)
        while not selected.select(remaining()):
            pass
        if os.read(0,1):
            raise ValueError

def acl(fd,mode,directory,private=True,sticky=False):
    no=(errno.ENODATA,getattr(errno,"ENOATTR",errno.ENODATA))
    try:
        raw=os.getxattr(fd,"system.posix_acl_access")
    except OSError as error:
        if error.errno not in no:
            raise ValueError
        raw=None
    if raw is not None:
        if len(raw)<4 or (len(raw)-4)%8 or len(raw)>2052 or struct.unpack_from("<I",raw)[0]!=2:
            raise ValueError
        entries={}
        for offset in range(4,len(raw),8):
            tag,permission,identity=struct.unpack_from("<HHI",raw,offset)
            if permission&~7:
                raise ValueError
            entries.setdefault(tag,[]).append((permission,identity))
        if (set(entries)-{1,4,16,32} or
                any(len(entries.get(tag,[]))!=1 for tag in (1,4,32)) or
                len(entries.get(16,[]))>1):
            raise ValueError
        group=entries[4][0][0]
        other=entries[32][0][0]
        mask=entries.get(16,[(group,0)])[0][0]
        if entries[1][0][0]!=((mode>>6)&7):
            raise ValueError
        if private and (group or other or mask):
            raise ValueError
        if not private and ((group&mask)!=((mode>>3)&7) or other!=(mode&7) or
                            (not sticky and ((group|other|mask)&2))):
            raise ValueError
    if directory and private:
        try:
            os.getxattr(fd,"system.posix_acl_default")
        except OSError as error:
            if error.errno not in no:
                raise ValueError
        else:
            raise ValueError

def checked_dir(path):
    if (type(path)is not str or not path.startswith("/") or len(path)>1028 or
            any(part in ("",".","..") for part in path.split("/")[1:])):
        raise ValueError
    parts=path.split("/")[1:]
    fd=os.open("/",os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for index,part in enumerate(parts):
            remaining()
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd)
            fd=child
            info=os.fstat(fd)
            mode=stat.S_IMODE(info.st_mode)
            leaf=index==len(parts)-1
            sticky=info.st_uid==0 and bool(mode&stat.S_ISVTX)
            if info.st_uid not in (0,os.geteuid()) or (not leaf and mode&0o022 and not sticky):
                raise ValueError
            acl(fd,mode,True,private=leaf,sticky=sticky)
        info=os.fstat(fd)
        if info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o700:
            raise ValueError
        return fd,info
    except BaseException:
        os.close(fd)
        raise

def identity(info):
    return (info.st_dev,info.st_ino,info.st_uid,info.st_mode,info.st_nlink)

def snapshot(info):
    return identity(info)+(info.st_size,info.st_mtime_ns,info.st_ctime_ns)

def verifydir(expected):
    remaining()
    current=os.fstat(directory)
    if identity(current)!=identity(expected):
        raise ValueError
    acl(directory,stat.S_IMODE(current.st_mode),True)

def target(name):
    try:
        fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
    except FileNotFoundError:
        return None
    try:
        info=os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or
                stat.S_IMODE(info.st_mode)!=0o600 or info.st_nlink!=1):
            raise ValueError
        acl(fd,stat.S_IMODE(info.st_mode),False)
        return snapshot(info)
    finally:
        os.close(fd)

try:
    header=json.loads(line())
    if (type(header)is not dict or set(header)!={"v","destination","keys","nonce","timeout_ms"} or
            type(header["v"])is not int or header["v"]!=1 or
            type(header["destination"])is not str or header["destination"]!=sys.argv[-1] or
            type(header["nonce"])is not str or not re.fullmatch("[0-9a-f]{32}",header["nonce"]) or
            type(header["timeout_ms"])is not int or not 0<header["timeout_ms"]<=120000):
        raise ValueError
    deadline=min(deadline,time.monotonic()+header["timeout_ms"]/1000)
    keys=header["keys"]
    if type(keys)is not list or not 0<len(keys)<=32:
        raise ValueError
    names=[]
    total=0
    for item in keys:
        if type(item)is not dict or set(item)!={"name","size","sha256"}:
            raise ValueError
        name=item["name"]
        size=item["size"]
        if (type(name)is not str or len(name)>128 or
                not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*\.key",name) or
                type(size)is not int or not 0<size<=65536 or
                type(item["sha256"])is not str or not re.fullmatch("[0-9a-f]{64}",item["sha256"])):
            raise ValueError
        names.append(name)
        total+=size
    if len(set(names))!=len(names) or names!=sorted(names) or total>2*1024*1024:
        raise ValueError
    directory,original=checked_dir(sys.argv[-1])
    before={name:target(name) for name in names}
    verifydir(original)
    emit({"status":"ready","count":len(names),"nonce":header["nonce"]})
    staged=[]
    for item in keys:
        verifydir(original)
        name=item["name"]
        size=item["size"]
        data=readn(size)
        if hashlib.sha256(data).hexdigest()!=item["sha256"]:
            raise ValueError
        temp=".origin-key-"+os.urandom(16).hex()+".tmp"
        fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
        created=os.fstat(fd)
        temporary[temp]=identity(created)
        try:
            if (created.st_uid!=os.geteuid() or stat.S_IMODE(created.st_mode)!=0o600 or
                    created.st_nlink!=1 or not stat.S_ISREG(created.st_mode)):
                raise ValueError
            acl(fd,stat.S_IMODE(created.st_mode),False)
            view=memoryview(data)
            offset=0
            while offset<len(view):
                remaining()
                written=os.write(fd,view[offset:])
                if written<=0:
                    raise ValueError
                offset+=written
            os.fsync(fd)
            after=os.fstat(fd)
            if identity(created)!=identity(after) or after.st_size!=size:
                raise ValueError
            acl(fd,stat.S_IMODE(after.st_mode),False)
            staged.append((temp,name,snapshot(after)))
        finally:
            os.close(fd)
    eof()
    verifydir(original)
    if any(target(name)!=before[name] for name in names):
        raise ValueError
    for temp,name,expected in staged:
        verifydir(original)
        current=os.stat(temp,dir_fd=directory,follow_symlinks=False)
        if snapshot(current)!=expected or target(name)!=before[name]:
            raise ValueError
        os.replace(temp,name,src_dir_fd=directory,dst_dir_fd=directory)
        temporary.pop(temp)
        # Rename changes ctime; identity, size and mtime must remain exact.
        if target(name)[:7]!=expected[:7]:
            raise ValueError
    os.fsync(directory)
    verifydir(original)
    emit({"status":"complete","count":len(names),"names":names,"nonce":header["nonce"]})
except SystemExit:
    raise
except BaseException:
    try:
        emit({"status":"refused","code":"transfer_failed"})
    except BaseException:
        pass
    raise SystemExit(2) from None
finally:
    if directory is not None:
        for name,owned in temporary.items():
            try:
                info=os.stat(name,dir_fd=directory,follow_symlinks=False)
                if identity(info)==owned:
                    os.unlink(name,dir_fd=directory)
            except BaseException:
                pass
        os.close(directory)
'''


def _header(destination: str, keys: list[tuple[str, bytes]], *,
            nonce: str | None = None, timeout_ms: int = 120000) -> bytes:
    total = sum(len(data) for _, data in keys)
    if not keys or len(keys) > MAX_KEYS or total > MAX_TOTAL_BYTES:
        _fail("source_batch_invalid")
    value = {"v": 1, "destination": destination,
             "keys": [{"name": name, "size": len(data),
                       "sha256": hashlib.sha256(data).hexdigest()} for name, data in keys],
             "nonce": nonce or os.urandom(16).hex(), "timeout_ms": timeout_ms}
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"
    if len(raw) > MAX_HEADER_BYTES:
        _fail("header_too_large")
    return raw


def _deadline(timeout: float) -> float:
    if type(timeout) not in (int, float) or isinstance(timeout, bool):
        _fail("deadline_invalid")
    value = float(timeout)
    if not math.isfinite(value) or not 0 < value <= MAX_OPERATION_SECONDS:
        _fail("deadline_invalid")
    return time.monotonic() + value


def _remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        _fail("deadline_exceeded", ambiguous=True)
    return value


def _read_line(process: subprocess.Popen[bytes], deadline: float, limit: int) -> bytes:
    if process.stdout is None:
        _fail("transport_pipe_invalid", ambiguous=True)
    fd = process.stdout.fileno()
    os.set_blocking(fd, False)
    received = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while len(received) <= limit:
            events = selector.select(_remaining(deadline))
            if not events:
                continue
            try:
                part = os.read(fd, min(512, limit + 1 - len(received)))
            except BlockingIOError:
                continue
            except OSError:
                _fail("transport_read_failed", ambiguous=True)
            if not part:
                _fail("transport_truncated", ambiguous=True)
            received.extend(part)
            if b"\n" in received:
                line, extra = bytes(received).split(b"\n", 1)
                if extra:
                    _fail("transport_extra_output", ambiguous=True)
                return line + b"\n"
    _fail("transport_receipt_too_large", ambiguous=True)


def _send_bytes(process: subprocess.Popen[bytes], payload: bytes, deadline: float) -> None:
    if process.stdin is None:
        _fail("transport_pipe_invalid", ambiguous=True)
    fd = process.stdin.fileno()
    os.set_blocking(fd, False)
    offset = 0
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdin, selectors.EVENT_WRITE)
        while offset < len(payload):
            selector.select(_remaining(deadline))
            try:
                count = os.write(fd, payload[offset:offset + 65536])
            except BlockingIOError:
                continue
            except OSError:
                _fail("transport_write_failed", ambiguous=offset > 0)
            if count <= 0:
                _fail("transport_write_failed", ambiguous=offset > 0)
            offset += count


def _parse_line(raw: bytes, expected: dict[str, object], code: str) -> dict[str, object]:
    if type(raw) is not bytes or len(raw) > MAX_RECEIPT_BYTES or not raw.endswith(b"\n"):
        _fail(code, ambiguous=True)
    try:
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError
                result[key] = value
            return result
        value = json.loads(raw[:-1].decode("ascii"), object_pairs_hook=pairs)
    except BaseException:
        _fail(code, ambiguous=True)
    if (type(value) is not dict or value != expected
            or any(type(value.get(key)) is not type(item) for key, item in expected.items())):
        _fail(code, ambiguous=True)
    return value


def _ssh_argv(host: str, user: str, destination: str) -> list[str]:
    remote = (PYTHON + " -I -c " + shlex.quote(_REMOTE_RECEIVER)
              + " -- " + shlex.quote(destination))
    return [SSH, "-oBatchMode=yes", "-oStrictHostKeyChecking=accept-new",
            "-oConnectTimeout=15", user + "@" + host, remote]


def _terminate(process: subprocess.Popen[bytes]) -> None:
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=0.25)
    except BaseException:
        pass
    for pipe in (process.stdin, process.stdout, process.stderr):
        try:
            if pipe is not None:
                pipe.close()
        except BaseException:
            pass


def transfer(source: str, host: str, user: str, remote_path: str,
             legacy_root: str, *, timeout: float = MAX_OPERATION_SECONDS) -> None:
    """Validate, handshake, stream once, and require an exact whole-batch receipt."""
    deadline = _deadline(timeout)
    try:
        keys = _validate_request(source, legacy_root, deadline=deadline)
        _remaining(deadline)
    except OriginKeyError:
        raise
    except BaseException:
        _fail("source_unverifiable")
    if not keys:
        return
    host, user, destination = _valid_route(host, user, remote_path)
    nonce = os.urandom(16).hex()
    header = _header(destination, keys, nonce=nonce,
                     timeout_ms=max(1, int(_remaining(deadline) * 1000)))
    payload = b"".join(data for _, data in keys)
    process: subprocess.Popen[bytes] | None = None
    sent = False
    try:
        process = subprocess.Popen(
            _ssh_argv(host, user, destination), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, shell=False,
            close_fds=True, bufsize=0, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        )
        _send_bytes(process, header, deadline)
        if process.stdin is None:
            _fail("transport_pipe_invalid")
        # The receiver emits READY only after destination directory, ACLs, and
        # every intended existing filename have passed the private checks.
        _parse_line(_read_line(process, deadline, MAX_RECEIPT_BYTES),
                    {"status": "ready", "count": len(keys), "nonce": nonce}, "ready_invalid")
        sent = True
        _send_bytes(process, payload, deadline)
        process.stdin.close()
        process.stdin = None
        expected = {"status": "complete", "count": len(keys),
                    "names": [name for name, _ in keys], "nonce": nonce}
        _parse_line(_read_line(process, deadline, MAX_RECEIPT_BYTES), expected,
                    "receipt_invalid")
        remaining = _remaining(deadline)
        if process.wait(timeout=remaining) != 0:
            _fail("transport_failed", ambiguous=True)
        _remaining(deadline)
        if process.stdout is not None:
            # A descendant retaining the pipe must not become unbounded EOF wait.
            with selectors.DefaultSelector() as selected:
                selected.register(process.stdout, selectors.EVENT_READ)
                if not selected.select(_remaining(deadline)):
                    _fail("deadline_exceeded", ambiguous=True)
                if os.read(process.stdout.fileno(), 1):
                    _fail("transport_extra_output", ambiguous=True)
        _remaining(deadline)
    except OriginKeyError as exc:
        if process is not None:
            _terminate(process)
        if sent and not exc.ambiguous:
            _fail(exc.code, ambiguous=True)
        raise
    except BaseException:
        if process is not None:
            _terminate(process)
        _fail("transport_failed", ambiguous=sent)
    finally:
        if process is not None:
            for pipe in (process.stdin, process.stdout, process.stderr):
                try:
                    if pipe is not None:
                        pipe.close()
                except BaseException:
                    pass


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "transfer"):
        item = commands.add_parser(name, add_help=False)
        item.add_argument("--source", required=True)
        item.add_argument("--legacy-root", required=True)
        if name == "transfer":
            item.add_argument("--host", required=True)
            item.add_argument("--user", required=True)
            item.add_argument("--remote-path", required=True)
    try:
        args = parser.parse_args(argv)
        if args.command == "preflight":
            preflight(args.source, args.legacy_root)
        else:
            transfer(args.source, args.host, args.user, args.remote_path,
                     args.legacy_root)
    except OriginKeyError as exc:
        print("private-origin-keys: " + exc.code, file=sys.stderr)
        return 1
    except SystemExit:
        raise
    except BaseException:
        print("private-origin-keys: request_invalid", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
