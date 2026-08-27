"""Behavioural regression test for tailnet-connect's reuse probe (WFT-CI-26).

Run with: python .github/scripts/test_tailnet_connect_probe.py

The real `ts_probe` step's `run:` script is extracted verbatim from
action.yml and executed under bash with `tailscale`/`jq`/`ip` stubbed --
deliberately NOT reimplementing the probe's own logic (same rationale as
test_deploy_app_action.py's `_extract_cleanup_block`: a test that
re-implements what it checks proves nothing). `jq` and `ip` are unavailable
on this Windows dev box, so both are stubbed too -- the `jq` stub is a small
Python shim that answers exactly the three fixed filter expressions this
script actually uses (not a general jq clone).

This is structural/behavioural, not the WO's required live proof: whether a
runner's warm daemon is ACTUALLY routable is a live-only fact (AGENTS.md
exception (b), two-sided dispatch) -- that evidence is separate from this file.
"""

from pathlib import Path
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
ACTION = (ROOT / ".github/actions/tailnet-connect/action.yml").read_text(encoding="utf-8")

BASH = shutil.which("bash")

JQ_STUB = r"""#!/usr/bin/env python3
import json, re, sys

args = sys.argv[1:]
named_args = {}
i = 0
while i < len(args) and args[i] != args[-1]:
    if args[i] == "-r":
        i += 1
        continue
    if args[i] == "--arg":
        named_args[args[i + 1]] = args[i + 2]
        i += 3
        continue
    break
filt = args[-1]
data = json.load(sys.stdin)

def is_ipv6(s):
    return ":" in s

if filt == '.BackendState // "unknown"':
    print(data.get("BackendState") or "unknown")
elif filt == '((.Self.Tags // []) | index($t)) != null':
    tags = (data.get("Self") or {}).get("Tags") or []
    print("true" if named_args.get("t") in tags else "false")
elif filt == '(.Self.TailscaleIPs // []) | map(select(test(":") | not)) | .[0] // empty':
    ips = (data.get("Self") or {}).get("TailscaleIPs") or []
    v4 = [ip for ip in ips if not is_ipv6(ip)]
    print(v4[0] if v4 else "")
else:
    sys.stderr.write(f"unhandled jq filter in stub: {filt!r}\n")
    sys.exit(1)
"""

IP_STUB_TEMPLATE = r"""#!/usr/bin/env bash
# `ip -4 addr show` -- prints one "inet <addr>/<prefix> ..." line per address
# in $LOCAL_IPV4S (space-separated, no prefix needed by the caller's grep -F).
for addr in $LOCAL_IPV4S; do
  printf '    inet %s/32 scope global tailscale0\n' "$addr"
done
exit 0
"""

TAILSCALE_STUB = r"""#!/usr/bin/env bash
if [ "$1" = "status" ]; then
  cat "$STATUS_JSON_FILE"
  exit 0
fi
exit 1
"""

# ts_probe_2's real script opens with `sleep 90` (the backoff before
# re-probing). Timing is not part of the routability decision logic under
# test, so this stub no-ops it -- otherwise every ts_probe_2 test costs 90s.
SLEEP_STUB = r"""#!/usr/bin/env bash
exit 0
"""


def _extract_probe_script():
    """Pull the real ts_probe step's `run:` block out of action.yml."""
    match = re.search(
        r"^    - name: Probe existing tailnet connection\n"
        r"      id: ts_probe\n"
        r"      shell: bash\n"
        r"      env:\n"
        r"        TS_TAG: .*\n"
        r"      run: \|\n"
        r"(?P<body>(?:^ {8}.*\n|^\n)+)",
        ACTION,
        re.MULTILINE,
    )
    assert match is not None, "ts_probe step not found at the expected shape in action.yml"
    lines = match.group("body").splitlines()
    dedented = "\n".join(line[8:] if line.startswith(" " * 8) else line for line in lines)
    return dedented


def _extract_probe2_script():
    """Pull the real ts_probe_2 step's `run:` block out of action.yml (WFT-CI-27)."""
    match = re.search(
        r"^    - name: Backoff and re-probe after failed join\n"
        r"      id: ts_probe_2\n"
        r"      if: .*\n"
        r"      shell: bash\n"
        r"      env:\n"
        r"        TS_TAG: .*\n"
        r"      run: \|\n"
        r"(?P<body>(?:^ {8}.*\n|^\n)+)",
        ACTION,
        re.MULTILINE,
    )
    assert match is not None, "ts_probe_2 step not found at the expected shape in action.yml"
    lines = match.group("body").splitlines()
    dedented = "\n".join(line[8:] if line.startswith(" " * 8) else line for line in lines)
    return dedented


PROBE_SCRIPT = _extract_probe_script()
PROBE2_SCRIPT = _extract_probe2_script()


