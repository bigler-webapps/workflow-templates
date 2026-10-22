"""Behavioral tests for the bounded deployment Python toolchain contract."""

from __future__ import annotations

import contextlib
import builtins
from email.message import Message
import importlib.util
import io
import json
from pathlib import Path
import socket
import stat
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest import mock


SCRIPT = Path(__file__).with_name("deploy_toolchain.py")
_spec = importlib.util.spec_from_file_location("deploy_toolchain_under_test", SCRIPT)
toolchain = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules[_spec.name] = toolchain
_spec.loader.exec_module(toolchain)

ROOT = SCRIPT.parents[2]
CONTRACT_PATH = ROOT / "toolchains" / "deploy-python.json"


def fixture_contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def raw_contract(value: dict) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def fake_distribution(name, versions=None, entries=None):
    versions = versions if versions is not None else {"PyYAML":"6.0.3", "django-core-micha":"3.17.0"}
    if name not in versions:
        raise toolchain.importlib.metadata.PackageNotFoundError(name)
    metadata = Message()
    metadata["Name"] = name
    metadata["Version"] = versions[name]
    if entries is None:
        entries = [SimpleNamespace(group="console_scripts", name="generate-env", value="django_core_micha.scripts.generate_env:main")]
    return SimpleNamespace(metadata=metadata, entry_points=entries)


class FakeCollector:
    def __init__(self, versions=None, entrypoints=None):
        self.versions = versions or {"PyYAML": "6.0.3", "django-core-micha": "3.17.0"}
        self.entrypoint_data = entrypoints or {
            "django-core-micha": (("generate-env", "django_core_micha.scripts.generate_env:main"),)
        }
        self.version_calls = []
        self.entrypoint_calls = []

    def version(self, name):
        self.version_calls.append(name)
        return self.versions.get(name)

    def entrypoints(self, name):
        self.entrypoint_calls.append(name)
        return self.entrypoint_data.get(name, ())


