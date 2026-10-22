"""Strict, read-only checks for the pre-provisioned deployment Python slice.

This module validates a small versioned JSON contract and the corresponding
hashed requirements files. It never installs, repairs, imports or executes a
deployment package or console entry point.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import importlib.metadata
import json
import os
import platform
import re
import sys
import stat
from pathlib import Path


CONTRACT_ID = "prebuilt-deployment-python-v1"
SCHEMA_VERSION = 1
PROFILES = ("controller", "target-parser")
CONTRACT_PATH = Path(__file__).resolve().parents[2] / "toolchains" / "deploy-python.json"
MAX_CONTRACT_BYTES = 16 * 1024
MAX_REQUIREMENTS_BYTES = 4 * 1024
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_WHEEL_TEMPLATES = {
    "PyYAML": "pyyaml-{version}-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl",
    "django-core-micha": "django_core_micha-{version}-py3-none-any.whl",
}
_VERSION = re.compile(r"(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})\Z")
_PROFILE_DISTS = {
    "controller": ("PyYAML", "django-core-micha"),
    "target-parser": ("PyYAML",),
}
_PROFILE_REQUIREMENTS = {
    "controller": "deploy-python-requirements.txt",
    "target-parser": "compose-parser-requirements.txt",
}
_ENTRYPOINTS = {
    "controller": {
        "generate-env": ("django-core-micha", "django_core_micha.scripts.generate_env:main"),
    },
    "target-parser": {},
}
_REQ_HEADER = b"# Generated from deploy-python.json; provisioning only: --require-hashes --only-binary=:all: --no-deps.\n"


class ToolchainError(ValueError):
    """An error with a fixed, safe public code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise ToolchainError(code) from None


@dataclass(frozen=True)
class DistributionPin:
    name: str
    version: str
    wheel: str
    sha256: str


@dataclass(frozen=True)
class Profile:
    name: str
    distributions: tuple[str, ...]
    requirements: str
    entrypoints: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True)
class ToolchainContract:
    contract_id: str
    schema_version: int
    implementation: str
    python: str
    system: str
    machine: str
    distributions: tuple[DistributionPin, ...]
    profiles: tuple[Profile, ...]


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    profile: str
    contract_id: str
    code: str


def _pairs_no_duplicates(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("contract_duplicate_key")
        result[key] = value
    return result


def _strict_json(raw: bytes | bytearray) -> object:
    if type(raw) not in (bytes, bytearray):
        _fail("contract_bytes_required")
    if len(raw) > MAX_CONTRACT_BYTES:
        _fail("contract_too_large")
    try:
        return json.loads(bytes(raw).decode("utf-8", "strict"), object_pairs_hook=_pairs_no_duplicates,
                          parse_constant=lambda _value: _fail("contract_invalid_json"))
    except ToolchainError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError, TypeError):
        _fail("contract_invalid_json")


def _object(value: object, keys: set[str], code: str) -> dict:
    if type(value) is not dict or set(value) != keys:
        _fail(code)
    return value


def _plain(value: object, pattern: re.Pattern[str], code: str) -> str:
    if not isinstance(value, str) or len(value) > 256 or not pattern.fullmatch(value):
        _fail(code)
    return value


def parse_contract(raw: bytes | bytearray) -> ToolchainContract:
    """Parse and validate the complete bounded contract against local allowlists."""
    doc = _strict_json(raw)
    root = _object(doc, {"schema_version", "contract_id", "runtime", "distributions", "profiles"}, "contract_fields_invalid")
    if type(root["schema_version"]) is not int or root["schema_version"] != SCHEMA_VERSION:
        _fail("contract_schema_unsupported")
    if root["contract_id"] != CONTRACT_ID:
        _fail("contract_id_unsupported")
    runtime = _object(root["runtime"], {"implementation", "python", "system", "machine"}, "runtime_fields_invalid")
    if runtime != {"implementation": "CPython", "python": "3.12", "system": "Linux", "machine": "x86_64"}:
        _fail("runtime_contract_invalid")

    raw_dists = root["distributions"]
    if type(raw_dists) is not list or len(raw_dists) != len(_WHEEL_TEMPLATES):
        _fail("distribution_set_invalid")
    pins: list[DistributionPin] = []
    seen: set[str] = set()
    for item in raw_dists:
        entry = _object(item, {"name", "version", "wheel", "sha256"}, "distribution_fields_invalid")
        name = entry["name"]
        if not isinstance(name, str) or name not in _WHEEL_TEMPLATES or name in seen:
            _fail("distribution_identity_invalid")
        version = _plain(entry["version"], _VERSION, "distribution_pin_invalid")
        wheel = _WHEEL_TEMPLATES[name].format(version=version)
        digest = _plain(entry["sha256"], _HASH, "distribution_pin_invalid")
        if entry["wheel"] != wheel:
            _fail("distribution_pin_invalid")
        seen.add(name)
        pins.append(DistributionPin(name, version, wheel, digest))
    if seen != set(_WHEEL_TEMPLATES):
        _fail("distribution_set_invalid")

    raw_profiles = _object(root["profiles"], set(PROFILES), "profile_set_invalid")
    profiles: list[Profile] = []
    for name in PROFILES:
        item = _object(raw_profiles[name], {"distributions", "requirements", "entrypoints"}, "profile_fields_invalid")
        dists = item["distributions"]
        if type(dists) is not list or tuple(dists) != _PROFILE_DISTS[name]:
            _fail("profile_distribution_set_invalid")
        requirements = item["requirements"]
        if requirements != _PROFILE_REQUIREMENTS[name]:
            _fail("profile_requirements_path_invalid")
        eps = _object(item["entrypoints"], set(_ENTRYPOINTS[name]), "profile_entrypoints_invalid")
        parsed_eps = []
        for ep_name, target in _ENTRYPOINTS[name].items():
            spec = _object(eps[ep_name], {"distribution", "value"}, "entrypoint_fields_invalid")
            if (spec["distribution"], spec["value"]) != target:
                _fail("entrypoint_identity_invalid")
            parsed_eps.append((ep_name, target[0], target[1]))
        profiles.append(Profile(name, tuple(dists), requirements, tuple(parsed_eps)))
    return ToolchainContract(CONTRACT_ID, SCHEMA_VERSION, "CPython", "3.12", "Linux", "x86_64",
                             tuple(pins), tuple(profiles))


