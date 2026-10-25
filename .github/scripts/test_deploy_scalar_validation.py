"""Exercise the actual legacy deploy-app validation/configuration YAML blocks."""

from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
ACTION_PATH = ROOT / ".github/actions/deploy-app/action.yml"
ACTION = ACTION_PATH.read_text(encoding="utf-8")
STEPS = yaml.safe_load(ACTION)["runs"]["steps"]
VALIDATE = next(step["run"] for step in STEPS if "Validate caller-input strings" in step.get("name", ""))
CONFIGURE = next(step["run"] for step in STEPS if step.get("id") == "config")
BASH = shutil.which("bash")


def _private_bin(root: Path) -> Path:
    private_bin = root / "bin"
    private_bin.mkdir()
    wrapper = private_bin / "python3"
    wrapper.write_text(f"#!{BASH}\nexec {shlex.quote(sys.executable)} -I \"$@\"\n", encoding="utf-8")
    wrapper.chmod(0o700)
    return private_bin


class DeployScalarValidationTests(unittest.TestCase):
    def setUp(self):
        if not BASH:
            self.skipTest("bash is required for actual action run-block tests")
        self.tmp = Path(tempfile.mkdtemp(prefix="deploy-scalars-"))
        self.bin = _private_bin(self.tmp)
        self.sentinel = self.tmp / "injected-command-ran"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _run(self, block: str, env: dict[str, str], cwd: Path | None = None):
        child_env = {"PATH": str(self.bin), "LC_ALL": "C"}
        child_env.update(env)
        return subprocess.run(
            [BASH, "-c", block],
            cwd=cwd or self.tmp,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=False,
        )

    def _valid_input_env(self, **overrides):
        env = {
            "INPUT_ENV": "staging",
            "INPUT_SUBDIR": ".ssh/app",
            "INPUT_F1_NAME": "deploy_key.pem",
            "INPUT_F1_MODE": "0600",
            "INPUT_F2_NAME": "",
            "INPUT_F2_MODE": "600",
            "INPUT_SSH_USER": "deploy_2",
            "INPUT_SSH_HOST": "",
        }
        env.update(overrides)
        return env

    def test_valid_legacy_values_pass_and_host_override_is_a_complete_scalar(self):
        for host in ("", "staging.example.net", "192.0.2.14", "2001:db8::1"):
            with self.subTest(host=host):
                result = self._run(VALIDATE, self._valid_input_env(INPUT_SSH_HOST=host))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_caller_validation_does_not_require_python_before_setup(self):
        (self.bin / "python3").unlink()
        for host in ("override.example", "192.0.2.14", "2001:db8::1", "::1"):
            result = self._run(VALIDATE, self._valid_input_env(INPUT_SSH_HOST=host))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_configuration_rejects_lexically_safe_invalid_ipv6_before_output(self):
        for host in ("a:b", "1:2:3:4:5:6:7:8:9", "2001:::1"):
            result, output = self._configuration("project_name: hram\n", host=host)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(output.read_bytes(), b"before=unchanged\n")
            self.assertEqual(result.stderr.strip(), "deployment_configuration_invalid")

    def test_each_caller_field_rejects_multiline_suffixes_and_shell_syntax(self):
        cases = (
            ("INPUT_ENV", "staging\nBAD=1"),
            ("INPUT_ENV", "staging\n$(touch " + str(self.sentinel) + ")"),
            ("INPUT_SSH_USER", "deploy\nBAD=1"),
            ("INPUT_SSH_USER", "deploy;touch"),
            ("INPUT_SSH_HOST", "host.example\nBAD=1"),
            ("INPUT_SSH_HOST", "deploy@host.example"),
            ("INPUT_SSH_HOST", "host.example;touch"),
            ("INPUT_SSH_HOST", "fe80::1%eth0"),
            ("INPUT_SSH_HOST", "[2001:db8::1]"),
            ("INPUT_SUBDIR", ".ssh/app\nBAD=1"),
            ("INPUT_SUBDIR", ".ssh/../app"),
            ("INPUT_SUBDIR", ".ssh//app"),
            ("INPUT_SUBDIR", "-option"),
            ("INPUT_SUBDIR", "safe;touch"),
            ("INPUT_F1_NAME", "key.pem\nBAD=1"),
            ("INPUT_F1_NAME", "key;touch"),
            ("INPUT_F1_NAME", "-option"),
            ("INPUT_F2_NAME", "../key"),
            ("INPUT_F1_MODE", "600\nBAD=1"),
            ("INPUT_F2_MODE", "0899"),
        )
        for key, value in cases:
            with self.subTest(field=key, value=value):
                result = self._run(VALIDATE, self._valid_input_env(**{key: value}))
                output = result.stdout + result.stderr
                self.assertNotEqual(result.returncode, 0, output)
                self.assertNotIn(value, output)
                self.assertNotIn("BAD=1", output)
                self.assertFalse(self.sentinel.exists())

    def _configuration(self, project_yaml: str, *, target="staging", host="", suffix="", seed=b"before=unchanged\n"):
        self.tmp.mkdir(exist_ok=True)
        (self.tmp / "project.yaml").write_text(project_yaml, encoding="utf-8", newline="\n")
        env_file = self.tmp / "github-env.txt"
        env_file.write_bytes(seed)
        result = self._run(
            CONFIGURE,
            {
                "TARGET_ENV": target,
                "SSH_HOST_INPUT": host,
                "TAILNET_SUFFIX": suffix,
                "GITHUB_ENV": str(env_file),
            },
        )
        return result, env_file

    def test_every_caller_field_rejects_terminal_newline_cr_and_output_syntax(self):
        valid = self._valid_input_env(INPUT_SSH_HOST="valid.example", INPUT_F2_NAME="second.pem")
        for key, original in valid.items():
            for suffix in ("\n", "\r", "\nINJECTED_SCALAR_SENTINEL=1"):
                with self.subTest(field=key, suffix=repr(suffix)):
                    result = self._run(VALIDATE, {**valid, key: original + suffix})
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertNotIn("INJECTED_SCALAR_SENTINEL", result.stdout + result.stderr)
                    self.assertFalse(self.sentinel.exists())

    def test_parsed_multiline_scalars_are_refused_without_output(self):
        # Dump genuine string values: refusals must reach scalar validation,
        # rather than accidentally testing a malformed YAML document.
        base = {"project_name": "hram", "infra": {"persistent_dirs": ["uploads"]},
                "environments": {"staging": {"server": "staging"}}}
        for value in ("valid\nINJECTED_SCALAR_SENTINEL=1", "valid\n", "valid\r", "$(touch injected-command-ran)", "-option"):
            for field in ("project", "server", "directory", "target", "host", "suffix"):
                with self.subTest(field=field, value=repr(value)):
                    config = yaml.safe_load(yaml.safe_dump(base))
                    settings = {}
                    if field == "project": config["project_name"] = value
                    elif field == "server": config["environments"]["staging"]["server"] = value
                    elif field == "directory": config["infra"]["persistent_dirs"] = ["uploads", value]
                    else: settings[field] = value
                    result, output = self._configuration(yaml.safe_dump(config), **settings)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(output.read_bytes(), b"before=unchanged\n")
                    self.assertNotIn("INJECTED_SCALAR_SENTINEL", result.stdout + result.stderr)
                    self.assertNotIn("Traceback", result.stdout + result.stderr)
                    self.assertFalse(self.sentinel.exists())

    def test_project_boundaries_and_empty_optional_configuration_positive(self):
        for project in ("a", "a" * 63, "survey_contact_app", "bigler-consult"):
            result, output = self._configuration(yaml.safe_dump({"project_name": project}), host="override.example")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(f"PROJECT_NAME={project}\n".encode(), output.read_bytes())
            self.assertIn(f"REMOTE_PATH=/srv/apps/{project}_stage\n".encode(), output.read_bytes())
            self.assertIn(b"PERSISTENT_DIRS=\n", output.read_bytes())
        result, output = self._configuration(yaml.safe_dump({"project_name": "a" * 64}), host="override.example")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output.read_bytes(), b"before=unchanged\n")

    def test_staging_configuration_keeps_output_order_and_valid_derived_paths(self):
        yaml_text = (
            "project_name: survey_app\n"
            "infra:\n  persistent_dirs: [.cache, uploads/cache]\n"
            "environments:\n  staging:\n    server: staging\n"
        )
        result, env_file = self._configuration(yaml_text)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            env_file.read_bytes(),
            b"before=unchanged\nTARGET_ENV=staging\nREMOTE_PATH=/srv/apps/survey_app_stage\n"
            b"PROJECT_NAME=survey_app\nRESOLVED_SSH_HOST=staging.tail990d7f.ts.net\n"
            b"PERSISTENT_DIRS=.cache uploads/cache\n",
        )

    def test_production_and_explicit_hostname_or_ip_override_are_preserved(self):
        yaml_text = (
            "project_name: hpc-bridge\n"
            "infra: {}\n"
            "environments:\n  production:\n    server: main-prod\n"
        )
        result, env_file = self._configuration(yaml_text, target="production", suffix="tenant.example")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(b"REMOTE_PATH=/srv/apps/hpc-bridge\n", env_file.read_bytes())
        self.assertIn(b"RESOLVED_SSH_HOST=main-prod.tenant.example\n", env_file.read_bytes())

        for host in ("override.example.net", "192.0.2.15", "2001:db8::2"):
            with self.subTest(host=host):
                result, env_file = self._configuration(yaml_text, target="production", host=host)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(f"RESOLVED_SSH_HOST={host}\n".encode(), env_file.read_bytes())

    def test_empty_suffix_uses_default_but_whitespace_and_bad_suffix_refuse(self):
        yaml_text = "project_name: hram\nenvironments:\n  staging:\n    server: staging\n"
        result, env_file = self._configuration(yaml_text, suffix="")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(b"RESOLVED_SSH_HOST=staging.tail990d7f.ts.net\n", env_file.read_bytes())
        for suffix in ("   ", "tenant.example\nBAD=1", "bad..example", "-bad.example", "bad;host"):
            with self.subTest(suffix=suffix):
                result, env_file = self._configuration(yaml_text, suffix=suffix)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(env_file.read_bytes(), b"before=unchanged\n")
                self.assertNotIn("BAD=1", result.stdout + result.stderr)

    def test_invalid_yaml_scalars_mappings_hosts_and_paths_write_nothing(self):
        invalid_documents = (
            "project_name: hram\nproject_name: survey_app\n",
            "project_name: hram\ninfra: []\n",
            "project_name: hram\ninfra:\n  persistent_dirs: uploads\n",
            "project_name: hram\ninfra:\n  persistent_dirs: [uploads, 2]\n",
            "project_name: hram\ninfra:\n  persistent_dirs: [uploads/../secrets]\n",
            "project_name: hram\ninfra:\n  persistent_dirs: [.cache, -option]\n",
            "project_name: hram\nenvironments: []\n",
            "project_name: hram\nenvironments:\n  staging: []\n",
            "project_name: hram\nenvironments:\n  staging:\n    server: 'staging\nBAD=1'\n",
            "project_name: hram\nenvironments:\n  staging:\n    server: 'bad..host'\n",
            "project_name: hram\nenvironments:\n  staging:\n    server: 4\n",
            "project_name: 'hram\nBAD=1'\n",
            "project_name: true\n",
            "project_name: ../hram\n",
            "project_name: Hram\n",
            "project_name: hram\ninfra:\n  persistent_dirs: [valid, 'bad;touch']\n",
            "project_name: [malformed\n",
        )
        for document in invalid_documents:
            with self.subTest(document=document):
                result, env_file = self._configuration(document)
                output = result.stdout + result.stderr
                self.assertNotEqual(result.returncode, 0, output)
                self.assertEqual(env_file.read_bytes(), b"before=unchanged\n")
                self.assertNotIn("BAD=1", output)
                self.assertNotIn("touch", output)
                self.assertFalse(self.sentinel.exists())

    def test_explicit_host_does_not_skip_suffix_validation_or_config_shape_checks(self):
        yaml_text = "project_name: hram\nenvironments:\n  staging:\n    server: staging\n"
        for suffix in ("bad..suffix", "suffix\nBAD=1"):
            result, env_file = self._configuration(yaml_text, host="valid.example", suffix=suffix)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(env_file.read_bytes(), b"before=unchanged\n")
        result, env_file = self._configuration(
            "project_name: hram\nenvironments: []\n", host="valid.example"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(env_file.read_bytes(), b"before=unchanged\n")

    def test_validation_and_configuration_precede_output_and_transfer_steps(self):
        step_names = [step.get("name", "") for step in STEPS]
        validation_index = next(i for i, name in enumerate(step_names) if "Validate caller-input strings" in name)
        config_index = next(i for i, step in enumerate(STEPS) if step.get("id") == "config")
        generation_index = next(i for i, name in enumerate(step_names) if "Generate .env file" in name)
        rsync_index = next(i for i, name in enumerate(step_names) if "Rsync project to server" in name)
        self.assertLess(validation_index, config_index)
        self.assertLess(config_index, generation_index)
        self.assertLess(config_index, rsync_index)

    def test_named_file_requires_mode_but_unnamed_optional_empty_mode_passes(self):
        for field in ("1", "2"):
            with self.subTest(field=field):
                env = self._valid_input_env(**{f"INPUT_F{field}_NAME": "key.pem", f"INPUT_F{field}_MODE": ""})
                result = self._run(VALIDATE, env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"Invalid INPUT_F{field}_MODE", result.stderr)
        result = self._run(VALIDATE, self._valid_input_env(INPUT_F1_NAME="", INPUT_F1_MODE="", INPUT_F2_MODE=""))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_duplicate_yaml_keys_and_declared_null_environment_refuse_with_valid_override(self):
        for document in (
            "project_name: hram\nproject_name: survey_app\n",
            "project_name: hram\nenvironments: null\n",
            "project_name: hram\nenvironments: {staging: null}\n",
            "project_name: hram\nenvironments: {staging: {server: first, server: second}}\n",
            "project_name: hram\ninfra: {persistent_dirs: [first], persistent_dirs: [second]}\n",
            "project_name: hram\ninfra: {<<: {persistent_dirs: [first], persistent_dirs: [second]}}\n",
        ):
            with self.subTest(document=document):
                result, output = self._configuration(document, host="valid.example")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(output.read_bytes(), b"before=unchanged\n")
                self.assertEqual(result.stderr.strip(), "deployment_configuration_invalid")

    def test_yaml_anchor_merges_and_explicit_overrides_keep_effective_values(self):
        document = (
            "project_name: hram\n"
            "defaults: &defaults {server: original}\n"
            "environments:\n"
            "  staging: {<<: *defaults, server: selected}\n"
            "  production: {<<: *defaults, server: production}\n"
        )
        result, output = self._configuration(document)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(b"RESOLVED_SSH_HOST=selected.tail990d7f.ts.net\n", output.read_bytes())


if __name__ == "__main__":
    unittest.main()
