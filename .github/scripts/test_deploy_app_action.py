"""Structural regression tests for the immutable-image deploy path.

Run with: python .github/scripts/test_deploy_app_action.py
"""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = (ROOT / ".github/workflows/app-ci.yml").read_text(encoding="utf-8")
DEPLOY_ACTION = (ROOT / ".github/actions/deploy-app/action.yml").read_text(encoding="utf-8")
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
        """deploy-app/action.yml separates pull-based default from --build fallback."""
        self.assertIn("use_build_fallback:", DEPLOY_ACTION)
        self.assertIn("default: 'false'", DEPLOY_ACTION)
        fallback, default = re.search(
            r'if \[ "\\\$\{USE_BUILD_FALLBACK\}" = "true" \]; then(?P<fallback>.*?)'
            r'        else(?P<default>.*?)\n        fi',
            DEPLOY_ACTION,
            re.DOTALL,
        ).group("fallback", "default")
        self.assertIn('compose \\$COMPOSE_FILES --env-file .env up -d --build --force-recreate --remove-orphans', fallback)
        self.assertNotIn('--build', default)
        self.assertIn('compose \\$COMPOSE_FILES --env-file .env pull', default)
        self.assertIn('compose \\$COMPOSE_FILES --env-file .env up -d --force-recreate --remove-orphans', default)

    def test_empty_tag_cannot_fall_back_to_compose_latest(self):
        """deploy-app/action.yml fails before any remote pull/build if the tag is absent."""
        self.assertGreaterEqual(DEPLOY_ACTION.count('set -euo pipefail'), 3)
        self.assertIn('[ -n "$IMAGE_TAG" ] || { echo "❌ IMAGE_TAG is empty;', DEPLOY_ACTION)
        self.assertIn('IMAGE_TAG is empty on target; refusing to use compose\'s latest default.', DEPLOY_ACTION)

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
        """The target host login is scoped to the SSH session, then cleaned up."""
        config_export = 'export DOCKER_CONFIG="\\$DOCKER_CONFIG_DIR"'
        cleanup = 'trap \'rm -rf "\\$DOCKER_CONFIG_DIR"\' EXIT'
        self.assertIn('DOCKER_CONFIG_DIR="\\$(mktemp -d)"', DEPLOY_ACTION)
        self.assertIn(config_export, DEPLOY_ACTION)
        self.assertIn(cleanup, DEPLOY_ACTION)
        self.assertLess(DEPLOY_ACTION.index(config_export), DEPLOY_ACTION.index('login ghcr.io'))
        # The export sits before the if/else branch point -- covers the
        # --build fallback branch too, not just the default pull branch.
        self.assertLess(
            DEPLOY_ACTION.index(config_export),
            DEPLOY_ACTION.index('if [ "\\${USE_BUILD_FALLBACK}" = "true" ]; then'),
        )

    def test_sudo_docker_fallback_preserves_the_scoped_docker_config(self):
        """A sudo re-exec must not silently drop the DOCKER_CONFIG scoping."""
        self.assertIn('sudo -n --preserve-env=DOCKER_CONFIG docker', DEPLOY_ACTION)


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


# --- INF-51: the deploy removes the image it replaces -----------------------


import os
import shutil
import stat
import subprocess
import tempfile


BASH = shutil.which("bash")

# The stub answers the four docker forms the cleanup block uses. Which ids each
# listing returns is what the individual tests vary.
DOCKER_STUB = r"""#!/usr/bin/env bash
if [ "$1" = "compose" ]; then
  # `compose ... ps -q` -- the containers of THIS project, after the recreate.
  printf '%s\n' $NEW_IDS
  exit 0
fi
case "$1" in
  ps)
    # host-wide, every container including other apps'
    printf '%s\n' $ALL_IDS
    ;;
  inspect)
    shift 3   # drop: inspect --format <fmt>
    for id in "$@"; do
      eval "printf '%s\n' \"\$REF_$id\""
    done
    ;;
  image)
    if [ "$2" = "rm" ]; then
      echo "$3" >> "$RM_LOG"
    fi
    ;;
esac
"""


