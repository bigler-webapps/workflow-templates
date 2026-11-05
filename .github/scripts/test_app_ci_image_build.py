"""Regression coverage for full production image builds on Dockerfile PRs."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "app-ci.yml"
WORKFLOW = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
JOBS = WORKFLOW["jobs"]
IMAGE_BUILD = JOBS["image-build"]


def _step(job: dict, name: str) -> dict:
    return next(step for step in job["steps"] if step.get("name") == name)


class AppCiImageBuildStructureTests(unittest.TestCase):
    def test_image_build_is_a_named_contract_and_builds_without_a_target(self):
        self.assertNotIn("name", IMAGE_BUILD)
        job_if = IMAGE_BUILD["if"]
        self.assertIn("inputs.run-backend", job_if)
        self.assertIn("pull_request", job_if)
        self.assertIn("workflow_dispatch", job_if)

        step_runs = "\n".join(step.get("run", "") for step in IMAGE_BUILD["steps"])
        # The check name is a contract with webapp-management WM-RT-49: a caller job `ci` shows this job as
        # `ci / image-build`, which needs the job key `image-build` and no `name:` on it or on its siblings.
        self.assertIn("image-build", JOBS)
        self.assertTrue(all("name" not in job for job in JOBS.values()))
        self.assertNotIn("--target", step_runs)
        self.assertNotIn("docker push", step_runs.lower())
        self.assertNotIn("--push", step_runs)

        build = _step(IMAGE_BUILD, "Build full production image (no --target)")
        self.assertEqual(
            build["if"],
            "${{ steps.scope.outputs.dockerfile_changed == 'true' }}",
        )
        # A failing full build must fail the named check: strict shell, nothing swallows the exit status.
        self.assertIn("set -euo pipefail", build["run"])
        self.assertNotIn("|| true", build["run"])
        self.assertNotIn("continue-on-error", build)
        self.assertNotIn("continue-on-error", IMAGE_BUILD)
        self.assertIn("docker build", build["run"])
        self.assertIn('-f "$BACKEND_DOCKERFILE"', build["run"])
        self.assertIn('-t "$CI_IMAGE"', build["run"])
        self.assertIn("--secret id=hram_engine_token,env=HRAM_ENGINE_READ_TOKEN", build["run"])
        self.assertIn(
            '--build-arg "VITE_APP_MUI_LICENSE_KEY=$VITE_APP_MUI_LICENSE_KEY"',
            build["run"],
        )
        self.assertNotIn("BACKEND_TARGET", build.get("env", {}))

        detect = _step(IMAGE_BUILD, "Detect Dockerfile changes")
        self.assertEqual(detect["id"], "scope")
        self.assertEqual(detect["env"]["BACKEND_DOCKERFILE"], "${{ inputs.dockerfile }}")
        self.assertEqual(detect["env"]["BACKEND_BUILD_CONTEXT"], "${{ inputs.build-context }}")
        self.assertEqual(detect["env"]["EVENT_NAME"], "${{ github.event_name }}")
        self.assertIn("git diff --name-only -z HEAD^1 HEAD", detect["run"])
        self.assertIn("while IFS= read -r -d '' path", detect["run"])

        cleanup = _step(IMAGE_BUILD, "Remove CI image")
        self.assertIn("always()", cleanup["if"])
        self.assertIn("steps.scope.outputs.dockerfile_changed == 'true'", cleanup["if"])

    def test_backend_target_build_and_postgres_service_remain(self):
        backend = JOBS["backend"]
        build = _step(backend, "Build backend image")
        self.assertIn("target_args", build["run"])
        self.assertIn('target_args=(--target "$BACKEND_TARGET")', build["run"])
        self.assertEqual(build["env"]["BACKEND_TARGET"], "${{ inputs.backend-target }}")
        self.assertIn("postgres", backend["services"])


_BASH = shutil.which("bash")
if _BASH and os.name == "nt":
    _git_root = Path(_BASH).parent.parent.parent
    _git_bash_launcher = _git_root / "bin" / "bash.exe"
    if _git_bash_launcher.is_file():
        _BASH = str(_git_bash_launcher)
_GIT = shutil.which("git")


def _unavailable(reason: str):
    """In GitHub Actions (CI is set) a missing bash or git must fail, not skip: a silent skip would leave the
    scope behaviour untested while the run stays green."""
    if os.environ.get("CI"):
        raise AssertionError(reason)
    raise unittest.SkipTest(reason)


class AppCiImageBuildScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not (_BASH and _GIT):
            _unavailable("scope integration test requires bash and git")
        process_options = {
            "creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        } if os.name == "nt" else {}
        try:
            probe = subprocess.run(
                [_BASH, "-c", ":"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=10,
                **process_options,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            _unavailable(f"bash cannot execute in this environment: {error}")
        if probe.returncode != 0:
            _unavailable(f"bash is installed but cannot execute in this environment (exit {probe.returncode})")

    def _git(self, repository: Path, *args: str) -> str:
        completed = subprocess.run(
            [_GIT, *args],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    def _run_scope(
        self,
        changed_paths: tuple[str, ...],
        *,
        event_name: str = "pull_request",
        dockerfile: str = "backend/Dockerfile",
        build_context: str = ".",
        merge_commit: bool = True,
    ) -> tuple[str, str]:
        with tempfile.TemporaryDirectory(prefix="app-ci-image-build-", dir=ROOT) as temporary_directory:
            repository = Path(temporary_directory) / "repo"
            repository.mkdir()
            self._git(repository, "init", "--quiet")
            self._git(repository, "config", "user.name", "CI test")
            self._git(repository, "config", "user.email", "ci-test@example.invalid")
            self._git(repository, "checkout", "-b", "main")

            base_dockerfile = repository / "backend" / "Dockerfile"
            base_dockerfile.parent.mkdir(parents=True)
            base_dockerfile.write_text("FROM python:3.11\n", encoding="utf-8")
            self._git(repository, "add", "--all")
            self._git(repository, "commit", "--quiet", "-m", "base")

            if merge_commit:
                self._git(repository, "checkout", "-b", "pr-head")
                for index, changed_path in enumerate(changed_paths):
                    changed_file = repository / changed_path
                    changed_file.parent.mkdir(parents=True, exist_ok=True)
                    changed_file.write_text(f"changed file {index}\n", encoding="utf-8")
                if not changed_paths:
                    marker = repository / "pr-change.txt"
                    marker.write_text("unrelated PR change\n", encoding="utf-8")
                self._git(repository, "add", "--all")
                self._git(repository, "commit", "--quiet", "-m", "PR head")
                self._git(repository, "checkout", "main")
                self._git(repository, "merge", "--no-ff", "--no-edit", "pr-head")

            output_file = repository / "github_output.txt"
            output_file.touch()
            script_file = repository / "detect-dockerfile-changes.sh"
            script_file.write_text(
                _step(IMAGE_BUILD, "Detect Dockerfile changes")["run"],
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "EVENT_NAME": event_name,
                    "BACKEND_DOCKERFILE": dockerfile,
                    "BACKEND_BUILD_CONTEXT": build_context,
                    "GITHUB_OUTPUT": output_file.as_posix(),
                }
            )
            process_options = {
                "creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            } if os.name == "nt" else {}
            completed = subprocess.run(
                [_BASH, "-e", script_file.as_posix()],
                cwd=repository,
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                **process_options,
            )
            self.assertEqual(
                completed.returncode,
                0,
                f"bash={_BASH!r}; stdout={completed.stdout!r}; stderr={completed.stderr!r}",
            )
            return output_file.read_text(encoding="utf-8"), completed.stdout

    def _assert_scope(self, expected: bool, *args, **kwargs):
        output, _notice = self._run_scope(*args, **kwargs)
        self.assertEqual(output.strip(), f"dockerfile_changed={str(expected).lower()}")

    def test_caller_dockerfile_change_selects_full_build(self):
        self._assert_scope(True, ("backend/Dockerfile",))

    def test_unrelated_change_keeps_the_opted_in_target_path(self):
        self._assert_scope(False, ("docs/readme.txt",))

    def test_other_dockerfile_in_build_context_selects_full_build(self):
        self._assert_scope(True, ("backend/Dockerfile.prod",))

    def test_dockerfile_outside_narrow_build_context_is_ignored(self):
        self._assert_scope(
            False,
            ("other/Dockerfile",),
            build_context="backend",
        )

    def test_unrelated_path_with_spaces_is_ignored(self):
        self._assert_scope(False, ("docs/design notes.txt",))

    def test_non_pull_request_event_fails_open(self):
        output, notice = self._run_scope(
            ("docs/readme.txt",),
            event_name="workflow_dispatch",
        )
        self.assertEqual(output.strip(), "dockerfile_changed=true")
        self.assertIn("non-pull-request event", notice)

    def test_pull_request_without_second_parent_fails_open(self):
        output, notice = self._run_scope((), merge_commit=False)
        self.assertEqual(output.strip(), "dockerfile_changed=true")
        self.assertIn("HEAD has no second parent", notice)


if __name__ == "__main__":
    unittest.main()
