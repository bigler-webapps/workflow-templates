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
        app = step_body(E2E_JOB, "Start app")
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

    def test_capture_reads_manifest_cells_and_reseeds_before_each(self):
        # Cell IDs come from reading the plain manifest.json already in the
        # checkout (via node INSIDE the Playwright container, which has
        # Node -- the runner itself does not, unlike the frontend job), never
        # from hardcoding kerzenziehen's own cell names. Each cell gets its
        # own reseed via docker exec (same mechanism as the specs loop) before
        # the capture command runs with --routes=<cell-id>, and every capture
        # invocation carries E2E_RESEED=runner too.
        capture = step_body(E2E_JOB, "Capture E2E screens and report")
        self.assertIn("require('./tests/capture/manifest.json').screens.map(s=>s.id)", capture)
        self.assertIn('docker exec "$E2E_APP_CONTAINER" python manage.py "${{ inputs.e2e-seed-command }}"', capture)
        self.assertIn("-e E2E_RESEED=runner", capture)
        self.assertIn("${{ inputs.e2e-capture-command }} --routes=$cell", capture)
        self.assertIn('echo "::warning::E2E capture: no cells found in tests/capture/manifest.json."', capture)

    def test_assertion_fails_if_capture_stops_reseeding_per_cell(self):
        mutated = E2E_JOB.replace(
            'echo "::notice::reseeding before capture cell $cell"\n            docker exec "$E2E_APP_CONTAINER" python manage.py "${{ inputs.e2e-seed-command }}"\n',
            "",
            1,
        )
        self.assertNotEqual(mutated, E2E_JOB, "fixture setup did not match live per-cell reseed line")
        capture = step_body(mutated, "Capture E2E screens and report")
        self.assertNotIn('docker exec "$E2E_APP_CONTAINER" python manage.py "${{ inputs.e2e-seed-command }}"', capture)

    def test_capture_distinguishes_enumeration_failure_from_genuinely_zero_cells(self):
        # Review finding: under `set +e`, a failed manifest-enumeration docker
        # run produces the SAME empty $CELLS as a manifest that genuinely has
        # zero cells -- silently misreporting a broken container/image as
        # "nothing to capture" instead of a real setup failure.
        capture = step_body(E2E_JOB, "Capture E2E screens and report")
        self.assertIn("cells_status=$?", capture)
        self.assertIn('if [ "$cells_status" -ne 0 ]; then', capture)
        self.assertIn('echo "::warning::E2E capture: manifest enumeration itself failed', capture)

    def test_assertion_fails_if_enumeration_failure_check_is_dropped(self):
        mutated = E2E_JOB.replace(
            'cells_status=$?\n          if [ "$cells_status" -ne 0 ]; then\n            echo "::warning::E2E capture: manifest enumeration itself failed (exit $cells_status), not treated as zero cells."\n            exit 0\n          fi\n',
            "",
            1,
        )
        self.assertNotEqual(mutated, E2E_JOB, "fixture setup did not match live enumeration-failure check")
        capture = step_body(mutated, "Capture E2E screens and report")
        self.assertNotIn("cells_status=$?", capture)

    def test_capture_skips_a_cell_when_its_reseed_fails_rather_than_capturing_stale_data(self):
        # Review finding: under `set +e`, a failed reseed was silently ignored
        # and Playwright/capture proceeded against stale data anyway, with the
        # loop only ever recording the CAPTURE command's own exit code.
        capture = step_body(E2E_JOB, "Capture E2E screens and report")
        self.assertIn("reseed_status=$?", capture)
        self.assertIn('if [ "$reseed_status" -ne 0 ]; then', capture)
        self.assertIn("continue", capture)

    def test_assertion_fails_if_capture_stops_skipping_on_reseed_failure(self):
        mutated = E2E_JOB.replace(
            'reseed_status=$?\n            if [ "$reseed_status" -ne 0 ]; then\n              echo "::warning::reseed failed before capture cell $cell -- skipping this cell rather than capturing stale data"\n              worst=$reseed_status\n              continue\n            fi\n',
            "",
            1,
        )
        self.assertNotEqual(mutated, E2E_JOB, "fixture setup did not match live capture reseed-failure handling")
        capture = step_body(mutated, "Capture E2E screens and report")
        self.assertNotIn("reseed_status=$?", capture)

    def test_capture_step_declares_git_as_a_safe_directory(self):
        # Live-confirmed on kerzenziehen's ci-test/WFT-CI-30 run: even with
        # the full checkout mounted, git refuses to operate on it with
        # "fatal: detected dubious ownership in repository at '/workspace'"
        # -- git >=2.35's protection against a repo whose files are owned by
        # a different UID than the process running git, which is exactly
        # what a bind mount from the runner's host UID into the Playwright
        # image's own container user produces. The GIT_CONFIG_* env vars are
        # a stateless equivalent of `git config --global --add safe.directory`
        # -- no config file written into the ephemeral container.
        capture = step_body(E2E_JOB, "Capture E2E screens and report")
        self.assertIn("-e GIT_CONFIG_COUNT=1", capture)
        self.assertIn("-e GIT_CONFIG_KEY_0=safe.directory", capture)
        self.assertIn("-e GIT_CONFIG_VALUE_0=/workspace", capture)

    def test_assertion_fails_if_the_safe_directory_override_is_dropped(self):
        mutated = E2E_JOB.replace(
            "              -e GIT_CONFIG_COUNT=1 \\\n              -e GIT_CONFIG_KEY_0=safe.directory \\\n              -e GIT_CONFIG_VALUE_0=/workspace \\\n",
            "",
            1,
        )
        self.assertNotEqual(mutated, E2E_JOB, "fixture setup did not match live capture env vars")
        capture = step_body(mutated, "Capture E2E screens and report")
        self.assertNotIn("GIT_CONFIG_COUNT", capture)

    def test_playwright_containers_run_as_the_host_uid_not_root(self):
        # Live-confirmed on kerzenziehen's ci-test/WFT-CI-30 run: the
        # containers write into the bind-mounted host checkout (node_modules,
        # pnpm's virtual store) as whatever user the image defaults to
        # (root). Root-owned files in a self-hosted runner's shared checkout
        # directory can't be removed by that runner's own (non-root) cleanup
        # process -- the NEXT run's `actions/checkout` failed outright
        # ("EACCES: permission denied, rmdir ... .pnpm-store"), and only
        # recovered because the checkout action itself falls back to
        # recreating the whole workdir. Matching the container's user to the
        # host's own UID/GID is the fix, not a per-file cleanup step.
        for name in ("Install Playwright dependencies", "Playwright specs", "Playwright quarantine specs", "Capture E2E screens and report"):
            step = step_body(E2E_JOB, name)
            self.assertIn('--user "$(id -u):$(id -g)" \\', step, f"{name} must run as the host UID, not the image default (root)")
            self.assertIn("-e HOME=/tmp \\", step, f"{name} needs HOME set -- an arbitrary UID has no /etc/passwd entry, breaking pnpm/npm's config resolution")

    def test_assertion_fails_if_a_step_drops_the_user_override(self):
        mutated = E2E_JOB.replace('--user "$(id -u):$(id -g)" \\\n            -e HOME=/tmp \\\n', "", 1)
        self.assertNotEqual(mutated, E2E_JOB, "fixture setup did not match live user/home lines")
        step = step_body(mutated, "Install Playwright dependencies")
        self.assertNotIn('--user "$(id -u):$(id -g)"', step)

    def test_playwright_containers_mount_the_full_checkout_not_just_frontend(self):
        # Live-confirmed on kerzenziehen's ci-test/WFT-CI-30 run: the capture
        # script's own build-provenance check (KZ-E2E-8) shells `git rev-parse
        # HEAD`, which needs a reachable .git -- mounting only frontend-path
        # (the pre-fix version of this workflow) put .git outside the mount
        # entirely and every git-plumbing call inside the container failed.
        for name in ("Install Playwright dependencies", "Playwright specs", "Playwright quarantine specs", "Capture E2E screens and report"):
            step = step_body(E2E_JOB, name)
            self.assertIn('-v "${{ github.workspace }}:/workspace" \\', step, f"{name} must mount the full checkout, not just frontend-path")
            self.assertIn('-w "/workspace/${{ inputs.frontend-path }}" \\', step, f"{name} must still run from the frontend-path subdirectory")
            self.assertNotIn(":/workspace/frontend", step, f"{name} must not use the old frontend-only mount target")

    def test_assertion_fails_if_a_step_reverts_to_frontend_only_mount(self):
        mutated = E2E_JOB.replace(
            '-v "${{ github.workspace }}:/workspace" \\\n            -w "/workspace/${{ inputs.frontend-path }}" \\',
            '-v "${{ github.workspace }}/${{ inputs.frontend-path }}:/workspace/frontend" \\\n            -w /workspace/frontend \\',
            1,
        )
        self.assertNotEqual(mutated, E2E_JOB, "fixture setup did not match live mount lines")
        step = step_body(mutated, "Install Playwright dependencies")
        self.assertNotIn('-v "${{ github.workspace }}:/workspace" \\', step)

    def test_playwright_dependencies_are_installed_before_specs_run(self):
        # Live-confirmed on kerzenziehen's ci-test/WFT-CI-30 run:
        # `corepack enable` tries to write a SYSTEM-WIDE symlink
        # (/usr/bin/pnpx), which needs root -- directly conflicting with
        # --user "$(id -u):$(id -g)" above. `npx` needs no shim install at
        # all (it caches in $HOME, which is writable), so it is the only
        # invocation compatible with running as a non-root, host-matching UID.
        step = step_body(E2E_JOB, "Install Playwright dependencies")
        self.assertNotIn("corepack enable", step, "corepack enable needs root for its system-wide shim; incompatible with --user")
        self.assertIn("npx --yes pnpm@${{ inputs.pnpm-version }} install --frozen-lockfile", step)
        # the step must exist strictly before "Playwright specs" in the job body
        self.assertLess(E2E_JOB.index("- name: Install Playwright dependencies"), E2E_JOB.index("- name: Playwright specs"))

    def test_assertion_fails_if_pnpm_install_is_dropped(self):
        mutated = E2E_JOB.replace("npx --yes pnpm@${{ inputs.pnpm-version }} install --frozen-lockfile", "true", 1)
        self.assertNotEqual(mutated, E2E_JOB, "fixture setup did not match live install command")
        step = step_body(mutated, "Install Playwright dependencies")
        self.assertNotIn("pnpm install --frozen-lockfile", step)

    def test_assertion_fails_if_corepack_enable_is_reintroduced(self):
        mutated = E2E_JOB.replace(
            "npx --yes pnpm@${{ inputs.pnpm-version }} install --frozen-lockfile",
            "corepack enable && corepack prepare pnpm@${{ inputs.pnpm-version }} --activate && pnpm install --frozen-lockfile",
            1,
        )
        self.assertNotEqual(mutated, E2E_JOB, "fixture setup did not match live install command")
        step = step_body(mutated, "Install Playwright dependencies")
        self.assertIn("corepack enable", step)

    def test_health_wait_loop_retries_and_fails_loudly(self):
        step = step_body(E2E_JOB, "Wait for app health")
        self.assertIn("curl -s -o /dev/null -w '%{http_code}'", step)
        self.assertIn('"http://127.0.0.1:$APP_PORT/api/healthz"', step)
        self.assertIn("for attempt in $(seq 1 60)", step)
        self.assertIn('echo "::error::app health check did not become ready"', step)
        self.assertIn("exit 1", step)

    def test_health_wait_accepts_any_response_but_not_connection_refused(self):
        # Live-confirmed on kerzenziehen's ci-test/WFT-CI-30 re-run: healthz's
        # shared "config" check reports 503 whenever AUTH_METHODS.social_login
        # is on with a provider missing its OAuth client_id -- true for every
        # caller's CI env by design (never provisioning real third-party
        # secrets here). Requiring status 200 would make readiness depend on
        # config this job must never supply. DB/Redis are independently
        # already proven reachable by this point (migrate just ran real SQL;
        # Redis's own docker health-cmd had to pass earlier) -- "curl got ANY
        # HTTP response" is what this step actually needs, not "got a 200".
        step = step_body(E2E_JOB, "Wait for app health")
        self.assertNotIn("curl -f", step, "must not fail out on a non-2xx status (e.g. healthz's own unrelated config-check 503)")
        self.assertIn('if [ -n "$code" ] && [ "$code" != "000" ]; then', step)

    def test_assertion_fails_if_health_check_reverts_to_requiring_200(self):
        mutated = E2E_JOB.replace(
            "            code=\"$(curl -s -o /dev/null -w '%{http_code}' \"http://127.0.0.1:$APP_PORT/api/healthz\" || true)\"\n            # curl prints the literal string \"000\" (not empty) when it never\n            # got a response at all (connection refused/reset) -- that is\n            # the \"not up yet\" case; anything else is a real HTTP response.\n            if [ -n \"$code\" ] && [ \"$code\" != \"000\" ]; then\n              exit 0\n            fi\n",
            "            if curl -fsS \"http://127.0.0.1:$APP_PORT/api/healthz\" >/dev/null; then\n              exit 0\n            fi\n",
            1,
        )
        self.assertNotEqual(mutated, E2E_JOB, "fixture setup did not match live health-wait body")
        step = step_body(mutated, "Wait for app health")
        self.assertIn("curl -f", step)

    def test_assertion_fails_if_health_failure_path_is_removed(self):
        mutated = E2E_JOB.replace(
            '          echo "::error::app health check did not become ready"\n          docker logs "$E2E_APP_CONTAINER" || true\n          exit 1\n',
            "",
            1,
        )
        self.assertNotEqual(mutated, E2E_JOB, "fixture setup did not match live failure path")
        step = step_body(mutated, "Wait for app health")
        self.assertNotIn('echo "::error::app health check did not become ready"', step)

    def test_health_wait_runs_after_migrate_not_before(self):
        # django_core_micha's shared /api/healthz view fails its own
        # "migrations" check (503) while any migration is pending -- waiting
        # on it BEFORE migrate is a deadlock the app can never recover from.
        # Confirmed live on kerzenziehen's ci-test/WFT-CI-30 run: Daphne was
        # up and answering the whole time, just always 503, because migrate
        # never got a chance to run first. docker exec (the migrate step)
        # needs no HTTP readiness at all, only the container running.
        migrate_idx = E2E_JOB.index("- name: Migrate and seed E2E database")
        health_idx = E2E_JOB.index("- name: Wait for app health")
        self.assertLess(migrate_idx, health_idx, "the health-wait step must come AFTER migrate, or it can never pass")

    def test_assertion_fails_if_health_wait_is_moved_back_before_migrate(self):
        mutated_job = (
            E2E_JOB.replace("      - name: Migrate and seed E2E database\n", "__MIGRATE_MARKER__\n", 1)
            .replace("      - name: Wait for app health\n", "      - name: Migrate and seed E2E database\n", 1)
            .replace("__MIGRATE_MARKER__\n", "      - name: Wait for app health\n", 1)
        )
        self.assertNotEqual(mutated_job, E2E_JOB, "fixture setup did not swap the two step headers")
        migrate_idx = mutated_job.index("- name: Migrate and seed E2E database")
        health_idx = mutated_job.index("- name: Wait for app health")
        self.assertGreater(migrate_idx, health_idx, "swap fixture did not actually invert the order")

    def test_app_port_is_loopback_only_and_parsed_from_a_single_line(self):
        step = step_body(E2E_JOB, "Start app")
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
        step = step_body(mutated, "Start app")
        self.assertNotIn("-p 127.0.0.1::8000", step)

    def test_assertion_fails_if_head_n1_is_dropped_from_port_parsing(self):
        mutated = E2E_JOB.replace(
            'docker port "$E2E_APP_CONTAINER" 8000/tcp | head -n1 | sed',
            'docker port "$E2E_APP_CONTAINER" 8000/tcp | sed',
            1,
        )
        self.assertNotEqual(mutated, E2E_JOB, "fixture setup did not match live port-parsing line")
        step = step_body(mutated, "Start app")
        self.assertNotIn('docker port "$E2E_APP_CONTAINER" 8000/tcp | head -n1 | sed', step)

    def test_assertion_fails_if_redis_gets_a_host_port_mapping(self):
        mutated = E2E_JOB.replace(
            "-e REDIS_HOST=\"$E2E_REDIS_CONTAINER\"",
            "-p 6379:6379 -e REDIS_HOST=localhost",
            1,
        )
        self.assertNotEqual(mutated, E2E_JOB, "fixture setup did not match Redis wiring")
        app = step_body(mutated, "Start app")
        self.assertIn("6379:6379", app)

    def test_specs_bucket_by_source_not_by_playwright_cli_output(self):
        # Operator decision (amended after the live proof showed the Playwright
        # image has no Docker at all): the runner resets the DB itself before
        # each spec file, so specs run ONE FILE AT A TIME rather than in one
        # bulk `playwright test` invocation. Bucketing must come from grepping
        # each file's OWN source for "@quarantine" -- never from parsing
        # Playwright's own --list/JSON-reporter output (version-fragile, never
        # actually observed in --list mode).
        specs = step_body(E2E_JOB, "Playwright specs")
        self.assertNotIn("--grep-invert", specs, "must not depend on Playwright's own --grep filtering")
        # Recursive discovery (review finding: a flat `tests/*.spec.js` glob
        # silently skips a nested file, e.g. tests/auth/login.spec.js, even
        # though Playwright's own testDir walks subdirectories by default).
        self.assertIn("find tests -name '*.spec.js'", specs)
        # Tightened to the same line as a `test(` call (review finding: a bare
        # `grep -q '@quarantine'` over the whole file also matches the string
        # appearing in a comment or unrelated literal, mis-bucketing the file).
        self.assertIn("if ! grep -qE 'test\\(.*@quarantine' \"$f\"", specs)
        self.assertNotIn("continue-on-error: true", specs)
        self.assertIn('echo "::error::no non-quarantined spec files found under tests/**/*.spec.js"', specs)
        self.assertIn("exit 1", specs)

        quarantine = step_body(E2E_JOB, "Playwright quarantine specs")
        self.assertNotIn("--grep ", quarantine, "must not depend on Playwright's own --grep filtering")
        self.assertIn("find tests -name '*.spec.js'", quarantine)
        self.assertIn("if grep -qE 'test\\(.*@quarantine' \"$f\"", quarantine)
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

    def test_specs_and_quarantine_reseed_before_each_file_with_runner_env_set(self):
        # The job's shell reseeds via docker exec before each Playwright
        # invocation (never inside the test container), and every Playwright
        # invocation carries E2E_RESEED=runner so the app's own reseed.js /
        # capture script skip their now-impossible docker-exec reseed.
        for name in ("Playwright specs", "Playwright quarantine specs"):
            step = step_body(E2E_JOB, name)
            self.assertIn('docker exec "$E2E_APP_CONTAINER" python manage.py "${{ inputs.e2e-seed-command }}"', step)
            self.assertIn("-e E2E_RESEED=runner", step)
            self.assertIn("npx playwright test '$f'", step)

    def test_quarantine_skips_a_file_when_its_reseed_fails_rather_than_running_stale(self):
        # Same review finding as the capture loop: under `set +e`, a failed
        # reseed must not be silently ignored while the file still runs
        # against stale data.
        quarantine = step_body(E2E_JOB, "Playwright quarantine specs")
        self.assertIn("reseed_status=$?", quarantine)
        self.assertIn('if [ "$reseed_status" -ne 0 ]; then', quarantine)
        self.assertIn("continue", quarantine)

    def test_assertion_fails_if_quarantine_stops_skipping_on_reseed_failure(self):
        mutated = E2E_JOB.replace(
            'reseed_status=$?\n            if [ "$reseed_status" -ne 0 ]; then\n              echo "::warning::reseed failed before $f (quarantine) -- skipping this file rather than running it against stale data"\n              worst=$reseed_status\n              continue\n            fi\n',
            "",
            1,
        )
        self.assertNotEqual(mutated, E2E_JOB, "fixture setup did not match live quarantine reseed-failure handling")
        quarantine = step_body(mutated, "Playwright quarantine specs")
        self.assertNotIn("reseed_status=$?", quarantine)

    def test_assertion_fails_if_gating_specs_stop_failing_loudly_on_empty_bucket(self):
        mutated = E2E_JOB.replace(
            'if [ -z "$GATING_FILES" ]; then\n            echo "::error::no non-quarantined spec files found under tests/**/*.spec.js"\n            exit 1\n          fi\n',
            "",
            1,
        )
        self.assertNotEqual(mutated, E2E_JOB, "fixture setup did not match live empty-bucket guard")
        specs = step_body(mutated, "Playwright specs")
        self.assertNotIn('echo "::error::no non-quarantined spec files found under tests/**/*.spec.js"', specs)

    def test_assertion_fails_if_empty_quarantine_bucket_stops_exiting_cleanly(self):
        # Review finding: the gating step's empty-bucket mutation test only
        # pinned that its OWN loud-failure text disappears -- nothing pinned
        # that an empty QUARANTINE bucket keeps exiting 0 rather than, say,
        # starting to fail the step. Mutate the quarantine empty-check away
        # and confirm the guard's own success path (exit 0, notice text) is
        # what disappears.
        mutated = E2E_JOB.replace(
            'if [ -z "$QUARANTINE_FILES" ]; then\n            echo "::notice::Playwright quarantine specs: no file tagged @quarantine yet."\n            echo \'### Playwright quarantine specs: no tests matched (nothing tagged yet)\' >> "$GITHUB_STEP_SUMMARY"\n            exit 0\n          fi\n',
            "",
            1,
        )
        self.assertNotEqual(mutated, E2E_JOB, "fixture setup did not match live quarantine empty-bucket guard")
        quarantine = step_body(mutated, "Playwright quarantine specs")
        self.assertNotIn("no file tagged @quarantine yet", quarantine)

    def test_assertion_fails_if_reseed_is_dropped_from_the_specs_loop(self):
        mutated = E2E_JOB.replace(
            'echo "::notice::reseeding before $f"\n            docker exec "$E2E_APP_CONTAINER" python manage.py "${{ inputs.e2e-seed-command }}"\n',
            "",
            1,
        )
        self.assertNotEqual(mutated, E2E_JOB, "fixture setup did not match live reseed line in the specs loop")
        specs = step_body(mutated, "Playwright specs")
        self.assertNotIn('docker exec "$E2E_APP_CONTAINER" python manage.py "${{ inputs.e2e-seed-command }}"', specs)

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
            "        continue-on-error: true\n        working-directory: ${{ inputs.frontend-path }}\n        run: |\n          set +e\n          QUARANTINE_FILES",
            "        working-directory: ${{ inputs.frontend-path }}\n        run: |\n          set -euo pipefail\n          QUARANTINE_FILES",
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


