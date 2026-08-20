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
    def test_ci_uses_github_token_and_preserves_the_empty_tag_guard(self):
        """The publish workflow relies on the run token, never a stored PAT."""
        self.assertIn("github_token: ${{ secrets.GITHUB_TOKEN }}", CI_WORKFLOW)
        self.assertIn("printf '%s' \"$GITHUB_TOKEN\" | docker login ghcr.io", PUBLISH_ACTION)
        self.assertIn('[ -n "$IMAGE_TAG" ] || { echo "❌ IMAGE_TAG is empty; refusing to publish.";', PUBLISH_ACTION)
        self.assertNotIn("GHCR_TOKEN", CI_WORKFLOW)

    def test_published_images_have_an_oci_source_label(self):
        """Both the normal build and CI-6 full-image rebuild are linked to the repo."""
        source_label = "--label org.opencontainers.image.source=https://github.com/${{ github.repository }}"
        self.assertEqual(CI_WORKFLOW.count(source_label), 1)
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

    def test_callers_resolve_the_checked_out_sha_without_github_sha(self):
        """Image tags come from checkout HEAD, never the triggering ref."""
        self.assertIn('run: echo "sha=$(git rev-parse HEAD)" >> "$GITHUB_OUTPUT"', CI_WORKFLOW)
        self.assertIn("image_tag: ${{ steps.resolve_sha.outputs.sha }}", CI_WORKFLOW)
        self.assertNotIn("github.sha", CI_WORKFLOW)
        self.assertNotIn("github.sha", DEPLOY_ACTION)
        self.assertNotIn("github.sha", PUBLISH_ACTION)
        self.assertIn("image_tag:", DEPLOY_ACTION)
        self.assertIn("required: true", DEPLOY_ACTION.split("image_tag:", 1)[1].split("environment:", 1)[0])
        self.assertEqual(DEPLOY_ACTION.count("IMAGE_TAG: ${{ inputs.image_tag }}"), 2)
        self.assertIn("printf '\\nIMAGE_TAG=%s\\n' \"$IMAGE_TAG\" >> .env", DEPLOY_ACTION)
        self.assertIn('docker push "$PUBLISHED_IMAGE:$IMAGE_TAG"', PUBLISH_ACTION)
        self.assertLess(
            CI_WORKFLOW.index("- name: Run pytest"),
            CI_WORKFLOW.index("- name: Publish backend image to GHCR"),
        )
        self.assertNotIn('docker push "$PUBLISHED_IMAGE:latest"', PUBLISH_ACTION)

    def test_shared_publish_action_is_the_only_publish_implementation(self):
        """CI calls the action and removes the image through its declared output."""
        self.assertIn("uses: ./.wt-checkout/.github/actions/publish-backend-image", CI_WORKFLOW)
        self.assertIn("${{ steps.publish_backend_image.outputs.image }}", CI_WORKFLOW)
        self.assertIn("outputs:\n  image:", PUBLISH_ACTION)
        self.assertIn('printf \'image=%s:%s\\n\' "$PUBLISHED_IMAGE" "$IMAGE_TAG" >> "$GITHUB_OUTPUT"', PUBLISH_ACTION)
        self.assertNotIn('docker push "$PUBLISHED_IMAGE:$IMAGE_TAG"', CI_WORKFLOW)

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
    runtime `$` is written `\\$`, so undoing that escaping is all that stands
    between the committed text and something bash can run.
    """
    start = DEPLOY_ACTION.index('echo "Removing the image this deploy replaced')
    end = DEPLOY_ACTION.index("} || true", start) + len("} || true")
    block = DEPLOY_ACTION[start:end]
    return block.replace("\\$", "$")


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
