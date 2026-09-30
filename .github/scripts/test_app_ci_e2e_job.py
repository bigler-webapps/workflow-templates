"""Structural tests for app-ci's optional E2E job (WFT-CI-30).

Run with: python .github/scripts/test_app_ci_e2e_job.py

The workflow is intentionally inspected as live YAML text: GitHub expressions
make a generic YAML parser a poor fit here, and this matches the neighboring
app-ci structural tests.
"""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = (ROOT / ".github/workflows/app-ci.yml").read_text(encoding="utf-8")
INPUTS_BLOCK = CI_WORKFLOW.split("workflow_call:\n    inputs:\n", 1)[1].split(
    "\n    secrets:\n", 1
)[0]
VALIDATE_JOB = CI_WORKFLOW.split("\n  validate-inputs:\n", 1)[1].split(
    "\n  backend:\n", 1
)[0]
E2E_JOB = CI_WORKFLOW.split("\n  e2e:\n", 1)[1]


def step_body(job, name):
    match = re.search(
        rf"^      - name: {re.escape(name)}\n(?P<body>.*?)(?=^      - name:|\Z)",
        job,
        re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"step not found: {name}")
    return match.group("body")


class WftCi30StructuralTests(unittest.TestCase):
    def test_e2e_inputs_have_required_types_and_defaults(self):
        expected = {
            "run-e2e": ("boolean", "false"),
            "e2e-settings-module": ("string", "''"),
            "e2e-seed-command": ("string", "''"),
            "e2e-playwright-image": ("string", "''"),
            "e2e-capture-command": ("string", "'node tests/capture/capture.js'"),
            "e2e-capture-output-path": ("string", "'tests/capture/output'"),
        }
        for name, (input_type, default) in expected.items():
            match = re.search(
                rf"^      {re.escape(name)}:\n(?:.*\n)*?        type: (?P<type>\S+)\n(?:.*\n)*?        default: (?P<default>\S.*)$",
                INPUTS_BLOCK,
                re.MULTILINE,
            )
            self.assertIsNotNone(match, f"missing input: {name}")
            assert match is not None
            self.assertEqual(match.group("type"), input_type)
            self.assertEqual(match.group("default"), default)

    def test_run_e2e_is_off_by_default(self):
        self.assertRegex(INPUTS_BLOCK, r"      run-e2e:\n        type: boolean\n        default: false\n")

    def test_assertion_fails_if_run_e2e_defaults_true(self):
        mutated = CI_WORKFLOW.replace(
            "      run-e2e:\n        type: boolean\n        default: false",
            "      run-e2e:\n        type: boolean\n        default: true",
            1,
        )
        self.assertNotEqual(mutated, CI_WORKFLOW, "fixture setup did not match live input")
        self.assertNotRegex(mutated, r"      run-e2e:\n        type: boolean\n        default: false\n")

    def test_validate_inputs_guard_requires_three_e2e_values(self):
        guard = step_body(VALIDATE_JOB, "run-e2e requires its own inputs")
        self.assertIn("if: ${{ inputs.run-e2e && (inputs.e2e-settings-module == '' || inputs.e2e-seed-command == '' || inputs.e2e-playwright-image == '') }}", guard)
        self.assertIn("exit 1", guard)

    def test_assertion_fails_if_e2e_guard_loses_a_required_input(self):
        mutated = VALIDATE_JOB.replace(
            "inputs.e2e-seed-command == '' || ",
            "",
            1,
        )
        self.assertNotEqual(mutated, VALIDATE_JOB, "fixture setup did not match live guard")
        guard = step_body(mutated, "run-e2e requires its own inputs")
        self.assertNotIn("inputs.e2e-seed-command == ''", guard)

    def test_job_is_opt_in_and_builds_full_image(self):
        self.assertIn("if: ${{ inputs.run-e2e }}", E2E_JOB)
        build = step_body(E2E_JOB, "Build full app image")
        self.assertNotIn("--target", build)
        self.assertIn("inputs.dockerfile", build)
        self.assertIn("inputs.build-context", build)
        self.assertIn("HRAM_ENGINE_READ_TOKEN", build)
        self.assertIn("VITE_APP_MUI_LICENSE_KEY", build)

    def test_assertion_fails_if_full_build_is_replaced_by_target_build(self):
        build = step_body(E2E_JOB, "Build full app image")
        mutated = build.replace("DOCKER_BUILDKIT=1 docker build \\", "DOCKER_BUILDKIT=1 docker build --target backend_test \\", 1)
        self.assertNotEqual(mutated, build, "fixture setup did not match build command")
        self.assertIn("--target", mutated)

    def test_containers_and_network_are_unique_and_redis_uses_internal_port(self):
        # Each resource's OWN env-var assignment must carry both run.id and
        # run_attempt -- a bare "somewhere in the job" search (the pre-fix
        # version of this test) would still pass if any one of the five had
        # been given a static name, so long as another line happened to
        # mention github.run_id.
        for var in ("CI_IMAGE", "E2E_NETWORK", "E2E_DB_CONTAINER", "E2E_REDIS_CONTAINER", "E2E_APP_CONTAINER"):
            match = re.search(rf"^      {var}: \S+\$\{{\{{ github\.run_id \}}\}}-\$\{{\{{ github\.run_attempt \}}\}}$", E2E_JOB, re.MULTILINE)
            self.assertIsNotNone(match, f"{var} is not scoped by both github.run_id and github.run_attempt")
        app = step_body(E2E_JOB, "Start app and wait for health")
        self.assertIn("-e REDIS_HOST=\"$E2E_REDIS_CONTAINER\"", app)
        self.assertIn("-e DB_PORT='5432'", app)
        self.assertIn("docker port \"$E2E_APP_CONTAINER\" 8000/tcp", app)
        self.assertNotIn("6379:", app)

    def test_assertion_fails_if_a_resource_name_loses_run_attempt_scoping(self):
        mutated = E2E_JOB.replace(
            "      E2E_DB_CONTAINER: ci-e2e-db-${{ github.run_id }}-${{ github.run_attempt }}",
            "      E2E_DB_CONTAINER: ci-e2e-db-${{ github.run_id }}",
            1,
        )
        self.assertNotEqual(mutated, E2E_JOB, "fixture setup did not match live env block")
        match = re.search(r"^      E2E_DB_CONTAINER: \S+\$\{\{ github\.run_id \}\}-\$\{\{ github\.run_attempt \}\}$", mutated, re.MULTILINE)
        self.assertIsNone(match, "guard would not catch a resource name losing run_attempt scoping")

    def test_db_and_redis_have_health_checks_and_are_awaited_before_migrate(self):
        deps = step_body(E2E_JOB, "Create E2E network and dependencies")
        self.assertIn("--health-cmd='pg_isready -U test'", deps)
        self.assertIn("--health-cmd='redis-cli ping'", deps)
        self.assertIn('$(docker inspect --format \'{{.State.Health.Status}}\' "$container")', deps)
        self.assertIn('if [ "$status" = "healthy" ]', deps)
        # both containers must be in the wait loop, not just the DB
        self.assertIn('for container in "$E2E_DB_CONTAINER" "$E2E_REDIS_CONTAINER"', deps)

    def test_assertion_fails_if_redis_loses_its_health_check(self):
        mutated = E2E_JOB.replace(
            "          docker run -d --name \"$E2E_REDIS_CONTAINER\" --network \"$E2E_NETWORK\" \\\n            --health-cmd='redis-cli ping' \\\n            --health-interval=10s --health-timeout=5s --health-retries=10 \\\n            redis:7\n",
            "          docker run -d --name \"$E2E_REDIS_CONTAINER\" --network \"$E2E_NETWORK\" redis:7\n",
            1,
        )
        self.assertNotEqual(mutated, E2E_JOB, "fixture setup did not match live redis run command")
        deps = step_body(mutated, "Create E2E network and dependencies")
        self.assertNotIn("--health-cmd='redis-cli ping'", deps)

    def test_migrate_and_seed_step_runs_both_commands(self):
        step = step_body(E2E_JOB, "Migrate and seed E2E database")
        self.assertIn("python manage.py migrate --noinput", step)
        self.assertIn('python manage.py "${{ inputs.e2e-seed-command }}"', step)

    def test_assertion_fails_if_migrate_step_is_dropped(self):
        mutated = E2E_JOB.replace(
            '          docker exec "$E2E_APP_CONTAINER" python manage.py migrate --noinput\n',
            "",
            1,
        )
        self.assertNotEqual(mutated, E2E_JOB, "fixture setup did not match live migrate line")
        step = step_body(mutated, "Migrate and seed E2E database")
        self.assertNotIn("python manage.py migrate --noinput", step)

    def test_playwright_dependencies_are_installed_before_specs_run(self):
        step = step_body(E2E_JOB, "Install Playwright dependencies")
        self.assertIn("corepack enable", step)
        self.assertIn("corepack prepare pnpm@${{ inputs.pnpm-version }} --activate", step)
        self.assertIn("pnpm install --frozen-lockfile", step)
        # the step must exist strictly before "Playwright specs" in the job body
        self.assertLess(E2E_JOB.index("- name: Install Playwright dependencies"), E2E_JOB.index("- name: Playwright specs"))

    def test_assertion_fails_if_pnpm_install_is_dropped(self):
        mutated = E2E_JOB.replace("pnpm install --frozen-lockfile", "true", 1)
        self.assertNotEqual(mutated, E2E_JOB, "fixture setup did not match live install command")
        step = step_body(mutated, "Install Playwright dependencies")
        self.assertNotIn("pnpm install --frozen-lockfile", step)

    def test_health_wait_loop_retries_and_fails_loudly(self):
        step = step_body(E2E_JOB, "Start app and wait for health")
        self.assertIn('curl -fsS "http://127.0.0.1:$APP_PORT/api/healthz"', step)
        self.assertIn("for attempt in $(seq 1 60)", step)
        self.assertIn('echo "::error::app health check did not become ready"', step)
        self.assertIn("exit 1", step)

    def test_assertion_fails_if_health_failure_path_is_removed(self):
        mutated = E2E_JOB.replace(
            '          echo "::error::app health check did not become ready"\n          docker logs "$E2E_APP_CONTAINER" || true\n          exit 1\n',
            "",
            1,
        )
        self.assertNotEqual(mutated, E2E_JOB, "fixture setup did not match live failure path")
        step = step_body(mutated, "Start app and wait for health")
        self.assertNotIn('echo "::error::app health check did not become ready"', step)

    def test_app_port_is_loopback_only_and_parsed_from_a_single_line(self):
        step = step_body(E2E_JOB, "Start app and wait for health")
        self.assertIn("-p 127.0.0.1::8000", step)
        self.assertNotRegex(step, r"docker run -d --name \"\$E2E_APP_CONTAINER\" --network \"\$E2E_NETWORK\" -p 8000 ")
        # `docker port` can emit an IPv4 AND an IPv6 line for one publish;
        # piping through `head -n1` before the port-only sed is what keeps a
        # dual-stack response from producing a multi-line APP_PORT value that
        # corrupts the GITHUB_ENV append (WFT-CI-30 blocker, regression lens).
        self.assertIn('docker port "$E2E_APP_CONTAINER" 8000/tcp | head -n1 | sed', step)

    def test_assertion_fails_if_loopback_bind_is_reverted_to_all_interfaces(self):
        mutated = E2E_JOB.replace(
            'docker run -d --name "$E2E_APP_CONTAINER" --network "$E2E_NETWORK" -p 127.0.0.1::8000 \\',
            'docker run -d --name "$E2E_APP_CONTAINER" --network "$E2E_NETWORK" -p 8000 \\',
            1,
        )
        self.assertNotEqual(mutated, E2E_JOB, "fixture setup did not match live bind flag")
        step = step_body(mutated, "Start app and wait for health")
        self.assertNotIn("-p 127.0.0.1::8000", step)

    def test_assertion_fails_if_head_n1_is_dropped_from_port_parsing(self):
        mutated = E2E_JOB.replace(
            'docker port "$E2E_APP_CONTAINER" 8000/tcp | head -n1 | sed',
            'docker port "$E2E_APP_CONTAINER" 8000/tcp | sed',
            1,
        )
        self.assertNotEqual(mutated, E2E_JOB, "fixture setup did not match live port-parsing line")
        step = step_body(mutated, "Start app and wait for health")
        self.assertNotIn('docker port "$E2E_APP_CONTAINER" 8000/tcp | head -n1 | sed', step)

    def test_assertion_fails_if_redis_gets_a_host_port_mapping(self):
        mutated = E2E_JOB.replace(
            "-e REDIS_HOST=\"$E2E_REDIS_CONTAINER\"",
            "-p 6379:6379 -e REDIS_HOST=localhost",
            1,
        )
        self.assertNotEqual(mutated, E2E_JOB, "fixture setup did not match Redis wiring")
        app = step_body(mutated, "Start app and wait for health")
        self.assertIn("6379:6379", app)

    def test_specs_gate_quarantine_is_non_blocking_and_capture_is_reported(self):
        specs = step_body(E2E_JOB, "Playwright specs")
        self.assertIn("--grep-invert \"@quarantine\"", specs)
        self.assertNotIn("continue-on-error: true", specs)

        quarantine = step_body(E2E_JOB, "Playwright quarantine specs")
        self.assertIn("continue-on-error: true", quarantine)
        self.assertIn("--grep \"@quarantine\"", quarantine)
        self.assertIn("GITHUB_STEP_SUMMARY", quarantine)
        # Both must carry if: always() -- otherwise a FAILING (gating) specs
        # step causes GitHub Actions to skip every later step by default, and
        # quarantine reporting / capture ("runs after the specs", per the
        # Envelope) would silently never happen on the one run where the
        # gate actually fires (envelope lens finding).
        self.assertIn("        if: always()\n        continue-on-error: true", quarantine)

        capture = step_body(E2E_JOB, "Capture E2E screens and report")
        self.assertIn("inputs.e2e-capture-command", capture)
        self.assertIn("        if: always()\n        continue-on-error: true", capture)
        upload = step_body(E2E_JOB, "Upload E2E capture artifact")
        self.assertIn("if: always()", upload)
        self.assertIn("inputs.e2e-capture-output-path", upload)

    def test_assertion_fails_if_quarantine_loses_its_always_gate(self):
        mutated = E2E_JOB.replace(
            "      - name: Playwright quarantine specs\n        id: quarantine\n        if: always()\n        continue-on-error: true",
            "      - name: Playwright quarantine specs\n        id: quarantine\n        continue-on-error: true",
            1,
        )
        self.assertNotEqual(mutated, E2E_JOB, "fixture setup did not match live quarantine step header")
        quarantine = step_body(mutated, "Playwright quarantine specs")
        self.assertNotIn("        if: always()\n        continue-on-error: true", quarantine)

    def test_assertion_fails_if_capture_loses_its_always_gate(self):
        mutated = E2E_JOB.replace(
            "      - name: Capture E2E screens and report\n        if: always()\n        continue-on-error: true",
            "      - name: Capture E2E screens and report\n        continue-on-error: true",
            1,
        )
        self.assertNotEqual(mutated, E2E_JOB, "fixture setup did not match live capture step header")
        capture = step_body(mutated, "Capture E2E screens and report")
        self.assertNotIn("        if: always()\n        continue-on-error: true", capture)

    def test_assertion_fails_if_quarantine_becomes_blocking(self):
        mutated = E2E_JOB.replace(
            "        continue-on-error: true\n        run: |\n          set +e\n          docker run --rm --network host \\\n            -e PLAYWRIGHT_BASE_URL",
            "        run: |\n          set -euo pipefail\n          docker run --rm --network host \\\n            -e PLAYWRIGHT_BASE_URL",
            1,
        )
        self.assertNotEqual(mutated, E2E_JOB, "fixture setup did not match quarantine step")
        quarantine = step_body(mutated, "Playwright quarantine specs")
        self.assertNotIn("continue-on-error: true", quarantine)

    def test_cleanup_is_unconditional_and_removes_all_resources(self):
        cleanup = step_body(E2E_JOB, "Cleanup E2E containers, network, and image")
        self.assertIn("if: always()", cleanup)
        for variable in ("E2E_APP_CONTAINER", "E2E_REDIS_CONTAINER", "E2E_DB_CONTAINER"):
            self.assertIn(variable, cleanup)
        self.assertIn("docker network rm \"$E2E_NETWORK\"", cleanup)
        self.assertIn("docker image rm -f \"$CI_IMAGE\"", cleanup)

    def test_assertion_fails_if_cleanup_is_not_always(self):
        mutated = E2E_JOB.replace(
            "      - name: Cleanup E2E containers, network, and image\n        if: always()",
            "      - name: Cleanup E2E containers, network, and image\n        if: success()",
            1,
        )
        self.assertNotEqual(mutated, E2E_JOB, "fixture setup did not match cleanup step")
        cleanup = step_body(mutated, "Cleanup E2E containers, network, and image")
        self.assertNotIn("if: always()", cleanup)


if __name__ == "__main__":
    unittest.main()