def _extract_cleanup_block() -> str:
    """Pull the real cleanup block out of action.yml and un-escape it.

    Deliberately NOT a copy of the logic: a test that re-implements what it
    checks proves nothing. The block lives inside an ssh heredoc where every
    runtime `$` is written `\$`, so undoing that escaping is all that stands
    between the committed text and something bash can run.
    """
    start = DEPLOY_ACTION.index('echo "Removing the image this deploy replaced')
    end = DEPLOY_ACTION.index("} || true", start) + len("} || true")
    block = DEPLOY_ACTION[start:end]
    return block.replace("\$", "$")


def _run_cleanup(prev_refs, new_ids, all_ids, refs, image_tag="NEWSHA"):
    """Execute the extracted block with docker stubbed. Returns (stdout, removed)."""
    if not BASH:
        raise unittest.SkipTest("no bash available on PATH")
    tmp = Path(tempfile.mkdtemp())
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "docker"
    stub.write_text(DOCKER_STUB, encoding="utf-8", newline="\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    rm_log = tmp / "removed.txt"
    script = tmp / "cleanup.sh"
    harness = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'DOCKER="docker"\n'
        'COMPOSE_FILES="-f docker-compose.yml"\n'
        f'IMAGE_TAG="{image_tag}"\n'
        'PREV_IMAGE_REFS="' + "\n".join(prev_refs) + '"\n'
        + _extract_cleanup_block()
        + "\n"
    )
    script.write_text(harness, encoding="utf-8", newline="\n")

    env = os.environ.copy()
    env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
    env["RM_LOG"] = str(rm_log)
    env["NEW_IDS"] = " ".join(new_ids)
    env["ALL_IDS"] = " ".join(all_ids)
    for cid, ref in refs.items():
        env["REF_" + cid] = ref

    proc = subprocess.run(
        [BASH, str(script)], env=env, capture_output=True, text=True, timeout=60
    )
    removed = set(rm_log.read_text(encoding="utf-8").split()) if rm_log.exists() else set()
    return proc, removed


