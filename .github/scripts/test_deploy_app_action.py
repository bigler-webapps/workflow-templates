"""Structural regression tests for the immutable-image deploy path.

Run with: python .github/scripts/test_deploy_app_action.py
"""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = (ROOT / ".github/workflows/app-ci.yml").read_text(encoding="utf-8")
DEPLOY_ACTION = (ROOT / ".github/actions/deploy-app/action.yml").read_text(encoding="utf-8")
DEPLOY_HELPER = (ROOT / ".github/scripts/guarded_deploy.py").read_text(encoding="utf-8")
PUBLISH_ACTION = (ROOT / ".github/actions/publish-backend-image/action.yml").read_text(encoding="utf-8")


class CI9StructuralTests(unittest.TestCase):
    """CI-9 put the publish into app-ci.yml on the assumption CI runs before
    the deploy; CI-11 disproved that and moved the real publish call into
    each app's own main.yml, paired with its own resolve_sha (untested here,
    each app owns its copy). WFT-CI-25 finally deleted the now-redundant
    app-ci.yml half. What stays worth asserting from this repo's side is the
    shared composite (PUBLISH_ACTION/DEPLOY_ACTION) both still use, plus that
    app-ci.yml (CI_WORKFLOW) never grows a second, direct implementation."""

    def test_publish_action_uses_the_run_token_and_preserves_the_empty_tag_guard(self):
        """The publish workflow relies on the run token, never a stored PAT."""
        self.assertIn("printf '%s' \"$GITHUB_TOKEN\" | docker login ghcr.io", PUBLISH_ACTION)
        self.assertIn('[ -n "$IMAGE_TAG" ] || { echo "❌ IMAGE_TAG is empty; refusing to publish.";', PUBLISH_ACTION)
        self.assertNotIn("GHCR_TOKEN", CI_WORKFLOW)
        self.assertNotIn("GHCR_TOKEN", PUBLISH_ACTION)

    def test_published_images_have_an_oci_source_label(self):
        """Both the normal build and CI-6 full-image rebuild are linked to the repo."""
        source_label = "--label org.opencontainers.image.source=https://github.com/${{ github.repository }}"
        self.assertEqual(CI_WORKFLOW.count(source_label), 2)
        self.assertEqual(PUBLISH_ACTION.count(source_label), 1)

    def test_deploy_action_keeps_registry_and_build_secrets_out_of_env(self):
        """The generated server .env excludes the run token and pull-only build secrets."""
        self.assertNotIn("GHCR_TOKEN", DEPLOY_ACTION)
        self.assertIn('test("^(SSH_|B2_|RESTIC_|GITHUB_|TS_)")', DEPLOY_ACTION)
        self.assertIn("del(.HRAM_ENGINE_READ_TOKEN, .VITE_APP_MUI_LICENSE_KEY)", DEPLOY_ACTION)

    def test_deploy_action_never_reads_secrets_context_directly(self):
        """A composite action has no `secrets` context -- every credential must
        arrive as a declared input, forwarded by the caller's `with:` block
        (CI-10 regression: an earlier draft referenced `secrets.GITHUB_TOKEN`
        directly inside this composite action, which is invalid GitHub Actions
        syntax and fails the whole action invocation, not just the login step)."""
        self.assertNotIn("${{ secrets.", DEPLOY_ACTION)
        self.assertIn("github_token:", DEPLOY_ACTION)
        self.assertIn("GITHUB_TOKEN: ${{ inputs.github_token }}", DEPLOY_ACTION)

    def test_image_tags_come_from_checkout_head_never_the_triggering_ref(self):
        """Each app's own main.yml resolves its own SHA (untested here); what
        this repo owns is that the shared composite/deploy action never falls
        back to `github.sha` (wrong on a merge-commit trigger) if a caller's
        resolution step is ever missing or misordered."""
        self.assertNotIn("github.sha", CI_WORKFLOW)
        self.assertNotIn("github.sha", DEPLOY_ACTION)
        self.assertNotIn("github.sha", PUBLISH_ACTION)
        self.assertIn("image_tag:", DEPLOY_ACTION)
        self.assertIn("required: true", DEPLOY_ACTION.split("image_tag:", 1)[1].split("environment:", 1)[0])
        self.assertEqual(DEPLOY_ACTION.count("IMAGE_TAG: ${{ inputs.image_tag }}"), 2)
        self.assertIn("printf '\\nIMAGE_TAG=%s\\n' \"$IMAGE_TAG\" >> \"$ENV_FILE\"", DEPLOY_ACTION)
        self.assertIn('docker push "$PUBLISHED_IMAGE:$IMAGE_TAG"', PUBLISH_ACTION)
        self.assertNotIn('docker push "$PUBLISHED_IMAGE:latest"', PUBLISH_ACTION)

    def test_app_ci_yml_never_grows_its_own_direct_publish(self):
        """WFT-CI-25: app-ci.yml stopped calling the shared composite at all
        (CI-11 moved the real publish to each app's main.yml) -- it must not
        quietly regain one, composite-wrapped or a raw `docker push` of any
        form (reviewer-caught: the first version of this test only matched
        the one exact removed string, so a differently-worded reintroduced
        push -- e.g. `docker push ghcr.io/...:${GITHUB_SHA}` -- would have
        passed unnoticed)."""
        self.assertNotIn("publish-backend-image", CI_WORKFLOW)
        self.assertNotIn("publish_backend_image", CI_WORKFLOW)
        self.assertNotIn("docker push", CI_WORKFLOW)
        self.assertIn("outputs:\n  image:", PUBLISH_ACTION)
        self.assertIn('printf \'image=%s:%s\\n\' "$PUBLISHED_IMAGE" "$IMAGE_TAG" >> "$GITHUB_OUTPUT"', PUBLISH_ACTION)

    def test_assertion_fails_on_any_reintroduced_docker_push(self):
        """Mutation check (reviewer-caught): prove the broadened guard above
        actually catches a push shaped differently from the exact original
        string, not just a verbatim copy of it."""
        mutated = CI_WORKFLOW + '\n      - run: docker push ghcr.io/bigler-webapps/example-backend:${{ github.sha }}\n'
        self.assertNotEqual(mutated, CI_WORKFLOW, "fixture setup: append did not change the workflow text")
        self.assertIn("docker push", mutated)

    def test_default_deploy_pulls_and_fallback_keeps_build(self):
        """The fixed deploy adapter preserves pull and explicit build paths."""
        self.assertIn("use_build_fallback:", DEPLOY_ACTION)
        self.assertIn("default: 'false'", DEPLOY_ACTION)
        self.assertIn('if fallback == "true":', DEPLOY_HELPER)
        self.assertIn('[*compose, "build"]', DEPLOY_HELPER)
        self.assertIn('[*compose, "create", "--force-recreate"]', DEPLOY_HELPER)
        self.assertIn('[*compose, "pull"]', DEPLOY_HELPER)
        self.assertIn('[*compose, "up", "-d", "--force-recreate", "--remove-orphans"]', DEPLOY_HELPER)

    def test_empty_tag_cannot_fall_back_to_compose_latest(self):
        """deploy-app/action.yml fails before any remote pull/build if the tag is absent."""
        self.assertGreaterEqual(DEPLOY_ACTION.count('set -euo pipefail'), 3)
        self.assertIn('[ -n "$IMAGE_TAG" ] || { echo "IMAGE_TAG is empty;', DEPLOY_ACTION)
        self.assertIn('if not image_tag:', DEPLOY_HELPER)

    def test_publish_uses_a_temporary_docker_config_before_docker_commands(self):
        """Runner-side build, login, and push cannot read or write the shared store."""
        config_export = 'export DOCKER_CONFIG="$DOCKER_CONFIG_DIR"'
        cleanup = 'trap \'rm -rf "$DOCKER_CONFIG_DIR"\' EXIT'
        self.assertIn('DOCKER_CONFIG_DIR="$(mktemp -d)"', PUBLISH_ACTION)
        self.assertIn(config_export, PUBLISH_ACTION)
        self.assertIn(cleanup, PUBLISH_ACTION)
        self.assertLess(PUBLISH_ACTION.index(config_export), PUBLISH_ACTION.index('docker build'))
        self.assertLess(PUBLISH_ACTION.index(config_export), PUBLISH_ACTION.index('docker login'))

    def test_remote_deploy_uses_a_temporary_docker_config_before_login(self):
        """The action ships the fixed adapter; it owns a temporary Docker store."""
        self.assertIn('DEPLOY_HELPER_PATH: ${{ github.action_path }}/../../scripts/guarded_deploy.py', DEPLOY_ACTION)
        self.assertIn('with tempfile.TemporaryDirectory(prefix="deploy-docker-config-") as docker_config:', DEPLOY_HELPER)
        self.assertIn('child_env["DOCKER_CONFIG"] = docker_config', DEPLOY_HELPER)
        self.assertLess(DEPLOY_HELPER.index('child_env["DOCKER_CONFIG"] = docker_config'), DEPLOY_HELPER.index('"login", "ghcr.io"'))

    def test_deploy_requires_the_authorized_docker_identity(self):
        """A sudo re-exec could close the inherited recovery descriptor."""
        self.assertIn('raise DeployError("the authorized deployment identity cannot reach Docker")', DEPLOY_HELPER)
        self.assertNotIn('"sudo",', DEPLOY_HELPER)

    def test_remote_deploy_runs_the_fixed_operation_under_the_installed_contract(self):
        self.assertIn('recovery.writer_guard(compose_project) as guard', DEPLOY_HELPER)
        self.assertIn('compose_project = f"{project}_{env[\'TARGET_ENV\']}"', DEPLOY_HELPER)
        self.assertIn('pass_fds=(guard.fd,)', DEPLOY_HELPER)
        self.assertIn('stack_recovery', DEPLOY_HELPER)
        self.assertNotIn('flock -x', DEPLOY_ACTION)


