"""Executable security tests for deploy-traefik's host .env secret filter."""

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ACTION_PATH = ROOT / ".github/actions/deploy-traefik/action.yml"
ACTION = ACTION_PATH.read_text(encoding="utf-8")
BASH = shutil.which("bash")
STEP_NAME = "🔐 Generate .env"
DEFAULT_KEYS = {
    "CLAUDE_ROUTINE_ENDPOINT",
    "CLAUDE_ROUTINE_TOKEN",
    "CLOUDFLARE_TUNNEL_TOKEN",
    "DOMAIN_KUMA",
    "DOMAIN_TRAEFIK",
    "TRAEFIK_DASHBOARD_AUTH",
}


def extract_step(action_text=ACTION):
    match = re.search(
        r"(?ms)^    - name: " + re.escape(STEP_NAME) + r"\n(?P<section>.*?)(?=^    - name: |\Z)",
        action_text,
    )
    if not match:
        raise AssertionError(f"step not found: {STEP_NAME}")
    section = match.group("section")
    run = re.search(r"(?ms)^      run: \|\n(?P<body>(?:^        .*\n|^\s*\n)*)", section)
    if not run:
        raise AssertionError(f"run body not found: {STEP_NAME}")
    body = "\n".join(line[8:] if line.startswith("        ") else "" for line in run.group("body").splitlines())
    return body, section


class DeployTraefikAllowlistTests(unittest.TestCase):
    def setUp(self):
        if not BASH:
            self.skipTest("no bash available on PATH")
        self.tmp = Path(tempfile.mkdtemp(prefix="deploy-traefik-allowlist-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin_dir = self.tmp / "bin"
        self.bin_dir.mkdir()
        stub = self.bin_dir / "generate-env"
        stub.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os\n"
            "context = json.loads(os.environ['SECRETS_CONTEXT'])\n"
            "args = __import__('sys').argv[1:]\n"
            "output_path = '.env'\n"
            "for index, value in enumerate(args):\n"
            "    if value == '--output': output_path = args[index + 1]\n"
            "open(output_path, 'w', encoding='utf-8').close()\n"
            "with open(os.environ['KEY_LOG'], 'w', encoding='utf-8') as output:\n"
            "    output.write('\\n'.join(sorted(context)) + ('\\n' if context else ''))\n",
            encoding="utf-8",
            newline="\n",
        )
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        self.body, self.section = extract_step()

    def run_step(self, raw_context, allowlist=None):
        env = os.environ.copy()
        env.update({
            "PATH": str(self.bin_dir) + os.pathsep + env.get("PATH", ""),
            "SECRETS_CONTEXT_RAW": raw_context,
            "SECRETS_ALLOWLIST": allowlist if allowlist is not None else ",".join(sorted(DEFAULT_KEYS)),
            "TARGET_ENVIRONMENT": "production",
            "RUNNER_TEMP": str(self.tmp),
            "GITHUB_ENV": str(self.tmp / "github-env"),
            "KEY_LOG": str(self.tmp / "keys.log"),
        })
        return subprocess.run(
            [BASH, "-c", self.body], cwd=ROOT, env=env,
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )

    def test_only_six_allowed_keys_reach_generate_env_and_values_are_never_logged(self):
        allowed = {key: f"allowed-value-{index}" for index, key in enumerate(sorted(DEFAULT_KEYS))}
        denied_names = [
            "GH_RUNNER_APP_PRIVATE_KEY", "GH_RUNNER_APP_ID", "GH_RUNNER_APP_INSTALLATION_ID",
            "GH_RUNNER_SHA256", "TAILSCALE_MGMT_AUTH_KEY", "TS_OAUTH_SECRET", "B2_APP_KEY",
            "CLOUDFLARE_API_TOKEN", "UNKNOWN_FUTURE_SECRET",
        ]
        denied = {key: f"sensitive-value-{index}" for index, key in enumerate(denied_names)}
        context = {**allowed, **denied}
        proc = self.run_step(json.dumps(context))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(set((self.tmp / "keys.log").read_text(encoding="utf-8").splitlines()), DEFAULT_KEYS)
        for value in [*allowed.values(), *denied.values()]:
            self.assertNotIn(value, proc.stdout + proc.stderr)
        self.assertIn("Allowed secrets forwarded:", proc.stderr)
        self.assertIn("CLOUDFLARE_TUNNEL_TOKEN", proc.stderr)

    def test_custom_allowlist_is_honored(self):
        context = {"KEEP_ME": "private-keep", "DROP_ME": "private-drop"}
        proc = self.run_step(json.dumps(context), "KEEP_ME")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual((self.tmp / "keys.log").read_text(encoding="utf-8").splitlines(), ["KEEP_ME"])
        self.assertNotIn("private-keep", proc.stdout + proc.stderr)
        self.assertNotIn("private-drop", proc.stdout + proc.stderr)

    def test_non_object_json_fails_the_step(self):
        proc = self.run_step(json.dumps(["not", "an", "object"]))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("expected an object", proc.stderr)

    def test_filter_uses_python_and_default_input_is_the_six_keys(self):
        self.assertNotIn("jq", self.body)
        self.assertIn("python3 - <<'PY'", self.body)
        input_match = re.search(r"(?ms)^  secrets_allowlist:\n(?P<block>.*?)(?=^  [A-Za-z0-9_-]+:|\Z)", ACTION)
        self.assertIsNotNone(input_match)
        default_match = re.search(r"(?m)^    default: '([^']*)'", input_match.group("block"))
        self.assertIsNotNone(default_match)
        self.assertEqual(set(default_match.group(1).split(",")), DEFAULT_KEYS)

    def test_the_step_wires_the_input_into_its_environment(self):
        self.assertIn("SECRETS_ALLOWLIST: ${{ inputs.secrets_allowlist }}", ACTION)

    def test_runner_credentials_can_never_be_admitted_by_a_caller_allowlist(self):
        context = {
            "KEEP_ME": "private-keep",
            "GH_RUNNER_APP_PRIVATE_KEY": "private-app-key",
            "GH_RUNNER_APP_ID": "private-app-id",
            "TAILSCALE_MGMT_AUTH_KEY": "private-mgmt-key",
        }
        allowlist = "KEEP_ME,GH_RUNNER_APP_PRIVATE_KEY,GH_RUNNER_APP_ID,TAILSCALE_MGMT_AUTH_KEY"
        proc = self.run_step(json.dumps(context), allowlist)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual((self.tmp / "keys.log").read_text(encoding="utf-8").splitlines(), ["KEEP_ME"])
        self.assertIn("Ignoring protected keys", proc.stderr)
        for secret in ("private-keep", "private-app-key", "private-app-id", "private-mgmt-key"):
            self.assertNotIn(secret, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