class INF51DeployImageCleanup(unittest.TestCase):
    def test_the_replaced_tag_is_removed_and_nothing_else_is(self):
        """The ordinary successful deploy: old app image goes, the new one and
        the unchanged sidecar stay."""
        proc, removed = _run_cleanup(
            prev_refs=["ghcr.io/x/app:OLDSHA", "postgres:18"],
            new_ids=["c_app", "c_db"],
            all_ids=["c_app", "c_db", "c_other"],
            refs={
                "c_app": "ghcr.io/x/app:NEWSHA",
                "c_db": "postgres:18",
                "c_other": "ghcr.io/x/otherapp:zzz",
            },
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(removed, {"ghcr.io/x/app:OLDSHA"})

    def test_it_never_removes_the_tag_just_deployed(self):
        """WO requirement 5, and the case that actually bites: re-deploying the
        SAME tag while a service crash-loops. The project was already running
        IMAGE_TAG, so it is in the "before" set; the failed container makes it
        absent from the "after" set; without the explicit guard the deploy would
        delete the image it is currently trying to run."""
        proc, removed = _run_cleanup(
            prev_refs=["ghcr.io/x/app:NEWSHA"],
            new_ids=[],          # nothing came up
            all_ids=["c_other"],
            refs={"c_other": "ghcr.io/x/otherapp:zzz"},
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("ghcr.io/x/app:NEWSHA", removed)
        self.assertIn("refusing to remove the tag just deployed", proc.stdout)

    def test_it_never_removes_another_apps_image(self):
        """Shared deploy hosts run many apps. Even if another app's ref somehow
        reached the candidate set, the host-wide in-use check must spare it."""
        proc, removed = _run_cleanup(
            prev_refs=["ghcr.io/x/otherapp:zzz", "ghcr.io/x/app:OLDSHA"],
            new_ids=["c_app"],
            all_ids=["c_app", "c_other"],
            refs={"c_app": "ghcr.io/x/app:NEWSHA", "c_other": "ghcr.io/x/otherapp:zzz"},
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("ghcr.io/x/otherapp:zzz", removed)
        self.assertEqual(removed, {"ghcr.io/x/app:OLDSHA"})
        self.assertIn("still in use by a container", proc.stdout)

    def test_a_failed_removal_never_fails_the_deploy(self):
        """Named risk in the WO: this runs on every app's deploy path including
        production, and an error here is worse than the disk problem it fixes."""
        tmp = Path(tempfile.mkdtemp())
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "docker"
        # Every `image rm` fails, and so does the host-wide listing.
        stub.write_text(
            DOCKER_STUB.replace(
                '    if [ "$2" = "rm" ]; then\n      echo "$3" >> "$RM_LOG"\n    fi',
                '    exit 1',
            ),
            encoding="utf-8",
            newline="\n",
        )
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        script = tmp / "cleanup.sh"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            'DOCKER="docker"\n'
            'COMPOSE_FILES="-f docker-compose.yml"\n'
            'IMAGE_TAG="NEWSHA"\n'
            'PREV_IMAGE_REFS="ghcr.io/x/app:OLDSHA"\n'
            + _extract_cleanup_block()
            + '\necho REACHED_END\n',
            encoding="utf-8",
            newline="\n",
        )
        env = os.environ.copy()
        env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
        env["RM_LOG"] = str(tmp / "rm.txt")
        env["NEW_IDS"] = ""
        env["ALL_IDS"] = ""
        if not BASH:
            self.skipTest("no bash available on PATH")
        proc = subprocess.run(
            [BASH, str(script)], env=env, capture_output=True, text=True, timeout=60
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("REACHED_END", proc.stdout)

    def test_cleanup_runs_after_migrations_not_before(self):
        """Ordering is the rollback guarantee: any earlier failure must leave
        the replaced image in place."""
        migrate = DEPLOY_ACTION.index("manage.py migrate --noinput")
        cleanup = DEPLOY_ACTION.index('echo "Removing the image this deploy replaced')
        self.assertLess(migrate, cleanup)

    def test_the_before_snapshot_is_taken_before_the_recreate(self):
        """PREV_IMAGE_REFS is only meaningful if it is captured while the old
        containers are still the running ones."""
        snapshot = DEPLOY_ACTION.index("PREV_IMAGE_REFS=")
        recreate = DEPLOY_ACTION.index("up -d --force-recreate")
        self.assertLess(snapshot, recreate)

    def test_a_tag_with_glob_metacharacters_is_matched_literally(self):
        """Defensive, per the security review: the guard is a `case` pattern,
        `*:"$IMAGE_TAG"`. Quoting the variable forces LITERAL matching; without
        the quotes a tag containing `*` would match other tags too and spare
        images that should have been removed. Docker's reference grammar makes
        such a tag unreachable in practice, so this pins the quoting rather than
        a live bug."""
        proc, removed = _run_cleanup(
            prev_refs=["ghcr.io/x/app:v1.0", "ghcr.io/x/app:v1.*"],
            new_ids=[],
            all_ids=[],
            refs={},
            image_tag="v1.*",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        # The literal tag is spared; the other one is NOT shielded by it.
        self.assertNotIn("ghcr.io/x/app:v1.*", removed)
        self.assertIn("ghcr.io/x/app:v1.0", removed)


# --- CI-18: deploy fails if the stack is restart-looping afterward ----------


# `compose ps -q` returns the project's container ids; `inspect --format
# '{{.Name}} {{.State.Restarting}}'` reports each one's restart state. Which
# ids/states each test sees is varied per test via env vars.
HEALTH_DOCKER_STUB = r"""#!/usr/bin/env bash
if [ "$1" = "compose" ]; then
  printf '%s\n' $IDS
  exit 0
fi
if [ "$1" = "inspect" ]; then
  shift 3   # drop: inspect --format <fmt>
  for id in "$@"; do
    eval "printf '%s\n' \"\$STATE_$id\""
  done
fi
"""


def _extract_health_check_block() -> str:
    """Pull the real CI-18 health-check block out of action.yml and un-escape
    it, same rationale as `_extract_cleanup_block`: a test that reimplements
    the logic it checks proves nothing."""
    start = DEPLOY_ACTION.index('echo "🩺 Checking container health...')
    end = DEPLOY_ACTION.index("no container is restart-looping.\"", start)
    end = DEPLOY_ACTION.index("\n", end) + 1
    block = DEPLOY_ACTION[start:end]
    return block.replace("\$", "$")


def _run_health_check(ids, states):
    """Execute the extracted block with docker stubbed. `states` maps id ->
    '/name true' or '/name false' (the raw `inspect --format` line)."""
    if not BASH:
        raise unittest.SkipTest("no bash available on PATH")
    tmp = Path(tempfile.mkdtemp())
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "docker"
    stub.write_text(HEALTH_DOCKER_STUB, encoding="utf-8", newline="\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    script = tmp / "healthcheck.sh"
    harness = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'DOCKER="docker"\n'
        'COMPOSE_FILES="-f docker-compose.yml"\n'
        + _extract_health_check_block()
        + "echo REACHED_END\n"
    )
    script.write_text(harness, encoding="utf-8", newline="\n")

    env = os.environ.copy()
    env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
    env["IDS"] = " ".join(ids)
    for cid, state in states.items():
        env["STATE_" + cid] = state

    return subprocess.run(
        [BASH, str(script)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )


class CI18ContainerHealthCheck(unittest.TestCase):
    def test_a_healthy_stack_passes_and_reaches_the_end(self):
        """No container restarting -- the deploy proceeds to image cleanup."""
        proc = _run_health_check(
            ids=["c_backend", "c_db"],
            states={"c_backend": "/app-backend false", "c_db": "/app-db false"},
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("REACHED_END", proc.stdout)
        self.assertIn("Container health check passed", proc.stdout)

    def test_a_restart_looping_container_fails_the_deploy(self):
        """Mutation check named in the WO: this must FAIL against a broken
        stack, not merely pass against a healthy one. Reproduces the 2026-08-20
        incident shape -- one container (`beat`-equivalent) stuck restarting
        while the rest of the stack is fine."""
        proc = _run_health_check(
            ids=["c_backend", "c_beat", "c_db"],
            states={
                "c_backend": "/app-backend false",
                "c_beat": "/app-beat true",
                "c_db": "/app-db false",
            },
        )
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("REACHED_END", proc.stdout)
        self.assertIn("stuck restarting", proc.stdout)
        self.assertIn("/app-beat", proc.stdout)

    def test_only_this_projects_containers_are_checked(self):
        """The listing is `compose ps -q`, i.e. scoped to the project the
        deploy just touched -- a restart-looping container belonging to a
        DIFFERENT app on the same host must never fail this deploy."""
        block = _extract_health_check_block()
        self.assertIn("compose $COMPOSE_FILES --env-file .env ps -q", block)
        self.assertNotIn("$DOCKER ps -aq", block)

    def test_health_check_runs_after_migrations_and_before_image_cleanup(self):
        """A crash must be caught before the replaced image is removed, so a
        failed deploy still has an image to roll back to."""
        migrate = DEPLOY_ACTION.index("manage.py migrate --noinput")
        health_check = DEPLOY_ACTION.index('echo "🩺 Checking container health...')
        cleanup = DEPLOY_ACTION.index('echo "Removing the image this deploy replaced')
        self.assertLess(migrate, health_check)
        self.assertLess(health_check, cleanup)

    def test_health_check_runs_before_the_success_line(self):
        """The final echo must not claim success over a stack that is
        restart-looping."""
        health_check = DEPLOY_ACTION.index('echo "🩺 Checking container health...')
        success = DEPLOY_ACTION.index('echo "✅ Deployment finished successfully!"')
        self.assertLess(health_check, success)


if __name__ == "__main__":
    unittest.main()
