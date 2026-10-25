"""Exercise the three deploy-app connection blocks with private CLI stubs."""

from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
ACTION_PATH = ROOT / ".github/actions/deploy-app/action.yml"
ACTION = ACTION_PATH.read_text(encoding="utf-8")
STEPS = yaml.safe_load(ACTION)["runs"]["steps"]
BASH = shutil.which("bash")


PROBE = next(step["run"] for step in STEPS if step.get("id") == "ts_probe")
SUMMARY = next(step["run"] for step in STEPS if "Tailnet diagnostics (read-only)" in step["name"])
PREFLIGHT = next(step["run"] for step in STEPS if "Pre-flight Tailscale-SSH connectivity check" in step["name"])


JQ_STUB = r'''#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
filter_text = args[-1]
data = json.load(sys.stdin)
if filter_text == '.BackendState // "unknown"':
    print(data.get("BackendState") or "unknown")
elif filter_text == '((.Self.Tags // []) | index($t)) != null':
    tag = args[args.index("--arg") + 2]
    print("true" if tag in ((data.get("Self") or {}).get("Tags") or []) else "false")
else:
    sys.exit(9)
'''


TIMEOUT_STUB = r'''#!/usr/bin/env bash
printf '%s\n' "$*" >> "$COMMAND_LOG"
args=("$@")
[[ "${args[0]}" == --signal=TERM && "${args[1]}" == --kill-after=5s ]] || exit 97
while [[ "${args[0]}" == --* ]]; do args=("${args[@]:1}"); done
requested="${args[0]}"
command=("${args[@]:1}")
case "$requested" in
  25s) duration=0.2s ;;
  *) exit 98 ;;
esac
exec "$REAL_TIMEOUT" --signal=TERM --kill-after=0.1s "$duration" "${command[@]}"
'''


TAILSCALE_STUB = r'''#!/usr/bin/env bash
printf 'tailscale %s\n' "$*" >> "$COMMAND_LOG"
case "${TS_MODE}:${1:-}:${2:-}" in
  status:status:--json)
    if [[ "${TS_SLEEP:-}" == yes ]]; then sleep 3; fi
    if [[ "${VIA_SUDO:-0}" == 1 ]]; then
      printf '%s' "${TS_SUDO_STDOUT:-$TS_STDOUT}"
    else
      printf '%s' "${TS_DIRECT_STDOUT:-$TS_STDOUT}"
    fi
    printf '%s' "$TS_STDERR" >&2
    if [[ "${VIA_SUDO:-0}" == 1 ]]; then exit "${TS_SUDO_EXIT:-${TS_EXIT:-0}}"; fi
    exit "${TS_DIRECT_EXIT:-${TS_EXIT:-0}}" ;;
  ping:ping:--c)
    printf '%s' "$PING_STDOUT"
    printf '%s' "$PING_STDERR" >&2
    if [[ "${VIA_SUDO:-0}" == 1 ]]; then exit "${PING_SUDO_EXIT:-${PING_EXIT:-0}}"; fi
    exit "${PING_DIRECT_EXIT:-${PING_EXIT:-0}}" ;;
esac
printf '%s' "$TS_STDOUT"
printf '%s' "$TS_STDERR" >&2
exit "${TS_EXIT:-1}"
'''


SUDO_STUB = r'''#!/usr/bin/env bash
printf 'sudo %s\n' "$*" >> "$COMMAND_LOG"
[[ "${1:-}" == -n ]] || exit 90
shift
[[ "${1:-}" == tailscale ]] || exit 91
[[ "${2:-}" == status || "${2:-}" == ping ]] || exit 92
VIA_SUDO=1 exec "$@"
'''


SSH_STUB = r'''#!/usr/bin/env bash
printf 'ssh %s\n' "$*" >> "$COMMAND_LOG"
printf '%s' "$SSH_STDOUT"
printf '%s' "$SSH_STDERR" >&2
if [[ "${SSH_SLEEP:-}" == yes ]]; then sleep 3; fi
exit "${SSH_EXIT:-0}"
'''


class ConnectionDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        if not BASH:
            self.skipTest("bash is required for action run-block tests")
        self.tmp = Path(tempfile.mkdtemp(prefix="deploy-diagnostics-"))
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        # Only explicitly selected harmless runtime tools can leave this PATH.
        for name, target in (("bash", BASH), ("python3", sys.executable),
                             ("head", "/usr/bin/head"), ("sleep", "/usr/bin/sleep")):
            (self.bin / name).symlink_to(target)
        self.log = self.tmp / "commands.txt"
        self.output = self.tmp / "github-output.txt"
        self._write_executable("timeout", TIMEOUT_STUB)
        self._write_executable("tailscale", TAILSCALE_STUB)
        self._write_executable("sudo", SUDO_STUB)
        self._write_executable("ssh", SSH_STUB)
        self._write_executable("jq", JQ_STUB)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _write_executable(self, name: str, content: str):
        path = self.bin / name
        path.write_text(content, encoding="utf-8", newline="\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def _run(self, script: str, **settings):
        self.output.write_text("", encoding="utf-8")
        script_path = self.tmp / "block.sh"
        script_path.write_text(script, encoding="utf-8", newline="\n")
        env = {
            "PATH": str(self.bin),
            "COMMAND_LOG": str(self.log),
            "GITHUB_OUTPUT": str(self.output),
            "REAL_TIMEOUT": "/usr/bin/timeout",
            "TS_TAG": "tag:diagnostic-test",
            "TAILNET_HOST": "synthetic-target",
            "SSH_USER": "synthetic-user",
            "TS_MODE": "ping" if script == PREFLIGHT else "status",
            "TS_STDOUT": '{"BackendState":"Running","Self":{"Tags":["tag:diagnostic-test"]}}\n',
            "TS_STDERR": "peer=100.64.1.2 credential=sentinel::add-mask::\n",
            "TS_EXIT": "0",
            "PING_EXIT": "0",
            "PING_STDOUT": "peer=100.64.1.2\n",
            "PING_STDERR": "credential=sentinel\n",
            "SSH_EXIT": "0",
            "SSH_STDOUT": "host=synthetic-target\n",
            "SSH_STDERR": "credential=sentinel\n",
        }
        env.update({key: str(value) for key, value in settings.items()})
        return subprocess.run(
            [BASH, str(script_path)],
            cwd=self.tmp,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
        )

    def _logs(self, result):
        output = result.stdout + result.stderr
        self.assertNotIn("100.64.1.2", output)
        self.assertNotIn("sentinel", output)
        self.assertNotIn("synthetic-target", output)
        self.assertNotIn("synthetic-user", output)
        self.assertNotIn("::add-mask::", output)
        return output

    def test_probe_reuses_only_running_matching_tag_and_keeps_output_contract(self):
        result = self._run(PROBE)
        output = self._logs(result)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "connected=true\n")
        self.assertEqual(output.strip(), "phase=runner_connection result=reused")
        log = self.log.read_text(encoding="utf-8")
        self.assertIn("--signal=TERM --kill-after=5s 25s tailscale status --json", log)
        self.assertIn("if: steps.ts_probe.outputs.connected != 'true'", ACTION)

    def test_probe_direct_failure_uses_scoped_sudo_read_fallback(self):
        result = self._run(PROBE, TS_DIRECT_EXIT="1")
        self._logs(result)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "connected=true\n")
        self.assertIn("sudo -n tailscale status --json", self.log.read_text(encoding="utf-8"))

    def test_probe_both_status_paths_fail_joins_without_reflecting_provider_output(self):
        result = self._run(
            PROBE,
            TS_DIRECT_EXIT="1",
            TS_SUDO_EXIT="1",
            TS_STDOUT="state=credential-sentinel\n",
            TS_STDERR="peer=100.64.1.2\n",
        )
        output = self._logs(result)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "connected=false\n")
        self.assertEqual(output.strip(), "phase=runner_connection result=join_required")

    def test_probe_malformed_nonrunning_and_missing_tag_fail_closed(self):
        cases = (
            ('{"BackendState":"Running","Self":{"Tags":["other"]}}', "false"),
            ('{"BackendState":"Stopped","Self":{"Tags":["tag:diagnostic-test"]}}', "false"),
            ("not-json", "false"),
        )
        for payload, expected in cases:
            with self.subTest(payload=payload):
                result = self._run(PROBE, TS_STDOUT=payload)
                self._logs(result)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(self.output.read_text(encoding="utf-8"), f"connected={expected}\n")

    def test_probe_missing_cli_cannot_escape_private_path(self):
        (self.bin / "tailscale").unlink()
        result = self._run(PROBE)
        self._logs(result)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "connected=false\n")
        self.assertFalse(self.log.exists())

    def test_failed_status_cannot_authorize_reuse_even_with_valid_json(self):
        result = self._run(PROBE, TS_EXIT="1")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "connected=false\n")
        self.assertEqual(self._logs(result).strip(), "phase=runner_connection result=join_required")

    def test_status_size_and_injected_values_are_private(self):
        for payload in (
            '{"BackendState":"Running","Self":{"Tags":["tag:diagnostic-test"]},"padding":"' + "x" * 65536 + '"}',
            '{"BackendState":"::add-mask::credential-sentinel","Self":{"Tags":["tag:diagnostic-test"]}}',
        ):
            with self.subTest(size=len(payload)):
                result = self._run(PROBE, TS_STDOUT=payload)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(self.output.read_text(encoding="utf-8"), "connected=false\n")
                self.assertEqual(self._logs(result).strip(), "phase=runner_connection result=join_required")

    def test_status_byte_cap_preserves_trailing_whitespace_before_deciding(self):
        valid = '{"BackendState":"Running","Self":{"Tags":["tag:diagnostic-test"]}}'
        for size, expected in ((65536, "true"), (65537, "false"), (70000, "false")):
            with self.subTest(bytes=size):
                result = self._run(PROBE, TS_STDOUT=valid + "\n" * (size - len(valid)))
                self.assertEqual(result.returncode, 0)
                self.assertEqual(self.output.read_text(encoding="utf-8"), f"connected={expected}\n")
                self._logs(result)
        payload = valid[:-1] + ',"padding":"' + "\u00e9" * 33000 + '"}'
        result = self._run(PROBE, TS_STDOUT=payload)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "connected=false\n")
        self._logs(result)

    def test_oversized_failed_direct_read_cannot_use_valid_sudo_fallback(self):
        valid = '{"BackendState":"Running","Self":{"Tags":["tag:diagnostic-test"]}}'
        result = self._run(PROBE, TS_DIRECT_STDOUT=valid + "\n" * 70000, TS_DIRECT_EXIT="1")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "connected=false\n")
        self.assertNotIn("sudo", self.log.read_text(encoding="utf-8"))
        self.assertEqual(self._logs(result).strip(), "phase=runner_connection result=join_required")

    def test_target_and_tag_injection_cannot_become_log_commands(self):
        tag = "::add-mask::credential-sentinel"
        result = self._run(PROBE, TS_TAG=tag, TS_STDOUT='{"BackendState":"Running","Self":{"Tags":["' + tag + '"]}}')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "connected=true\n")
        self._logs(result)
        result = self._run(PREFLIGHT, TAILNET_HOST=tag, SSH_USER=tag)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._logs(result).splitlines(), ["phase=target_datapath result=observed", "phase=target_ssh result=success"])

    def test_probe_timeout_is_bounded_and_quiet(self):
        result = self._run(PROBE, TS_SLEEP="yes")
        output = self._logs(result)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "connected=false\n")
        self.assertIn("phase=runner_connection result=join_required", output)

    def test_summary_executes_no_topology_commands_and_emits_only_fixed_code(self):
        result = self._run(SUMMARY)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._logs(result).strip(), "phase=runner_diagnostics result=bounded_summary")
        self.assertFalse(self.log.exists())
        self.assertNotIn("tailscale status\n", SUMMARY)
        self.assertNotIn("tailscale netcheck", SUMMARY)

    def test_preflight_ping_success_and_sudo_fallback_keep_ssh_authoritative(self):
        direct = self._run(PREFLIGHT)
        self.assertEqual(direct.returncode, 0)
        self.assertEqual(
            self._logs(direct).splitlines(),
            ["phase=target_datapath result=observed", "phase=target_ssh result=success"],
        )
        self.assertIn("--signal=TERM --kill-after=5s 25s ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes", self.log.read_text(encoding="utf-8"))

        self.log.unlink()
        fallback = self._run(PREFLIGHT, PING_DIRECT_EXIT="1", PING_SUDO_EXIT="0")
        self.assertEqual(fallback.returncode, 0)
        log = self.log.read_text(encoding="utf-8")
        self.assertIn("sudo -n tailscale ping --c 3 synthetic-target", log)

    def test_preflight_ping_failure_is_nonfatal_but_ssh_failure_or_timeout_fails(self):
        ping_failure = self._run(PREFLIGHT, PING_EXIT="1", SSH_EXIT="0")
        self.assertEqual(ping_failure.returncode, 0)
        self.assertEqual(
            self._logs(ping_failure).splitlines(),
            ["phase=target_datapath result=failed", "phase=target_ssh result=success"],
        )

        self.log.unlink()
        ssh_failure = self._run(PREFLIGHT, SSH_EXIT="1")
        self.assertNotEqual(ssh_failure.returncode, 0)
        self.assertEqual(self._logs(ssh_failure).splitlines(), ["phase=target_datapath result=observed", "phase=target_ssh result=failed"])

        self.log.unlink()
        ssh_timeout = self._run(PREFLIGHT, SSH_SLEEP="yes")
        self.assertNotEqual(ssh_timeout.returncode, 0)
        self.assertEqual(self._logs(ssh_timeout).splitlines(), ["phase=target_datapath result=observed", "phase=target_ssh result=failed"])

    def test_each_status_ping_and_ssh_invocation_uses_finite_timeout_and_quiet_outputs(self):
        self._run(PROBE, TS_DIRECT_EXIT="1")
        log = self.log.read_text(encoding="utf-8")
        self.assertIn("--signal=TERM --kill-after=5s 25s tailscale status --json", log)
        self.assertIn("--signal=TERM --kill-after=5s 25s sudo -n tailscale status --json", log)

        self.log.unlink()
        self._run(PREFLIGHT, PING_EXIT="1")
        log = self.log.read_text(encoding="utf-8")
        for line in log.splitlines():
            if line.startswith("--signal="):
                self.assertTrue(line.startswith("--signal=TERM --kill-after=5s 25s "), line)
        for command in ("tailscale ping", "sudo -n tailscale ping", "ssh -o"):
            self.assertIn("--signal=TERM --kill-after=5s 25s " + command, log)
        self.assertNotIn("timeout 25", PREFLIGHT)
        self.assertIn("timeout --signal=TERM --kill-after=5s 25s", PREFLIGHT)


if __name__ == "__main__":
    unittest.main()