def _profile(contract: ToolchainContract, profile: str) -> Profile:
    if not isinstance(contract, ToolchainContract):
        _fail("contract_object_invalid")
    if profile not in PROFILES:
        _fail("profile_unsupported")
    for candidate in contract.profiles:
        if candidate.name == profile:
            return candidate
    _fail("profile_missing")


def render_requirements(contract: ToolchainContract, profile: str) -> bytes:
    """Return canonical hash-pinned requirements bytes; this function never writes."""
    selected = _profile(contract, profile)
    pins = {pin.name: pin for pin in contract.distributions}
    lines = [_REQ_HEADER]
    for name in selected.distributions:
        pin = pins[name]
        lines.append(f"{pin.name}=={pin.version} --hash=sha256:{pin.sha256}\n".encode("ascii"))
    return b"".join(lines)


class MetadataCollector:
    """Read selected installed distribution metadata without loading package code."""

    def __init__(self, versions: dict[str, str]):
        self._versions = versions

    def _distribution(self, distribution: str):
        # Wheel-only supported installation: no eggs, ZIPs or metadata search
        # fallbacks that parse unrelated/unbounded distribution files.
        directory = distribution.replace("-", "_").lower() + "-" + self._versions[distribution] + ".dist-info"
        if len(sys.path) > 64:
            _fail("metadata_unavailable")
        found = []
        roots = set()
        for root in sys.path:
            if type(root) is not str or not root or len(root) > 4096:
                _fail("metadata_unavailable")
            base = Path(root).resolve()
            if base in roots:
                continue
            roots.add(base)
            candidate = base / directory
            try:
                identity = candidate.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(identity.st_mode):
                _fail("metadata_unavailable")
            found.append(candidate)
        if not found:
            raise importlib.metadata.PackageNotFoundError(distribution)
        if len(found) != 1:
            _fail("metadata_unavailable")
        return _BoundedDistribution(found[0])

    def version(self, distribution: str) -> str | None:
        try:
            metadata = self._distribution(distribution).metadata
            names = metadata.get_all("Name", [])
            versions = metadata.get_all("Version", [])
            if (len(names) != 1 or len(versions) != 1
                    or names[0].lower().replace("_", "-") != distribution.lower().replace("_", "-")):
                _fail("metadata_unavailable")
            return versions[0]
        except importlib.metadata.PackageNotFoundError:
            return None
        except Exception:
            _fail("metadata_unavailable")

    def entrypoints(self, distribution: str) -> tuple[tuple[str, str], ...]:
        try:
            dist = self._distribution(distribution)
            eps = dist.entry_points
            found = []
            for index, ep in enumerate(eps):
                if index >= 128:
                    _fail("metadata_unavailable")
                if ep.group == "console_scripts":
                    if (type(ep.name) is not str or type(ep.value) is not str
                            or len(ep.name) > 128 or len(ep.value) > 512):
                        _fail("metadata_unavailable")
                    found.append((ep.name, ep.value))
            return tuple(found)
        except importlib.metadata.PackageNotFoundError:
            return ()
        except Exception:
            _fail("metadata_unavailable")


class _BoundedDistribution(importlib.metadata.Distribution):
    """Stdlib metadata parser with bounded regular-file reads before parsing."""

    def __init__(self, directory: Path):
        self.directory = directory

    def read_text(self, filename):
        if filename not in ("METADATA", "entry_points.txt"):
            return None
        path = self.directory / filename
        try:
            return _read_bounded(path, 64 * 1024).decode("utf-8", "strict")
        except FileNotFoundError:
            return None

    def locate_file(self, path):
        _fail("metadata_unavailable")