# --- WFT-CI-42: keep generated runtime env files off the runner workspace -----


import os
import shutil
import stat
import subprocess
import tempfile


WFT_CI42_ACTIONS = {
    "deploy-app": ROOT / ".github/actions/deploy-app/action.yml",
    "deploy-traefik": ROOT / ".github/actions/deploy-traefik/action.yml",
}
WFT_CI42_BASH = shutil.which("bash")
WFT_CI42_GENERATE_STUB = r"""#!/usr/bin/env bash
set -euo pipefail
output=.env
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--output" ]; then output="$2"; shift 2; else shift; fi
done
printf 'SECRET=value\n' > "$output"
"""
WFT_CI42_JQ_STUB = r"""#!/usr/bin/env bash
cat
"""
WFT_CI42_RSYNC_STUB = r"""#!/usr/bin/env bash
printf '%s\n' "$@" > "$RSYNC_LOG"
"""
WFT_CI42_SSH_STUB = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" > "$SSH_COMMAND_LOG"
cat > "$SSH_STDIN_LOG"
[ "${SSH_FAIL:-0}" != 1 ]
"""


def _wft_ci42_step(action_text, step_name):
    """Return a real composite step's run body and full YAML section."""
    match = re.search(
        r"(?ms)^    - name: " + re.escape(step_name) + r"\n(?P<section>.*?)(?=^    - name: |\Z)",
        action_text,
    )
    if not match:
        raise AssertionError(f"step not found: {step_name}")
    section = match.group("section")
    run = re.search(r"(?ms)^      run: \|\n(?P<body>(?:^        .*\n|^\s*\n)*)", section)
    if not run:
        raise AssertionError(f"run body not found for: {step_name}")
    body = "\n".join(line[8:] if line.startswith("        ") else "" for line in run.group("body").splitlines())
    return body, match.group(0)