def load_e2e_steps(workflow_text=CI_WORKFLOW):
    """Parsed with a real YAML loader (WFT-CI-32) -- WFT-CI-31 showed that
    string-slicing checks miss invalid YAML entirely; this also guarantees
    each step is read structurally (by its `name` key), never by a text
    boundary that could drift."""
    import yaml

    data = yaml.safe_load(workflow_text)
    return data["jobs"]["e2e"]["steps"]


def find_step(steps, name):
    for step in steps:
        if step.get("name") == name:
            return step
    raise AssertionError(f"step not found: {name}")


class WftCi32StructuralTests(unittest.TestCase):
    """WFT-CI-32: traces + error context survive a failing e2e spec."""

    def test_gating_and_quarantine_specs_run_with_trace_retain_on_failure(self):
        steps = load_e2e_steps()
        gating = find_step(steps, "Playwright specs")
        quarantine = find_step(steps, "Playwright quarantine specs")
        self.assertIn("--trace retain-on-failure", gating["run"])
        self.assertIn("--trace retain-on-failure", quarantine["run"])

    def test_assertion_fails_if_gating_trace_flag_is_dropped(self):
        mutated_text = CI_WORKFLOW.replace(
            "npx playwright test '$f' --trace retain-on-failure\"; then",
            "npx playwright test '$f'\"; then",
            1,
        )
        self.assertNotEqual(mutated_text, CI_WORKFLOW, "fixture setup did not match live gating invocation")
        gating = find_step(load_e2e_steps(mutated_text), "Playwright specs")
        self.assertNotIn("--trace retain-on-failure", gating["run"])

    def test_quarantine_isolates_each_invocations_output_directory(self):
        # Review-anticipated gap: the gating loop stops at the first failure
        # (only one invocation's output ever exists), but the quarantine loop
        # keeps iterating after a failure, and Playwright wipes test-results/
        # at the START of every invocation -- a second file's run would
        # otherwise silently erase the first file's trace.
        quarantine = find_step(load_e2e_steps(), "Playwright quarantine specs")["run"]
        self.assertIn(
            'OUTPUT_SUBDIR="test-results/$(printf \'%03d\' "$idx")-$(echo "$f" | tr \'/\' \'-\')"',
            quarantine,
        )
        self.assertIn("--output '$OUTPUT_SUBDIR'", quarantine)

    def test_quarantine_output_subdir_is_disambiguated_by_loop_position(self):
        # Review finding (independently raised by my own pass, the regression
        # lens, and the envelope lens): tr '/' '-' alone can collide two
        # distinct spec paths into the same subdirectory name (e.g.
        # "a/b.spec.js" and a literal "a-b.spec.js" both sanitize to
        # "a-b.spec.js"), silently overwriting one trace with another's. The
        # idx prefix (the loop position, zero-padded) makes that impossible.
        quarantine = find_step(load_e2e_steps(), "Playwright quarantine specs")["run"]
        self.assertIn("idx=0", quarantine)
        self.assertIn("idx=$((idx + 1))", quarantine)

    def test_assertion_fails_if_quarantine_output_subdir_loses_its_index_prefix(self):
        mutated_text = CI_WORKFLOW.replace(
            'OUTPUT_SUBDIR="test-results/$(printf \'%03d\' "$idx")-$(echo "$f" | tr \'/\' \'-\')"',
            'OUTPUT_SUBDIR="test-results/$(echo "$f" | tr \'/\' \'-\')"',
            1,
        )
        self.assertNotEqual(mutated_text, CI_WORKFLOW, "fixture setup did not match live OUTPUT_SUBDIR computation")
        quarantine = find_step(load_e2e_steps(mutated_text), "Playwright quarantine specs")["run"]
        self.assertNotIn("printf '%03d' \"$idx\"", quarantine)

    def test_assertion_fails_if_quarantine_output_isolation_is_dropped(self):
        mutated_text = CI_WORKFLOW.replace(
            "npx playwright test '$f' --trace retain-on-failure --output '$OUTPUT_SUBDIR'\"",
            "npx playwright test '$f' --trace retain-on-failure\"",
            1,
        )
        self.assertNotEqual(mutated_text, CI_WORKFLOW, "fixture setup did not match live quarantine invocation")
        quarantine = find_step(load_e2e_steps(mutated_text), "Playwright quarantine specs")
        self.assertNotIn("--output", quarantine["run"])

    def test_quarantine_reports_its_own_failure_as_a_step_output(self):
        # `failure()` alone cannot see a quarantine-only failure: this step's
        # own `continue-on-error: true` reports its conclusion as success
        # regardless of its internal exit code, so the upload step's `if:`
        # needs this step's OWN output instead.
        quarantine = find_step(load_e2e_steps(), "Playwright quarantine specs")
        self.assertIn('echo "had_failure=$([ "$worst" -ne 0 ] && echo true || echo false)" >> "$GITHUB_OUTPUT"', quarantine["run"])

    def test_assertion_fails_if_quarantine_stops_reporting_had_failure(self):
        mutated_text = CI_WORKFLOW.replace(
            '          echo "had_failure=$([ "$worst" -ne 0 ] && echo true || echo false)" >> "$GITHUB_OUTPUT"\n',
            "",
            1,
        )
        self.assertNotEqual(mutated_text, CI_WORKFLOW, "fixture setup did not match live had_failure output line")
        quarantine = find_step(load_e2e_steps(mutated_text), "Playwright quarantine specs")
        self.assertNotIn("had_failure", quarantine["run"])

    def test_upload_failure_artifact_step_is_correctly_wired(self):
        upload = find_step(load_e2e_steps(), "Upload E2E failure artifacts")
        self.assertEqual(
            upload["if"],
            "${{ always() && (failure() || steps.quarantine.outputs.had_failure == 'true') }}",
        )
        self.assertEqual(upload["with"]["name"], "e2e-failures-${{ github.run_id }}-${{ github.run_attempt }}")
        self.assertEqual(upload["with"]["path"], "${{ inputs.frontend-path }}/test-results")
        self.assertEqual(upload["with"]["retention-days"], 7)
        # `warn`, not `ignore`: a clean run never reaches this step (the `if:`
        # is false), so this only fires on an actual failure -- and a failure
        # with nothing to upload (sec-review finding: e.g. the container
        # crashed before Playwright could write anything) should say so, not
        # stay silent. Matches the existing capture-artifact step's own value.
        self.assertEqual(upload["with"]["if-no-files-found"], "warn")

    def test_had_failure_is_unset_on_the_early_exit_no_quarantine_files_path(self):
        # Tests-lens finding: the echo of `had_failure` must sit AFTER the
        # early-exit guard's own `exit 0` (no @quarantine-tagged files found
        # yet) -- otherwise that branch would never reach it, and the upload
        # step's `if:` would read an unset output. GitHub Actions treats an
        # unset step output as the empty string, so `== 'true'` is correctly
        # false there -- but only by virtue of this ordering, pinned here.
        quarantine_run = find_step(load_e2e_steps(), "Playwright quarantine specs")["run"]
        early_exit_marker = "no file tagged @quarantine yet"
        early_exit_pos = quarantine_run.index(early_exit_marker)
        early_exit_block_end = quarantine_run.index("exit 0", early_exit_pos)
        text_before_early_exit = quarantine_run[:early_exit_block_end]
        self.assertNotIn("had_failure", text_before_early_exit)

    def test_assertion_fails_if_had_failure_echo_moves_before_the_early_exit(self):
        marker = '            echo "::notice::Playwright quarantine specs: no file tagged @quarantine yet."\n'
        hoisted = (
            '            echo "had_failure=$([ "$worst" -ne 0 ] && echo true || echo false)" '
            '>> "$GITHUB_OUTPUT"\n' + marker
        )
        mutated_text = CI_WORKFLOW.replace(marker, hoisted, 1)
        self.assertNotEqual(mutated_text, CI_WORKFLOW, "fixture setup did not match live early-exit notice line")
        quarantine_run = find_step(load_e2e_steps(mutated_text), "Playwright quarantine specs")["run"]
        early_exit_marker = "no file tagged @quarantine yet"
        early_exit_pos = quarantine_run.index(early_exit_marker)
        early_exit_block_end = quarantine_run.index("exit 0", early_exit_pos)
        text_before_early_exit = quarantine_run[:early_exit_block_end]
        self.assertIn("had_failure", text_before_early_exit)

    def test_app_log_is_saved_into_the_same_artifact_on_a_failure(self):
        # Envelope amendment 2026-10-02: every diagnosis so far needed the
        # trace and the backend log of the same time window side by side.
        # E2E_APP_CONTAINER is a fresh per-run container (named with
        # run_id/run_attempt), so a plain `docker logs` with no --since
        # flag already covers exactly "since the job started" -- mirrors
        # the health-check-timeout path's own call to the same command.
        step = find_step(load_e2e_steps(), "Save app container log for failed specs")
        self.assertEqual(
            step["if"],
            "${{ always() && (failure() || steps.quarantine.outputs.had_failure == 'true') }}",
        )
        self.assertIn('docker logs "$E2E_APP_CONTAINER" > test-results/docker-logs-app.log', step["run"])

    def test_assertion_fails_if_app_log_capture_is_dropped(self):
        mutated_text = CI_WORKFLOW.replace(
            '          docker logs "$E2E_APP_CONTAINER" > test-results/docker-logs-app.log 2>&1 || true\n',
            "",
            1,
        )
        self.assertNotEqual(mutated_text, CI_WORKFLOW, "fixture setup did not match live app-log capture line")
        step = find_step(load_e2e_steps(mutated_text), "Save app container log for failed specs")
        self.assertNotIn("docker logs", step["run"])

    def test_app_log_step_runs_before_cleanup_removes_the_container(self):
        steps = load_e2e_steps()
        names = [s.get("name") for s in steps]
        self.assertLess(
            names.index("Playwright quarantine specs"),
            names.index("Save app container log for failed specs"),
        )
        self.assertLess(
            names.index("Save app container log for failed specs"),
            names.index("Upload E2E failure artifacts"),
        )
        self.assertLess(
            names.index("Upload E2E failure artifacts"),
            names.index("Cleanup E2E containers, network, and image"),
        )

    def test_assertion_fails_if_upload_step_loses_its_conditional(self):
        mutated_text = CI_WORKFLOW.replace(
            "        if: ${{ always() && (failure() || steps.quarantine.outputs.had_failure == 'true') }}\n        uses: actions/upload-artifact@65c4c4a1ddee5b72f698fdd19549f0f0fb45cf08  # v4.6.0\n        with:\n          name: e2e-failures-${{ github.run_id }}-${{ github.run_attempt }}",
            "        uses: actions/upload-artifact@65c4c4a1ddee5b72f698fdd19549f0f0fb45cf08  # v4.6.0\n        with:\n          name: e2e-failures-${{ github.run_id }}-${{ github.run_attempt }}",
            1,
        )
        self.assertNotEqual(mutated_text, CI_WORKFLOW, "fixture setup did not match live upload step header")
        upload = find_step(load_e2e_steps(mutated_text), "Upload E2E failure artifacts")
        self.assertNotIn("if", upload)

    def test_upload_step_is_positioned_after_quarantine_before_capture(self):
        steps = load_e2e_steps()
        names = [s.get("name") for s in steps]
        self.assertLess(
            names.index("Playwright quarantine specs"),
            names.index("Upload E2E failure artifacts"),
        )
        self.assertLess(
            names.index("Upload E2E failure artifacts"),
            names.index("Capture E2E screens and report"),
        )

    def test_job_summary_names_the_artifact_next_to_the_failed_spec(self):
        steps = load_e2e_steps()
        gating = find_step(steps, "Playwright specs")["run"]
        quarantine = find_step(steps, "Playwright quarantine specs")["run"]
        self.assertIn("Trace + error context: artifact", gating)
        self.assertIn("e2e-failures-${{ github.run_id }}-${{ github.run_attempt }}", gating)
        self.assertIn("Trace + error context: artifact", quarantine)
        self.assertIn("e2e-failures-${{ github.run_id }}-${{ github.run_attempt }}", quarantine)


if __name__ == "__main__":
    unittest.main()
