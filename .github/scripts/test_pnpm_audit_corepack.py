"""Exercise app-ci's report-only pnpm audit step with a stub Corepack."""

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = (ROOT / ".github/workflows/app-ci.yml").read_text(encoding="utf-8")
SECURITY_JOB = CI_WORKFLOW.split("\n  security:\n", 1)[1].split("\n  frontend:\n", 1)[0]
GIT_BASH = Path("C:/Program Files/Git/bin/bash.exe")
CAPTURE_BASH = str(GIT_BASH) if sys.platform == "win32" and GIT_BASH.is_file() else shutil.which("bash")
WARNING = '::warning::pnpm audit findings (non-blocking v1)'


def step_body(job, name):
    match = re.search(
        rf"^      - name: {re.escape(name)}\n(?P<body>.*?)(?=^      - name:|\Z)",
        job,
        re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"step not found: {name}")
    return match.group("body")


def shell_body(step):
    match = re.search(
        r"^        run: \|\n(?P<run>(?:^          .*\n|^\n)*)",
        step,
        re.MULTILINE,
    )
    if match is None:
        raise AssertionError("step does not have a literal run block")
    return "\n".join(
        line[10:] if line.startswith("          ") else ""
        for line in match.group("run").splitlines()
    ) + "\n"


class PnpmAuditCorepackTests(unittest.TestCase):
    def setUp(self):
        self.step = step_body(SECURITY_JOB, "pnpm audit (report-only)")
        self.script = shell_body(self.step)

    def test_live_step_uses_corepack_without_installing_global_shims(self):
        self.assertNotRegex(
            self.script,
            r"(?i)corepack\s+(?:enable|prepare)|(?:npm|pnpm)\s+(?:install|i)\s+-g\s+pnpm",
            "the audit step must not install or enable a global pnpm shim",
        )
        self.assertIn(
            "corepack pnpm audit --audit-level high",
            self.script,
            "the live report-only audit must run through Corepack",
        )
        self.assertIn("continue-on-error: true", self.step)

    def test_security_node_setup_uses_frontend_pin_and_precedes_audit(self):
        setup = step_body(SECURITY_JOB, "Set up Node for the pnpm audit")
        frontend_job = CI_WORKFLOW.split("\n  frontend:\n", 1)[1]
        frontend_setup = re.search(
            r"^      - uses: actions/setup-node@(?P<sha>[0-9a-f]{40})[^\n]*\n"
            r"        with:\n"
            r"          node-version: (?P<version>[^\n]+)",
            frontend_job,
            re.MULTILINE,
        )
        self.assertIsNotNone(frontend_setup, "frontend setup-node pin/input not found")
        assert frontend_setup is not None
        self.assertIn(
            f"uses: actions/setup-node@{frontend_setup.group('sha')}",
            setup,
            "security must reuse the frontend's pinned setup-node action",
        )
        self.assertIn(
            f"node-version: {frontend_setup.group('version')}",
            setup,
            "security must use the frontend's node-version input",
        )
        self.assertEqual(
            frontend_setup.group("sha"),
            "49933ea5288caeca8642d1e84afbd3f7d6820020",
            "the frontend's approved setup-node pin changed",
        )
        self.assertEqual(frontend_setup.group("version"), "${{ inputs.node-version }}")
        self.assertRegex(
            setup,
            r"(?m)^        if: " + re.escape("${{ hashFiles(format('{0}/pnpm-lock.yaml', inputs.frontend-path)) != '' }}") + r"$",
        )
        self.assertNotIn("run-frontend-tests", setup)
        self.assertRegex(setup, r"(?m)^        continue-on-error: true$")
        self.assertRegex(
            setup,
            r"(?m)^        uses: actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020[^\n]*\n"
            r"        with:\n          node-version: " + re.escape("${{ inputs.node-version }}") + r"\n",
        )
        self.assertLess(
            SECURITY_JOB.index("- name: Set up Node for the pnpm audit"),
            SECURITY_JOB.index("- name: pnpm audit (report-only)"),
        )

    @unittest.skipUnless(CAPTURE_BASH, "Bash is required to execute the extracted workflow step")
    def test_lockfile_runs_audit_in_frontend_and_forwards_arguments(self):
        result, cwd, args = self._run_step(lockfile=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(cwd.endswith("client-ui"), cwd)
        self.assertEqual(args, ["pnpm", "audit", "--audit-level", "high"])

    @unittest.skipUnless(CAPTURE_BASH, "Bash is required to execute the extracted workflow step")
    def test_missing_lockfile_skips_corepack(self):
        result, cwd, args = self._run_step(lockfile=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(cwd)
        self.assertIsNone(args)

    @unittest.skipUnless(CAPTURE_BASH, "Bash is required to execute the extracted workflow step")
    def test_audit_failure_keeps_the_existing_non_blocking_warning(self):
        result, cwd, args = self._run_step(lockfile=True, audit_status=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(WARNING, result.stdout)
        self.assertTrue(cwd.endswith("client-ui"), cwd)
        self.assertEqual(args, ["pnpm", "audit", "--audit-level", "high"])

    def _run_step(self, *, lockfile, audit_status=0):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            frontend = root / "client-ui"
            frontend.mkdir()
            if lockfile:
                (frontend / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")

            stub_dir = root / "stub-bin"
            stub_dir.mkdir()
            stub = stub_dir / "corepack"
            stub.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$PWD\" > corepack-cwd.txt\n"
                "printf '%s\\n' \"$@\" > corepack-args.txt\n"
                "if [ \"$1\" = enable ]; then exit 97; fi\n"
                "exit \"${AUDIT_STATUS:-0}\"\n",
                encoding="utf-8",
            )
            stub.chmod(0o755)
            # A future regression to bare pnpm must fail inside the fixture,
            # never reach the machine's real package manager or the network.
            bare_pnpm = stub_dir / "pnpm"
            bare_pnpm.write_text("#!/bin/sh\nexit 98\n", encoding="utf-8")
            bare_pnpm.chmod(0o755)

            # This substitutes only the known workflow input with a controlled
            # relative fixture path. No caller-controlled expression is run.
            executable_script = self.script.replace("${{ inputs.frontend-path }}", "client-ui")
            self.assertNotIn("${{", executable_script, "unexpected GitHub expression in extracted fixture")
            environment = os.environ.copy()
            environment["PATH"] = str(stub_dir) + os.pathsep + environment.get("PATH", "")
            environment["AUDIT_STATUS"] = str(audit_status)
            result = subprocess.run(
                [CAPTURE_BASH, "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", executable_script],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
            )
            cwd_log = frontend / "corepack-cwd.txt"
            args_log = frontend / "corepack-args.txt"
            cwd = cwd_log.read_text(encoding="utf-8").strip() if cwd_log.exists() else None
            args = args_log.read_text(encoding="utf-8").splitlines() if args_log.exists() else None
            return result, cwd, args


if __name__ == "__main__":
    unittest.main()