class DeployToolchainTests(unittest.TestCase):
    def setUp(self):
        self.raw = CONTRACT_PATH.read_bytes()
        self.contract = toolchain.parse_contract(self.raw)
        self.runtime = ("CPython", "3.12", "Linux", "x86_64")

    def assert_code(self, code, raw):
        with self.assertRaises(toolchain.ToolchainError) as caught:
            toolchain.parse_contract(raw)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def test_contract_accepts_both_exact_profiles_and_the_two_hashes(self):
        self.assertEqual(self.contract.contract_id, "prebuilt-deployment-python-v1")
        self.assertEqual(toolchain._profile(self.contract, "controller").distributions,
                         ("PyYAML", "django-core-micha"))
        self.assertEqual(toolchain._profile(self.contract, "target-parser").distributions, ("PyYAML",))
        self.assertEqual(len(self.contract.distributions), 2)
        self.assertTrue(all(len(pin.sha256) == 64 for pin in self.contract.distributions))

    def test_renderer_matches_parent_requirements_byte_for_byte(self):
        for profile, filename in (("controller", "deploy-python-requirements.txt"),
                                  ("target-parser", "compose-parser-requirements.txt")):
            expected = (ROOT / "toolchains" / filename).read_bytes()
            self.assertEqual(toolchain.render_requirements(self.contract, profile), expected.replace(b"\r\n", b"\n"))

    def test_renderer_is_pure_bytes_and_refuses_unknown_profile(self):
        rendered = toolchain.render_requirements(self.contract, "controller")
        self.assertIs(type(rendered), bytes)
        with self.assertRaises(toolchain.ToolchainError) as caught:
            toolchain.render_requirements(self.contract, "hosted-recovery")
        self.assertEqual(caught.exception.code, "profile_unsupported")

    def test_duplicate_json_key_is_rejected_with_fixed_error(self):
        raw = b'{"schema_version":1,"schema_version":1}'
        self.assert_code("contract_duplicate_key", raw)

    def test_malformed_and_oversized_contracts_refuse(self):
        self.assert_code("contract_invalid_json", b"{nope")
        self.assert_code("contract_too_large", b" " * (toolchain.MAX_CONTRACT_BYTES + 1))
        with self.assertRaises(toolchain.ToolchainError) as caught:
            toolchain.parse_contract("{}")
        self.assertEqual(caught.exception.code, "contract_bytes_required")

    def test_unknown_keys_and_wrong_contract_identity_refuse(self):
        value = fixture_contract()
        value["unexpected"] = "hostile-private-metadata"
        self.assert_code("contract_fields_invalid", raw_contract(value))
        value = fixture_contract()
        value["contract_id"] = "hostile-private-metadata"
        self.assert_code("contract_id_unsupported", raw_contract(value))

    def test_wrong_runtime_or_schema_refuses(self):
        value = fixture_contract()
        value["runtime"]["machine"] = "aarch64"
        self.assert_code("runtime_contract_invalid", raw_contract(value))
        value = fixture_contract()
        value["schema_version"] = True
        self.assert_code("contract_schema_unsupported", raw_contract(value))

    def test_distribution_duplicate_float_forgery_and_hash_drift_refuse(self):
        value = fixture_contract()
        value["distributions"][1] = dict(value["distributions"][0])
        self.assert_code("distribution_identity_invalid", raw_contract(value))
        value = fixture_contract()
        value["distributions"][0]["version"] = ">=6.0"
        self.assert_code("distribution_pin_invalid", raw_contract(value))
        value = fixture_contract()
        value["distributions"][0]["sha256"] = "invalid-hash"
        self.assert_code("distribution_pin_invalid", raw_contract(value))
        value = fixture_contract()
        value["distributions"][0]["wheel"] = "https://invalid.example/wheel.whl"
        self.assert_code("distribution_pin_invalid", raw_contract(value))

    def test_profile_boundary_path_distribution_and_entrypoint_are_exact(self):
        for mutation, code in (
            (lambda v: v["profiles"]["target-parser"]["distributions"].append("django-core-micha"),
             "profile_distribution_set_invalid"),
            (lambda v: v["profiles"]["controller"].update(requirements="../secrets.txt"),
             "profile_requirements_path_invalid"),
            (lambda v: v["profiles"]["controller"]["entrypoints"]["generate-env"].update(
                value="hostile.module:run"), "entrypoint_identity_invalid"),
            (lambda v: v["profiles"]["controller"]["entrypoints"].update(extra={}),
             "profile_entrypoints_invalid"),
        ):
            value = fixture_contract()
            mutation(value)
            self.assert_code(code, raw_contract(value))

    def test_target_parser_checks_yaml_only_and_never_inspects_dcm(self):
        class YamlOnly:
            def __init__(self):
                self.calls = []

            def version(self, name):
                self.calls.append(("version", name))
                if name != "PyYAML":
                    raise AssertionError("target-parser inspected dcm metadata")
                return "6.0.3"

            def entrypoints(self, _name):
                raise AssertionError("target-parser inspected entry points")

        collector = YamlOnly()
        result = toolchain.check_runtime(
            self.contract, "target-parser",
            toolchain.render_requirements(self.contract, "target-parser"),
            collector=collector, runtime=self.runtime,
        )
        self.assertEqual(result, toolchain.CheckResult(True, "target-parser", toolchain.CONTRACT_ID, "ok"))
        self.assertEqual(collector.calls, [("version", "PyYAML")])

    def test_controller_positive_checks_versions_and_exact_console_entrypoint(self):
        collector = FakeCollector()
        result = toolchain.check_runtime(
            self.contract, "controller", toolchain.render_requirements(self.contract, "controller"),
            collector=collector, runtime=self.runtime,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.code, "ok")
        self.assertEqual(collector.version_calls, ["PyYAML", "django-core-micha"])
        self.assertEqual(collector.entrypoint_calls, ["django-core-micha"])

    def test_runtime_and_distribution_drift_return_safe_nonzero_results(self):
        wrong_runtime = toolchain.check_runtime(
            self.contract, "controller", toolchain.render_requirements(self.contract, "controller"),
            collector=FakeCollector(), runtime=("CPython", "3.13", "Linux", "x86_64"),
        )
        self.assertEqual(wrong_runtime.code, "runtime_unsupported")
        missing = FakeCollector(versions={"PyYAML": "6.0.3"})
        result = toolchain.check_runtime(
            self.contract, "controller", toolchain.render_requirements(self.contract, "controller"),
            collector=missing, runtime=self.runtime,
        )
        self.assertEqual(result.code, "distribution_missing")
        wrong = FakeCollector(versions={"PyYAML": "6.0.3", "django-core-micha": "3.16.9"})
        result = toolchain.check_runtime(
            self.contract, "controller", toolchain.render_requirements(self.contract, "controller"),
            collector=wrong, runtime=self.runtime,
        )
        self.assertEqual(result.code, "distribution_version_mismatch")

    def test_entrypoint_missing_duplicate_and_wrong_value_refuse(self):
        cases = [(), (("generate-env", "django_core_micha.scripts.generate_env:main"),) * 2,
                 (("generate-env", "hostile.module:run"),)]
        expected = ["entrypoint_missing_or_ambiguous", "entrypoint_missing_or_ambiguous", "entrypoint_mismatch"]
        for eps, code in zip(cases, expected):
            collector = FakeCollector(entrypoints={"django-core-micha": eps})
            result = toolchain.check_runtime(
                self.contract, "controller", toolchain.render_requirements(self.contract, "controller"),
                collector=collector, runtime=self.runtime,
            )
            self.assertEqual(result.code, code)

    def test_requirements_drift_and_oversize_refuse_before_metadata(self):
        collector = FakeCollector()
        with self.assertRaises(toolchain.ToolchainError) as caught:
            toolchain.check_runtime(self.contract, "controller", b"PyYAML>=6", collector=collector,
                                    runtime=self.runtime)
        self.assertEqual(caught.exception.code, "requirements_drift")
        self.assertEqual(collector.version_calls, [])
        with self.assertRaises(toolchain.ToolchainError) as caught:
            toolchain.check_runtime(self.contract, "controller", b" " * (toolchain.MAX_REQUIREMENTS_BYTES + 1),
                                    collector=collector, runtime=self.runtime)
        self.assertEqual(caught.exception.code, "requirements_input_invalid")

    def test_default_collector_uses_stdlib_metadata_without_loading_or_running_code(self):
        raw_eps = (("generate-env", "django_core_micha.scripts.generate_env:main"),)
        load = mock.Mock(side_effect=AssertionError("package load"))
        entries = [SimpleNamespace(group="console_scripts", name=n, value=v, load=load) for n, v in raw_eps]
        def selected(name):
            return fake_distribution(name, entries=entries)
        with mock.patch.object(toolchain.MetadataCollector, "_distribution", side_effect=selected) as distribution, \
                mock.patch.object(subprocess, "Popen", side_effect=AssertionError("process execution")), \
                mock.patch.object(socket, "socket", side_effect=AssertionError("network socket")):
            result = toolchain.check_runtime(
                self.contract, "controller", toolchain.render_requirements(self.contract, "controller"),
                runtime=self.runtime,
            )
        self.assertTrue(result.ok)
        self.assertEqual(distribution.call_args_list, [mock.call("PyYAML"), mock.call("django-core-micha"), mock.call("django-core-micha")])
        load.assert_not_called()

    def test_cli_emits_fixed_json_and_exit_status(self):
        requirements = toolchain.render_requirements(self.contract, "target-parser")
        original_read = toolchain._read_bounded

        def safe_read(path, limit):
            if path.name == "deploy-python.json":
                return self.raw
            if path.name == "compose-parser-requirements.txt":
                return requirements
            return original_read(path, limit)

        stdout = io.StringIO()
        collector = FakeCollector()
        with mock.patch.object(toolchain, "_read_bounded", safe_read), \
                mock.patch.object(toolchain.MetadataCollector, "_distribution", side_effect=fake_distribution), \
                mock.patch.object(toolchain, "_runtime_values", return_value=self.runtime), contextlib.redirect_stdout(stdout):
            status = toolchain._main(["--profile", "target-parser"])
        self.assertEqual(status, 0)
        self.assertEqual(stdout.getvalue(), '{"code":"ok","contract_id":"prebuilt-deployment-python-v1",'
                         '"ok":true,"profile":"target-parser"}\n')

        stdout = io.StringIO()
        with mock.patch.object(toolchain, "_read_bounded", side_effect=PermissionError("hostile-private-metadata")), \
                contextlib.redirect_stdout(stdout):
            status = toolchain._main(["--profile", "target-parser"])
        self.assertEqual(status, 1)
        self.assertNotIn("hostile-private-metadata", stdout.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())["code"], "contract_unavailable")

    def test_versions_and_hashes_come_from_reviewed_manifest_not_second_pin_table(self):
        value = fixture_contract()
        value["distributions"][1].update(version="3.18.1", wheel="django_core_micha-3.18.1-py3-none-any.whl", sha256="1" * 64)
        contract = toolchain.parse_contract(raw_contract(value))
        requirements = toolchain.render_requirements(contract, "controller")
        self.assertIn(b"django-core-micha==3.18.1 --hash=sha256:" + b"1" * 64, requirements)
        result = toolchain.check_runtime(contract, "controller", requirements,
            collector=FakeCollector(versions={"PyYAML": "6.0.3", "django-core-micha": "3.18.1"}), runtime=self.runtime)
        self.assertTrue(result.ok)
        with self.assertRaises(toolchain.ToolchainError):
            toolchain.check_runtime(contract, "controller", toolchain.render_requirements(self.contract, "controller"),
                collector=FakeCollector(), runtime=self.runtime)

    def test_real_runtime_collector_uses_canonical_cpython_name(self):
        self.assertEqual(toolchain._runtime_values()[0], "CPython")

    def test_cli_real_checker_success_refusal_and_safe_invalid_arguments(self):
        for profile, versions, expected in (("target-parser", {"PyYAML": "6.0.3"}, "ok"),
                ("controller", {"PyYAML":"6.0.3", "django-core-micha":"3.17.0"}, "ok"),
                ("controller", {"PyYAML":"6.0.3", "django-core-micha":"3.16.9"}, "distribution_version_mismatch"),
                ("target-parser", {"PyYAML": "6.0.2"}, "distribution_version_mismatch")):
            stdout = io.StringIO()
            with mock.patch.object(toolchain.MetadataCollector, "_distribution", side_effect=lambda n: fake_distribution(n, versions)), \
                    mock.patch.object(toolchain, "_runtime_values", return_value=self.runtime), \
                    contextlib.redirect_stdout(stdout):
                status = toolchain._main(["--profile", profile])
            self.assertEqual(status, 0 if expected == "ok" else 1)
            self.assertEqual(json.loads(stdout.getvalue())["code"], expected)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = toolchain._main(["--profile", "hostile-private-metadata"])
        self.assertEqual(status, 1)
        self.assertNotIn("hostile-private-metadata", stdout.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())["code"], "arguments_invalid")

    def test_parser_pin_drift_refuses_valid_installed_metadata(self):
        with mock.patch.object(toolchain, "_source_parser_version", return_value="6.0.2"):
            result = toolchain.check_runtime(self.contract, "target-parser",
                toolchain.render_requirements(self.contract, "target-parser"),
                collector=FakeCollector(), runtime=self.runtime)
        self.assertEqual(result.code, "parser_pin_mismatch")

    def test_metadata_failure_never_echoes_hostile_exception(self):
        stdout = io.StringIO()
        with mock.patch.object(toolchain.MetadataCollector, "_distribution", side_effect=RuntimeError("hostile-private-metadata")), \
                mock.patch.object(toolchain, "_runtime_values", return_value=self.runtime), contextlib.redirect_stdout(stdout):
            status = toolchain._main(["--profile", "controller"])
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(stdout.getvalue())["code"], "metadata_unavailable")
        self.assertNotIn("hostile-private-metadata", stdout.getvalue())

    def test_bound_reads_request_limit_plus_one_and_refuse_oversize(self):
        stream = mock.MagicMock()
        stream.__enter__.return_value = stream
        stream.read.return_value = b"12345"
        with mock.patch.object(toolchain.os, "open", return_value=42) as opened, \
                mock.patch.object(toolchain.os, "fstat", return_value=SimpleNamespace(st_mode=stat.S_IFREG)), \
                mock.patch.object(toolchain.os, "fdopen", return_value=stream), mock.patch.object(toolchain.os, "close") as closed:
            with self.assertRaises(toolchain.ToolchainError) as caught:
                toolchain._read_bounded(Path("contract.json"), 4)
        self.assertEqual(caught.exception.code, "input_too_large")
        self.assertEqual(opened.call_args.args[0], Path("contract.json"))
        self.assertTrue(opened.call_args.args[1] & toolchain.os.O_NONBLOCK)
        self.assertTrue(opened.call_args.args[1] & toolchain.os.O_NOFOLLOW)
        closed.assert_called_once_with(42)
        stream.read.assert_called_once_with(5)

    def test_default_checker_never_loads_or_executes_package_code(self):
        ep = SimpleNamespace(group="console_scripts", name="generate-env", value="django_core_micha.scripts.generate_env:main",
            load=mock.Mock(side_effect=AssertionError("package loaded")))
        with mock.patch.object(toolchain.MetadataCollector, "_distribution", side_effect=lambda n: fake_distribution(n, entries=[ep])), \
                mock.patch.object(subprocess, "Popen", side_effect=AssertionError("process")), \
                mock.patch.object(socket, "socket", side_effect=AssertionError("network")):
            result = toolchain.check_runtime(self.contract, "controller",
                toolchain.render_requirements(self.contract, "controller"), runtime=self.runtime)
        self.assertTrue(result.ok)
        ep.load.assert_not_called()

    def test_real_stdlib_metadata_parser_is_bounded_before_parsing(self):
        dist = toolchain._BoundedDistribution(Path("/not-accessed/package.dist-info"))
        for filename, content in (("METADATA", b"Metadata-Version: 2.1\nName: PyYAML\nVersion: 6.0.3\n"),
                ("entry_points.txt", b"[console_scripts]\ngenerate-env = django_core_micha.scripts.generate_env:main\n")):
            stream = io.BytesIO(content)
            with mock.patch.object(toolchain.os, "open", return_value=42) as opened, \
                    mock.patch.object(toolchain.os, "fstat", return_value=SimpleNamespace(st_mode=stat.S_IFREG)), \
                    mock.patch.object(toolchain.os, "fdopen", return_value=stream), mock.patch.object(toolchain.os, "close") as closed:
                parsed = dist.metadata if filename == "METADATA" else dist.entry_points
                if filename == "METADATA":
                    self.assertEqual(parsed["Version"], "6.0.3")
                else:
                    self.assertEqual([(ep.name,ep.value) for ep in parsed], [("generate-env","django_core_micha.scripts.generate_env:main")])
            self.assertEqual(opened.call_args.args[0].name, filename)
            self.assertTrue(opened.call_args.args[1] & toolchain.os.O_NOFOLLOW)
            self.assertTrue(opened.call_args.args[1] & toolchain.os.O_NONBLOCK)
            closed.assert_called_once_with(42)
        stream = io.BytesIO(b"x" * (64 * 1024 + 1))
        with mock.patch.object(toolchain.os, "open", return_value=42), \
                mock.patch.object(toolchain.os, "fstat", return_value=SimpleNamespace(st_mode=stat.S_IFREG)), \
                mock.patch.object(toolchain.os, "fdopen", return_value=stream), mock.patch.object(toolchain.os, "close") as closed:
            with self.assertRaises(toolchain.ToolchainError) as caught:
                unused = dist.metadata
        self.assertEqual(caught.exception.code, "input_too_large")
        closed.assert_called_once_with(42)

    def test_selected_wheel_metadata_discovery_refuses_ambiguity_and_non_directories(self):
        collector = toolchain.MetadataCollector({"PyYAML":"6.0.3"})
        with mock.patch.object(toolchain.sys, "path", [str(ROOT), str(ROOT / "docs")]), \
                mock.patch.object(Path, "stat", return_value=SimpleNamespace(st_mode=stat.S_IFDIR)):
            with self.assertRaises(toolchain.ToolchainError) as caught:
                collector._distribution("PyYAML")
        self.assertEqual(caught.exception.code, "metadata_unavailable")
        with mock.patch.object(toolchain.sys, "path", [str(ROOT)]), \
                mock.patch.object(Path, "stat", return_value=SimpleNamespace(st_mode=stat.S_IFLNK)):
            with self.assertRaises(toolchain.ToolchainError):
                collector._distribution("PyYAML")

    def test_default_discovery_to_parser_to_cli_positive_and_wrong_entrypoint(self):
        original_stat = Path.stat
        original_open, original_fstat, original_fdopen, original_close = toolchain.os.open, toolchain.os.fstat, toolchain.os.fdopen, toolchain.os.close
        files = {
            "pyyaml-6.0.3.dist-info/METADATA": b"Metadata-Version: 2.1\nName: PyYAML\nVersion: 6.0.3\n",
            "django_core_micha-3.17.0.dist-info/METADATA": b"Metadata-Version: 2.1\nName: django-core-micha\nVersion: 3.17.0\n",
            "django_core_micha-3.17.0.dist-info/entry_points.txt": b"[console_scripts]\ngenerate-env = django_core_micha.scripts.generate_env:main\n",
        }
        handles = {}
        visited = []
        def selected_stat(path, *args, **kwargs):
            if path.name.endswith(".dist-info"):
                if path.name not in {"pyyaml-6.0.3.dist-info", "django_core_micha-3.17.0.dist-info"}:
                    raise FileNotFoundError()
                return SimpleNamespace(st_mode=stat.S_IFDIR)
            return original_stat(path, *args, **kwargs)
        def bounded_open(path, flags):
            self.assertNotIn(path.name, (".env", "secrets.yaml"))
            self.assertFalse(flags & (toolchain.os.O_WRONLY | toolchain.os.O_RDWR | toolchain.os.O_CREAT | toolchain.os.O_TRUNC))
            if not path.parent.name.endswith(".dist-info"):
                return original_open(path, flags)
            key = path.parent.name + "/" + path.name
            self.assertIn(key, files)
            self.assertFalse(flags & (toolchain.os.O_WRONLY | toolchain.os.O_RDWR | toolchain.os.O_CREAT))
            descriptor = 100 + len(handles)
            handles[descriptor] = files[key]
            visited.append(key)
            return descriptor
        def bounded_fdopen(descriptor, mode, closefd):
            self.assertEqual(mode, "rb")
            if descriptor not in handles:
                return original_fdopen(descriptor, mode, closefd=closefd)
            self.assertEqual(mode, "rb")
            self.assertFalse(closefd)
            return io.BytesIO(handles[descriptor])
        for profile, expected in (("controller","ok"), ("target-parser","ok"), ("controller","entrypoint_mismatch")):
            if expected != "ok":
                files["django_core_micha-3.17.0.dist-info/entry_points.txt"] = b"[console_scripts]\ngenerate-env = hostile.module:run\n"
            visited.clear()
            output = io.StringIO()
            with mock.patch.object(toolchain.sys, "path", [str(ROOT)]), \
                    mock.patch.object(subprocess, "Popen", side_effect=AssertionError("process effect")), \
                    mock.patch.object(socket, "socket", side_effect=AssertionError("network effect")), \
                    mock.patch.object(toolchain.os, "system", side_effect=AssertionError("shell effect")), \
                    mock.patch.object(toolchain.importlib.metadata.EntryPoint, "load", side_effect=AssertionError("entrypoint execution")), \
                    mock.patch.object(Path, "write_text", side_effect=AssertionError("text write")), \
                    mock.patch.object(Path, "write_bytes", side_effect=AssertionError("byte write")), \
                    mock.patch.object(Path, "mkdir", side_effect=AssertionError("directory write")), \
                    mock.patch.object(builtins, "open", side_effect=AssertionError("unbounded file open")), \
                    mock.patch.object(Path, "stat", selected_stat), \
                    mock.patch.object(toolchain.os, "open", side_effect=bounded_open), \
                    mock.patch.object(toolchain.os, "fstat", side_effect=lambda fd: SimpleNamespace(st_mode=stat.S_IFREG) if fd in handles else original_fstat(fd)), \
                    mock.patch.object(toolchain.os, "fdopen", side_effect=bounded_fdopen), \
                    mock.patch.object(toolchain.os, "close", side_effect=lambda fd: None if fd in handles else original_close(fd)) as closed, \
                    mock.patch.object(toolchain, "_runtime_values", return_value=self.runtime), contextlib.redirect_stdout(output):
                status = toolchain._main(["--profile", profile])
            self.assertEqual(status, 0 if expected == "ok" else 1)
            self.assertEqual(json.loads(output.getvalue())["code"], expected)
            self.assertEqual(sum(call.args[0] in handles for call in closed.call_args_list), len(visited))
            self.assertIn("pyyaml-6.0.3.dist-info/METADATA", visited)
            if profile == "controller":
                self.assertIn("django_core_micha-3.17.0.dist-info/entry_points.txt", visited)
            else:
                self.assertEqual(visited, ["pyyaml-6.0.3.dist-info/METADATA"])
    def test_git_crlf_allowed_but_lone_carriage_return_and_hash_drift_refuse(self):
        original = toolchain.render_requirements(self.contract, "target-parser")
        result = toolchain.check_runtime(self.contract, "target-parser", original.replace(b"\n", b"\r\n"),
            collector=FakeCollector(), runtime=self.runtime)
        self.assertTrue(result.ok)
        for data in (original + b"\r", original.replace(b"sha256:", b"sha256:1")):
            with self.assertRaises(toolchain.ToolchainError) as caught:
                toolchain.check_runtime(self.contract, "target-parser", data, collector=FakeCollector(), runtime=self.runtime)
            self.assertEqual(caught.exception.code, "requirements_drift")

    def test_runtime_observations_drive_default_acceptance_and_each_platform_refusal(self):
        observations = {"implementation":"CPython", "system":"Linux", "machine":"x86_64", "minor":12}
        for field, alternative in ((None,None), ("implementation","PyPy"), ("system","FreeBSD"), ("machine","aarch64"), ("minor",13)):
            values = dict(observations)
            if field:
                values[field] = alternative
            collector = FakeCollector()
            with mock.patch.object(toolchain.platform, "python_implementation", return_value=values["implementation"]), \
                    mock.patch.object(toolchain.platform, "system", return_value=values["system"]), \
                    mock.patch.object(toolchain.platform, "machine", return_value=values["machine"]), \
                    mock.patch.object(toolchain.sys, "version_info", SimpleNamespace(major=3,minor=values["minor"])):
                self.assertEqual(toolchain._runtime_values(), (values["implementation"],f"3.{values['minor']}",values["system"],values["machine"]))
                result = toolchain.check_runtime(self.contract, "target-parser",
                    toolchain.render_requirements(self.contract,"target-parser"), collector=collector)
            self.assertEqual(result.ok, field is None)
            if field:
                self.assertEqual(result.code, "runtime_unsupported")
                self.assertEqual(collector.version_calls, [])

    def test_parser_literal_reassignment_is_not_accepted_as_coherent(self):
        for source, success in ((b'COMPOSE_PARSER_VERSION = "6.0.3"\n', True),
                (b'COMPOSE_PARSER_VERSION = "6.0.3"\nCOMPOSE_PARSER_VERSION = "6.0.2"\n', False),
                (b'COMPOSE_PARSER_VERSION = "6.0.3"\nCOMPOSE_PARSER_VERSION += ".1"\n', False),
                (b'COMPOSE_PARSER_VERSION = compute()\n', False)):
            with mock.patch.object(toolchain,"_read_bounded",return_value=source):
                if success:
                    self.assertEqual(toolchain._source_parser_version(), "6.0.3")
                else:
                    with self.assertRaises(toolchain.ToolchainError) as caught:
                        toolchain._source_parser_version()
                    self.assertEqual(caught.exception.code,"parser_pin_unavailable")

    def test_nonregular_bounded_input_refuses_before_read_and_closes_descriptor(self):
        with mock.patch.object(toolchain.os, "open", return_value=42), \
                mock.patch.object(toolchain.os, "fstat", return_value=SimpleNamespace(st_mode=stat.S_IFIFO)), \
                mock.patch.object(toolchain.os, "fdopen", side_effect=AssertionError("must not read FIFO")), \
                mock.patch.object(toolchain.os, "close") as closed:
            with self.assertRaises(toolchain.ToolchainError) as caught:
                toolchain._read_bounded(Path("not-a-regular-file"), 4)
        self.assertEqual(caught.exception.code, "input_not_regular")
        closed.assert_called_once_with(42)


if __name__ == "__main__":
    unittest.main(verbosity=2)