def _run_script(script_text, status_json, ts_tag="tag:ci-deploy", local_ipv4s=""):
    """Execute a real extracted probe script under bash with tailscale/jq/ip
    stubbed. Shared by both ts_probe and ts_probe_2 -- they use the identical
    stub contract (STATUS_JSON_FILE, LOCAL_IPV4S, TS_TAG, GITHUB_OUTPUT)."""
    if not BASH:
        raise unittest.SkipTest("no bash available on PATH")
    tmp = Path(tempfile.mkdtemp())
    bin_dir = tmp / "bin"
    bin_dir.mkdir()

    jq_py = tmp / "jq_stub.py"
    jq_py.write_text(JQ_STUB, encoding="utf-8", newline="\n")
    jq_shim = bin_dir / "jq"
    jq_shim.write_text(
        f'#!/usr/bin/env bash\nexec "{Path(sys.executable).as_posix()}" "{jq_py.as_posix()}" "$@"\n',
        encoding="utf-8",
        newline="\n",
    )
    jq_shim.chmod(jq_shim.stat().st_mode | stat.S_IEXEC)

    ip_stub = bin_dir / "ip"
    ip_stub.write_text(IP_STUB_TEMPLATE, encoding="utf-8", newline="\n")
    ip_stub.chmod(ip_stub.stat().st_mode | stat.S_IEXEC)

    ts_stub = bin_dir / "tailscale"
    ts_stub.write_text(TAILSCALE_STUB, encoding="utf-8", newline="\n")
    ts_stub.chmod(ts_stub.stat().st_mode | stat.S_IEXEC)

    sleep_stub = bin_dir / "sleep"
    sleep_stub.write_text(SLEEP_STUB, encoding="utf-8", newline="\n")
    sleep_stub.chmod(sleep_stub.stat().st_mode | stat.S_IEXEC)

    status_file = tmp / "status.json"
    status_file.write_text(status_json, encoding="utf-8")

    output_file = tmp / "github_output.txt"
    script = tmp / "probe.sh"
    script.write_text(script_text, encoding="utf-8", newline="\n")

    env = os.environ.copy()
    env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
    env["TS_TAG"] = ts_tag
    env["STATUS_JSON_FILE"] = str(status_file)
    env["LOCAL_IPV4S"] = local_ipv4s
    env["GITHUB_OUTPUT"] = str(output_file)

    proc = subprocess.run([BASH, str(script)], env=env, capture_output=True, text=True, timeout=30)
    connected = None
    if output_file.exists():
        for line in output_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("connected="):
                connected = line.split("=", 1)[1]
    return proc, connected


def _run_probe(status_json, ts_tag="tag:ci-deploy", local_ipv4s=""):
    return _run_script(PROBE_SCRIPT, status_json, ts_tag, local_ipv4s)


def _run_probe2(status_json, ts_tag="tag:ci-deploy", local_ipv4s=""):
    return _run_script(PROBE2_SCRIPT, status_json, ts_tag, local_ipv4s)


