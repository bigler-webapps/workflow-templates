from pathlib import Path
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
BASH = shutil.which("bash")
MESSAGE = "The default tailscaled on this runner reports itself connected, but tailscale0 has no tailnet IPv4 address; restart it on the runner with: sudo systemctl restart tailscaled (WM-INF-98)."
ACTIONS = {
    "deploy-app": ROOT / ".github/actions/deploy-app/action.yml",
    "tailnet-connect": ROOT / ".github/actions/tailnet-connect/action.yml",
}


def _probe(path):
    action = yaml.safe_load(path.read_text(encoding="utf-8"))
    return next(s["run"] for s in action["runs"]["steps"] if s.get("id") == "ts_probe")


class TailnetProbeGuard(unittest.TestCase):
    def setUp(self):
        if not BASH:
            self.skipTest("no bash available on PATH")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()

    def _stub(self, name, content):
        path = self.bin / name
        path.write_text(content, encoding="utf-8", newline="\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def _run(self, action, state="Running", tag="tag:ci-deploy", address="100.109.210.125", mode="good", cli=True):
        self.bin.mkdir(exist_ok=True)
        log = self.root / "calls.log"
        self._stub("jq", r'''#!/usr/bin/env bash
case "$*" in
  *BackendState*) printf '%s\n' "$TS_STATE" ;;
  *Tags*) [ "$TS_TAGGED" = true ] && echo true || echo false ;;
  *TailscaleIPs*) [ -n "$TS_ADDRESS" ] && printf '%s\n' "$TS_ADDRESS" || true ;;
  *) exit 1 ;;
esac
''')
        self._stub("ip", r'''#!/usr/bin/env bash
echo "ip $*" >> "$CALL_LOG"
if [ "$IP_MODE" = good ]; then echo "2: tailscale0 inet $TS_ADDRESS/32 scope global tailscale0"; else echo "2: tailscale0 inet6 fe80::1/64 scope link"; fi
''')
        self._stub("sudo", '#!/usr/bin/env bash\necho "sudo $*" >> "$CALL_LOG"\nshift\nexec "$@"\n')
        self._stub("timeout", '#!/usr/bin/env bash\nshift 3\nexec "$@"\n')
        if cli:
            self._stub("tailscale", '#!/usr/bin/env bash\necho "tailscale $*" >> "$CALL_LOG"\nprintf \'{}\'\n')
        run = _probe(ACTIONS[action])
        env = os.environ.copy()
        env.update({"PATH": str(self.bin) + os.pathsep + env.get("PATH", ""), "GITHUB_OUTPUT": str(self.root / "out"),
                    "CALL_LOG": str(log), "TS_STATE": state, "TS_TAGGED": str(tag == "tag:ci-deploy").lower(),
                    "TS_ADDRESS": address, "IP_MODE": mode, "TS_TAG": "tag:ci-deploy"})
        proc = subprocess.run([BASH, "-c", run], cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
        calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        output = Path(env["GITHUB_OUTPUT"]).read_text(encoding="utf-8") if Path(env["GITHUB_OUTPUT"]).exists() else ""
        return proc, calls, output

    def test_routable_connected_daemon_is_reused(self):
        for action in ACTIONS:
            with self.subTest(action=action):
                proc, _, output = self._run(action)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                if action == "deploy-app":
                    self.assertIn("connected=true", output)
                else:
                    self.assertIn("connected=true", output)
                    self.assertIn("reuse existing tailnet connection: true", proc.stdout)

    def test_link_local_only_fails_with_remedy_without_daemon_mutation(self):
        for action in ACTIONS:
            with self.subTest(action=action):
                proc, calls, _ = self._run(action, mode="linklocal")
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertEqual(proc.stdout.count("::error::" + MESSAGE), 1, proc.stdout)
                self.assertFalse(any(any(word in call for word in ("tailscale up", "tailscale reset", "tailscale logout", "systemctl")) for call in calls), calls)

    def test_disconnected_and_unavailable_states_take_join_required_path(self):
        for action in ACTIONS:
            cases = [("Starting", "tag:ci-deploy", True, "100.109.210.125"), ("Running", "tag:wrong", True, "100.109.210.125"), ("Running", "tag:ci-deploy", False, "100.109.210.125")]
            for state, tag, cli, address in cases:
                with self.subTest(action=action, state=state, tag=tag, cli=cli, address=address):
                    proc, _, output = self._run(action, state=state, tag=tag, cli=cli, address=address)
                    self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                    self.assertIn("connected=false", output)

    def test_running_tagged_daemon_without_any_reported_ipv4_fails_instead_of_joining(self):
        """A Running, correctly tagged daemon whose status reports no tailnet IPv4 is the same dead interface
        as a link-local-only tailscale0: red with the remedy, never a join against the shared daemon."""
        for action in ACTIONS:
            with self.subTest(action=action):
                proc, calls, output = self._run(action, address="")
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertEqual(proc.stdout.count("::error::" + MESSAGE), 1, proc.stdout)
                self.assertNotIn("connected=", output)
                self.assertFalse(any(any(word in call for word in ("tailscale up", "tailscale reset", "tailscale logout", "systemctl")) for call in calls), calls)


if __name__ == "__main__":
    unittest.main()