def _runtime_values() -> tuple[str, str, str, str]:
    return (platform.python_implementation(), f"{sys.version_info.major}.{sys.version_info.minor}",
            platform.system(), platform.machine())


def _source_parser_version() -> str:
    """Read the reviewed literal without importing or executing target code."""
    source = Path(__file__).with_name("prebuilt_target.py")
    try:
        tree = ast.parse(_read_bounded(source, 256 * 1024).decode("utf-8"))
    except Exception:
        _fail("parser_pin_unavailable")
    bindings = [node for node in ast.walk(tree) if isinstance(node, ast.Name)
                and node.id == "COMPOSE_PARSER_VERSION" and isinstance(node.ctx, (ast.Store, ast.Del))]
    if len(bindings) != 1:
        _fail("parser_pin_unavailable")
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "COMPOSE_PARSER_VERSION"):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value
    _fail("parser_pin_unavailable")


def check_runtime(
    contract: ToolchainContract,
    profile: str,
    requirements: bytes | bytearray,
    *,
    collector: object | None = None,
    runtime: tuple[str, str, str, str] | None = None,
) -> CheckResult:
    """Compare pinned requirements and selected installed metadata with safe results."""
    selected = _profile(contract, profile)
    if type(requirements) not in (bytes, bytearray) or len(requirements) > MAX_REQUIREMENTS_BYTES:
        _fail("requirements_input_invalid")
    if bytes(requirements).replace(b"\r\n", b"\n") != render_requirements(contract, profile):
        _fail("requirements_drift")
    actual_runtime = runtime if runtime is not None else _runtime_values()
    if actual_runtime != (contract.implementation, contract.python, contract.system, contract.machine):
        return CheckResult(False, profile, contract.contract_id, "runtime_unsupported")
    pins = {pin.name: pin for pin in contract.distributions}
    metadata = MetadataCollector({name: pins[name].version for name in selected.distributions}) if collector is None else collector
    for name in selected.distributions:
        try:
            observed = metadata.version(name)
        except ToolchainError:
            raise
        except Exception:
            _fail("metadata_unavailable")
        if observed is None:
            return CheckResult(False, profile, contract.contract_id, "distribution_missing")
        if observed != pins[name].version:
            return CheckResult(False, profile, contract.contract_id, "distribution_version_mismatch")
    for ep_name, distribution, value in selected.entrypoints:
        try:
            entries = metadata.entrypoints(distribution)
            hits = tuple((n, v) for n, v in entries if n == ep_name)
        except ToolchainError:
            raise
        except Exception:
            _fail("metadata_unavailable")
        if len(hits) != 1:
            return CheckResult(False, profile, contract.contract_id, "entrypoint_missing_or_ambiguous")
        if hits[0][1] != value:
            return CheckResult(False, profile, contract.contract_id, "entrypoint_mismatch")
    # The image executor's parser pin must remain exactly the checked contract pin.
    if "PyYAML" in selected.distributions:
        if _source_parser_version() != pins["PyYAML"].version:
            return CheckResult(False, profile, contract.contract_id, "parser_pin_mismatch")
    return CheckResult(True, profile, contract.contract_id, "ok")


def _read_bounded(path: Path, limit: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            _fail("input_not_regular")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return _read_stream_bounded(stream, limit)
    finally:
        os.close(descriptor)


def _read_stream_bounded(stream, limit: int) -> bytes:
    data = stream.read(limit + 1)
    if len(data) > limit:
        _fail("input_too_large")
    return data


def _main(argv: list[str] | None = None) -> int:
    import argparse

    class SafeParser(argparse.ArgumentParser):
        def error(self, message):
            _fail("arguments_invalid")

    parser = SafeParser(description="Read-only deployment Python toolchain drift check", allow_abbrev=False)
    parser.add_argument("--profile", required=True, choices=PROFILES)
    profile = "unknown"
    try:
        args = parser.parse_args(argv)
        profile = args.profile
        raw_contract = _read_bounded(CONTRACT_PATH, MAX_CONTRACT_BYTES)
        contract = parse_contract(raw_contract)
        selected = _profile(contract, args.profile)
        req_path = CONTRACT_PATH.parent / selected.requirements
        requirements = _read_bounded(req_path, MAX_REQUIREMENTS_BYTES)
        result = check_runtime(contract, args.profile, requirements)
    except ToolchainError as exc:
        result = CheckResult(False, profile, CONTRACT_ID, exc.code)
    except Exception:
        result = CheckResult(False, profile, CONTRACT_ID, "contract_unavailable")
    print(json.dumps({"ok": result.ok, "profile": result.profile, "contract_id": result.contract_id,
                      "code": result.code}, sort_keys=True, separators=(",", ":")))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