class WftCi26ProbeTests(unittest.TestCase):
    def test_running_tagged_and_routable_reuses(self):
        """The healthy warm-daemon case: the CI-6-style reuse path this
        action exists for must still work."""
        proc, connected = _run_probe(
            status_json='{"BackendState":"Running","Self":{"Tags":["tag:ci-deploy"],"TailscaleIPs":["100.109.210.125","fd7a:115c::1"]}}',
            local_ipv4s="100.109.210.125",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(connected, "true")

    def test_running_tagged_but_not_routable_does_not_reuse(self):
        """The netcup-runner-1 defect, reproduced: Running + tagged, but the
        reported tailnet IPv4 is not bound to any local interface (only a
        link-local IPv6 on tailscale0, as measured). Must fall through to a
        join, not silently reuse an unroutable daemon."""
        proc, connected = _run_probe(
            status_json='{"BackendState":"Running","Self":{"Tags":["tag:ci-deploy"],"TailscaleIPs":["100.109.210.125"]}}',
            local_ipv4s="",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(connected, "false")

    def test_not_running_does_not_reuse(self):
        proc, connected = _run_probe(
            status_json='{"BackendState":"Stopped","Self":{"Tags":["tag:ci-deploy"],"TailscaleIPs":["100.109.210.125"]}}',
            local_ipv4s="100.109.210.125",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(connected, "false")

    def test_wrong_tag_does_not_reuse(self):
        proc, connected = _run_probe(
            status_json='{"BackendState":"Running","Self":{"Tags":["tag:something-else"],"TailscaleIPs":["100.109.210.125"]}}',
            local_ipv4s="100.109.210.125",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(connected, "false")

    def test_no_tailscale_ipv4_at_all_does_not_reuse(self):
        """No TailscaleIPs entry (or only IPv6) -- ip4 resolves empty, must
        not pass the `grep -qF ""` degenerate case as a match."""
        proc, connected = _run_probe(
            status_json='{"BackendState":"Running","Self":{"Tags":["tag:ci-deploy"],"TailscaleIPs":["fd7a:115c::1"]}}',
            local_ipv4s="10.0.0.5",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(connected, "false")

    def test_prefix_address_does_not_false_positive_on_a_longer_local_address(self):
        """Reviewer-caught (R3): an unanchored substring match would let a
        daemon-reported IPv4 that is a literal prefix of a DIFFERENT locally
        bound address (e.g. reported 100.109.210.1, local 100.109.210.125)
        wrongly read as routable. The daemon's own reported address is never
        actually present locally in this fixture -- must not reuse."""
        proc, connected = _run_probe(
            status_json='{"BackendState":"Running","Self":{"Tags":["tag:ci-deploy"],"TailscaleIPs":["100.109.210.1"]}}',
            local_ipv4s="100.109.210.125",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(connected, "false")

    def test_assertion_fails_against_the_pre_fix_two_condition_probe(self):
        """Mutation check: run the OLD (pre-WFT-CI-26) two-condition probe
        text against the exact defect fixture and confirm it would have
        wrongly reused -- proving this test suite actually catches the bug,
        not just exercises the new code path."""
        if not BASH:
            self.skipTest("no bash available on PATH")
        before, rest = PROBE_SCRIPT.split(
            '  # WFT-CI-26: state=Running + tagged alone', 1
        )
        _, after = rest.split(
            '  [ -n "$ip4" ] && ip -4 addr show 2>/dev/null | grep -qE "inet ${ip4//./[.]}/" && routable=true\n',
            1,
        )
        old_script = before + after
        old_script = old_script.replace(
            'echo "BackendState=${state}  hasTag(${TS_TAG})=${tagged}  routable(${ip4:-none})=${routable}"',
            'echo "BackendState=${state}  hasTag(${TS_TAG})=${tagged}"',
        )
        old_script = old_script.replace(
            'if [ "$state" = "Running" ] && [ "$tagged" = "true" ] && [ "$routable" = "true" ]; then',
            'if [ "$state" = "Running" ] && [ "$tagged" = "true" ]; then',
        )
        self.assertNotEqual(old_script, PROBE_SCRIPT, "fixture setup: old-script reconstruction did not change anything")
        self.assertNotIn("routable", old_script)

        tmp = Path(tempfile.mkdtemp())
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        jq_py = tmp / "jq_stub.py"
        jq_py.write_text(JQ_STUB, encoding="utf-8", newline="\n")
        jq_shim = bin_dir / "jq"
        jq_shim.write_text(
        f'#!/usr/bin/env bash\nexec "{Path(sys.executable).as_posix()}" "{jq_py.as_posix()}" "$@"\n',
        encoding="utf-8",
        newline="\n",
    )
        jq_shim.chmod(jq_shim.stat().st_mode | stat.S_IEXEC)
        ts_stub = bin_dir / "tailscale"
        ts_stub.write_text(TAILSCALE_STUB, encoding="utf-8", newline="\n")
        ts_stub.chmod(ts_stub.stat().st_mode | stat.S_IEXEC)

        status_file = tmp / "status.json"
        status_file.write_text(
            '{"BackendState":"Running","Self":{"Tags":["tag:ci-deploy"],"TailscaleIPs":["100.109.210.125"]}}',
            encoding="utf-8",
        )
        output_file = tmp / "github_output.txt"
        script = tmp / "probe.sh"
        script.write_text(old_script, encoding="utf-8", newline="\n")

        env = os.environ.copy()
        env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
        env["TS_TAG"] = "tag:ci-deploy"
        env["STATUS_JSON_FILE"] = str(status_file)
        env["GITHUB_OUTPUT"] = str(output_file)

        proc = subprocess.run([BASH, str(script)], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        connected = None
        for line in output_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("connected="):
                connected = line.split("=", 1)[1]
        self.assertEqual(
            connected,
            "true",
            "the pre-fix two-condition probe should wrongly reuse on this exact "
            "defect fixture -- if it doesn't, this mutation setup is broken",
        )

    def test_join_and_retry_steps_are_untouched(self):
        """Non-goal: no change to the join or retry branches."""
        for marker in (
            'if: steps.ts_probe.outputs.connected != \'true\'',
            "name: Backoff and re-probe after failed join",
            "sleep 90",
            "name: Join Tailnet (attempt 2)",
        ):
            self.assertIn(marker, ACTION)


class WftCi27Probe2Tests(unittest.TestCase):
    """ts_probe_2 ("Backoff and re-probe after failed join") carried the
    identical pre-fix two-condition defect WFT-CI-26 fixed in ts_probe,
    deliberately left untouched by that WO's own Non-goal. WFT-CI-27 applies
    the same fix here. Mirrors WftCi26ProbeTests' cases against ts_probe_2."""

    def test_running_tagged_and_routable_reuses(self):
        proc, connected = _run_probe2(
            status_json='{"BackendState":"Running","Self":{"Tags":["tag:ci-deploy"],"TailscaleIPs":["100.109.210.125","fd7a:115c::1"]}}',
            local_ipv4s="100.109.210.125",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(connected, "true")

    def test_running_tagged_but_not_routable_does_not_reuse(self):
        """The exact defect WFT-CI-26 fixed in ts_probe, reproduced here:
        must fall through (skip connected=true), letting Join attempt 2 run,
        rather than wrongly trusting an unroutable re-probed daemon."""
        proc, connected = _run_probe2(
            status_json='{"BackendState":"Running","Self":{"Tags":["tag:ci-deploy"],"TailscaleIPs":["100.109.210.125"]}}',
            local_ipv4s="",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(connected, "false")

    def test_not_running_does_not_reuse(self):
        proc, connected = _run_probe2(
            status_json='{"BackendState":"Stopped","Self":{"Tags":["tag:ci-deploy"],"TailscaleIPs":["100.109.210.125"]}}',
            local_ipv4s="100.109.210.125",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(connected, "false")

    def test_wrong_tag_does_not_reuse(self):
        proc, connected = _run_probe2(
            status_json='{"BackendState":"Running","Self":{"Tags":["tag:something-else"],"TailscaleIPs":["100.109.210.125"]}}',
            local_ipv4s="100.109.210.125",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(connected, "false")

    def test_prefix_address_does_not_false_positive_on_a_longer_local_address(self):
        """Guards against reintroducing the unanchored-substring bug WFT-CI-26's
        own reviewer caught in ts_probe -- must not leak back into ts_probe_2."""
        proc, connected = _run_probe2(
            status_json='{"BackendState":"Running","Self":{"Tags":["tag:ci-deploy"],"TailscaleIPs":["100.109.210.1"]}}',
            local_ipv4s="100.109.210.125",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(connected, "false")

    def test_no_tailscale_ipv4_at_all_does_not_reuse(self):
        """Reviewer-caught (R1): parity gap with WftCi26ProbeTests' equivalent
        case -- no TailscaleIPs entry (or only IPv6) must resolve ip4 empty
        and not pass the degenerate empty-match case as routable."""
        proc, connected = _run_probe2(
            status_json='{"BackendState":"Running","Self":{"Tags":["tag:ci-deploy"],"TailscaleIPs":["fd7a:115c::1"]}}',
            local_ipv4s="10.0.0.5",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(connected, "false")

    def test_assertion_fails_against_the_pre_fix_two_condition_probe2(self):
        """Mutation check: run the OLD (pre-WFT-CI-27) two-condition ts_probe_2
        text against the exact defect fixture and confirm it would have
        wrongly reused -- proving this test suite actually catches the bug."""
        before, rest = PROBE2_SCRIPT.split(
            '  # WFT-CI-27: same routability gap WFT-CI-26 fixed in ts_probe', 1
        )
        _, after = rest.split(
            '  [ -n "$ip4" ] && ip -4 addr show 2>/dev/null | grep -qE "inet ${ip4//./[.]}/" && routable=true\n',
            1,
        )
        old_script = before + after
        old_script = old_script.replace(
            'echo "BackendState=${state}  hasTag(${TS_TAG})=${tagged}  routable(${ip4:-none})=${routable}"',
            'echo "BackendState=${state}  hasTag(${TS_TAG})=${tagged}"',
        )
        old_script = old_script.replace(
            'if [ "$state" = "Running" ] && [ "$tagged" = "true" ] && [ "$routable" = "true" ]; then',
            'if [ "$state" = "Running" ] && [ "$tagged" = "true" ]; then',
        )
        self.assertNotEqual(old_script, PROBE2_SCRIPT, "fixture setup: old-script reconstruction did not change anything")
        self.assertNotIn("routable", old_script)

        proc, connected = _run_script(
            old_script,
            '{"BackendState":"Running","Self":{"Tags":["tag:ci-deploy"],"TailscaleIPs":["100.109.210.125"]}}',
            local_ipv4s="",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(
            connected,
            "true",
            "the pre-fix two-condition ts_probe_2 should wrongly reuse on this exact "
            "defect fixture -- if it doesn't, this mutation setup is broken",
        )

    def test_join_attempt_2_and_backoff_timing_are_untouched(self):
        """Non-goal: no change to the join steps or the retry timing."""
        for marker in (
            "sleep 90",
            "name: Join Tailnet (attempt 2)",
            "if: steps.ts_join_1.outcome == 'failure' && steps.ts_probe_2.outputs.connected != 'true'",
        ):
            self.assertIn(marker, ACTION)


if __name__ == "__main__":
    unittest.main()