class WftCi42RuntimeEnvTests(unittest.TestCase):
    """Run the actual generate, rsync, transfer, and cleanup step scripts."""

    def setUp(self):
        if not WFT_CI42_BASH:
            self.skipTest("no bash available on PATH")
        self.tmp = Path(tempfile.mkdtemp(prefix="wft-ci42-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _action(self, action_key):
        return WFT_CI42_ACTIONS[action_key].read_text(encoding="utf-8")

    def _write_stub(self, bin_dir, name, content):
        path = bin_dir / name
        path.write_text(content, encoding="utf-8", newline="\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return path

    def _prepare(self, action_key):
        action_text = self._action(action_key)
        workspace = self.tmp / f"{action_key}-workspace"
        runner_temp = self.tmp / f"{action_key}-runner-temp"
        bin_dir = self.tmp / f"{action_key}-bin"
        workspace.mkdir()
        runner_temp.mkdir()
        bin_dir.mkdir()
        self._write_stub(bin_dir, "generate-env", WFT_CI42_GENERATE_STUB)
        self._write_stub(bin_dir, "jq", WFT_CI42_JQ_STUB)
        self._write_stub(bin_dir, "rsync", WFT_CI42_RSYNC_STUB)
        self._write_stub(bin_dir, "ssh", WFT_CI42_SSH_STUB)
        github_env = self.tmp / f"{action_key}-github-env"
        github_env.write_text("", encoding="utf-8")
        env = os.environ.copy()
        env.update({
            "PATH": str(bin_dir) + os.pathsep + env.get("PATH", ""),
            "RUNNER_TEMP": runner_temp.as_posix(),
            "GITHUB_ENV": str(github_env),
            "TARGET_ENV": "staging",
            "TARGET_ENVIRONMENT": "staging",
            "IMAGE_TAG": "test-tag",
            "USE_BUILD_FALLBACK": "false",
            "SECRETS_CONTEXT_RAW": "{}",
            "SSH_USER": "deploy",
            "TAILNET_HOST": "server.test",
            "SSH_HOST": "server.test",
            "REMOTE_PATH": "/srv/apps/example",
            "PERSISTENT_DIRS": "media uploads",
            "RSYNC_LOG": str(self.tmp / f"{action_key}-rsync.log"),
            "SSH_COMMAND_LOG": str(self.tmp / f"{action_key}-ssh-command.log"),
            "SSH_STDIN_LOG": str(self.tmp / f"{action_key}-ssh-stdin.log"),
        })
        if action_key == "deploy-app":
            generate_name = "🔐 Generate .env file"
            rsync_name = "🚀 Rsync project to server (via plain ssh over Tailnet)"
        else:
            generate_name = "🔐 Generate .env"
            rsync_name = "📂 Rsync project to server"
        generate_body, _ = _wft_ci42_step(action_text, generate_name)
        proc = subprocess.run(
            [WFT_CI42_BASH, "-c", generate_body], cwd=workspace, env=env,
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse((workspace / ".env").exists())
        variables = dict(
            line.split("=", 1) for line in github_env.read_text(encoding="utf-8").splitlines() if "=" in line
        )
        env_dir = Path(variables["DEPLOY_ENV_DIR"])
        env_file = env_dir / ".env"
        self.assertTrue(env_file.is_file())
        # The content comes from the (stubbed) generate-env CLI plus the appended tag where the action appends one.
        self.assertIn("SECRET=value", env_file.read_text(encoding="utf-8"))
        if action_key == "deploy-app":
            self.assertIn("IMAGE_TAG=test-tag", env_file.read_text(encoding="utf-8"))
        # POSIX runners expose file mode bits. Windows chmod/stat emulation
        # reports 0666 for ordinary files regardless of the bash umask.
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(env_dir.stat().st_mode), 0o700)
        env["DEPLOY_ENV_DIR"] = env_dir.as_posix()
        return action_text, workspace, runner_temp, env_file, env, rsync_name

    def _run_step(self, action_text, step_name, workspace, env):
        body, _ = _wft_ci42_step(action_text, step_name)
        return subprocess.run(
            [WFT_CI42_BASH, "-c", body], cwd=workspace, env=env,
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )

    def test_deploy_app_generation_exclusion_transfer_and_cleanup(self):
        self._exercise_action("deploy-app")

    def test_deploy_traefik_generation_exclusion_transfer_and_cleanup(self):
        self._exercise_action("deploy-traefik")

    def test_deploy_app_remote_command_is_atomic_unique_and_cleans_up(self):
        self._exercise_remote_command("deploy-app")

    def test_deploy_traefik_remote_command_is_atomic_unique_and_cleans_up(self):
        self._exercise_remote_command("deploy-traefik")

    def _exercise_remote_command(self, action_key):
        """Execute the real remote command string locally (an ssh stub that runs it under bash)
        against a temporary 'server' directory."""
        action_text, workspace, _runner_temp, env_file, env, _rsync_name = self._prepare(action_key)
        bin_dir = self.tmp / f"{action_key}-bin"
        self._write_stub(bin_dir, "ssh", '#!/usr/bin/env bash\ncmd="${@: -1}"\nexec bash -c "$cmd"\n')
        self._write_stub(
            bin_dir, "chmod",
            '#!/usr/bin/env bash\n[ "${CHMOD_FAIL:-0}" = 1 ] && exit 1\n'
            'exec "$(PATH=/usr/bin:/bin command -v chmod)" "$@"\n',
        )
        server = self.tmp / f"{action_key}-server dir"  # a space: the path must survive quoting
        server.mkdir()
        (server / ".env").write_text("PREVIOUS=preserved\n", encoding="utf-8")
        env["REMOTE_PATH"] = server.as_posix()
        name = "🔐 Transfer runtime .env to server"

        failed_env = dict(env, CHMOD_FAIL="1")
        failed = self._run_step(action_text, name, workspace, failed_env)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual((server / ".env").read_text(encoding="utf-8"), "PREVIOUS=preserved\n")
        self.assertEqual(sorted(p.name for p in server.iterdir()), [".env"], "a failed transfer must leave no temp file")

        ok = self._run_step(action_text, name, workspace, env)
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertEqual((server / ".env").read_bytes(), env_file.read_bytes())
        self.assertEqual(sorted(p.name for p in server.iterdir()), [".env"], "no shared or leftover temp name")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE((server / ".env").stat().st_mode), 0o600)

    def _exercise_action(self, action_key):
        action_text, workspace, runner_temp, env_file, env, rsync_name = self._prepare(action_key)
        rsync = self._run_step(action_text, rsync_name, workspace, env)
        self.assertEqual(rsync.returncode, 0, rsync.stdout + rsync.stderr)
        rsync_args = Path(env["RSYNC_LOG"]).read_text(encoding="utf-8").splitlines()
        self.assertTrue("--exclude=/.env" in rsync_args or "/.env" in rsync_args, rsync_args)

        transfer_name = "🔐 Transfer runtime .env to server"
        cleanup_name = "🧹 Remove runner runtime .env"
        remote_existing = self.tmp / f"{action_key}-remote-env"
        remote_existing.write_text("PREVIOUS=preserved\n", encoding="utf-8")

        # Success sends the exact generated file, then the cleanup step removes it.
        succeeded = self._run_step(action_text, transfer_name, workspace, env)
        self.assertEqual(succeeded.returncode, 0, succeeded.stdout + succeeded.stderr)
        self.assertEqual(Path(env["SSH_STDIN_LOG"]).read_bytes(), env_file.read_bytes())
        cleanup, cleanup_section = _wft_ci42_step(action_text, cleanup_name)
        self.assertIn("if: ${{ always() }}", cleanup_section)
        self.assertEqual(cleanup_section.rstrip("\n"), action_text[action_text.rfind("    - name: "):].rstrip("\n"))
        successful_cleanup = self._run_step(action_text, cleanup_name, workspace, env)
        self.assertEqual(successful_cleanup.returncode, 0, successful_cleanup.stdout + successful_cleanup.stderr)
        self.assertFalse(env_file.parent.exists())

        # A later transfer failure is propagated; its previous remote file remains
        # untouched, and the always cleanup still removes the new runner copy.
        failed_dir = Path(tempfile.mkdtemp(prefix="deploy-env.", dir=runner_temp))
        failed_file = failed_dir / ".env"
        failed_file.write_text("SECRET=forced-failure\n", encoding="utf-8")
        failed_file.chmod(0o600)
        env["DEPLOY_ENV_DIR"] = failed_dir.as_posix()
        env["SSH_FAIL"] = "1"
        failed = self._run_step(action_text, transfer_name, workspace, env)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(remote_existing.read_text(encoding="utf-8"), "PREVIOUS=preserved\n")
        self.assertEqual(Path(env["SSH_STDIN_LOG"]).read_bytes(), failed_file.read_bytes())
        command = Path(env["SSH_COMMAND_LOG"]).read_text(encoding="utf-8")
        self.assertIn("umask 077", command)
        self.assertIn("chmod 600", command)
        self.assertIn("mv -f", command)
        failed_cleanup = self._run_step(action_text, cleanup_name, workspace, env)
        self.assertEqual(failed_cleanup.returncode, 0, failed_cleanup.stdout + failed_cleanup.stderr)
        self.assertFalse(failed_dir.exists())

        # Unset paths are harmless; paths outside RUNNER_TEMP are preserved.
        unset_env = env.copy()
        unset_env.pop("DEPLOY_ENV_DIR", None)
        unset_cleanup = subprocess.run(
            [WFT_CI42_BASH, "-c", cleanup], cwd=workspace, env=unset_env,
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        self.assertEqual(unset_cleanup.returncode, 0, unset_cleanup.stdout + unset_cleanup.stderr)
        outside = self.tmp / f"{action_key}-outside"
        outside.mkdir()
        outside_env = env.copy()
        outside_env["DEPLOY_ENV_DIR"] = outside.as_posix()
        outside_cleanup = subprocess.run(
            [WFT_CI42_BASH, "-c", cleanup], cwd=workspace, env=outside_env,
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        self.assertEqual(outside_cleanup.returncode, 0, outside_cleanup.stdout + outside_cleanup.stderr)
        self.assertTrue(outside.exists())

    def test_cleanup_is_the_last_step_and_runs_always_for_both_actions(self):
        for action_key in WFT_CI42_ACTIONS:
            with self.subTest(action=action_key):
                text = self._action(action_key)
                _, section = _wft_ci42_step(text, "🧹 Remove runner runtime .env")
                self.assertIn("if: ${{ always() }}", section)
                self.assertEqual(section.rstrip("\n"), text[text.rfind("    - name: "):].rstrip("\n"))


if __name__ == "__main__":
    unittest.main()


# Deploy behavior moved into guarded_deploy.py; its executable regression tests live in test_stack_recovery_callers.py.
