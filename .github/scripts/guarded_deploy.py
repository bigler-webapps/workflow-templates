"""Run the fixed legacy Compose deployment while holding host recovery admission."""

from __future__ import annotations

import os
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterable


RECOVERY_MODULE_DIR = "/usr/local/lib/webapp-recovery"
PROJECT_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}\Z", re.ASCII)
ENVIRONMENT_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z", re.ASCII)
RELATIVE_DIR_RE = re.compile(r"[A-Za-z0-9._/-]+\Z", re.ASCII)


class DeployError(RuntimeError):
    pass


def _load_recovery():
    if RECOVERY_MODULE_DIR not in sys.path:
        sys.path.insert(0, RECOVERY_MODULE_DIR)
    try:
        import stack_recovery
    except ImportError as exc:
        raise DeployError("required destination recovery contract is unavailable") from exc
    return stack_recovery


def _safe_persistent_dir(value: str) -> bool:
    if not value or not RELATIVE_DIR_RE.fullmatch(value) or value.startswith("/"):
        return False
    return all(part not in ("", ".", "..") and not part.startswith("-") for part in value.split("/"))


def _completed(argv: Iterable[str], *, guard, env, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(argv), env=env, check=True, pass_fds=(guard.fd,), **kwargs
    )


def _captured(argv: Iterable[str], *, guard, env) -> str:
    try:
        result = subprocess.run(
            list(argv), env=env, check=True, pass_fds=(guard.fd,),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise DeployError("Docker observation failed; container state is unknown") from exc
    if result.returncode != 0 or not isinstance(result.stdout, str):
        raise DeployError("Docker observation is unavailable; container state is unknown")
    return result.stdout


def _unique_lines(value: str) -> list[str]:
    return sorted({line.strip() for line in value.splitlines() if line.strip()})


def deploy(env: dict[str, str], recovery) -> None:
    project = env.get("PROJECT_NAME", "")
    if not PROJECT_RE.fullmatch(project):
        raise DeployError("invalid deployment project")
    if not ENVIRONMENT_RE.fullmatch(env.get("TARGET_ENV", "")):
        raise DeployError("invalid deployment environment")

    remote_path = env.get("REMOTE_PATH", "")
    if not remote_path or not Path(remote_path).is_absolute():
        raise DeployError("invalid deployment path")
    os.chdir(remote_path)

    image_tag = env.get("IMAGE_TAG", "")
    if not image_tag:
        raise DeployError("image tag is required")
    fallback = env.get("USE_BUILD_FALLBACK", "false")
    skip_migrate = env.get("SKIP_MIGRATE", "false")
    if fallback not in ("true", "false") or skip_migrate not in ("true", "false"):
        raise DeployError("invalid deployment option")
    token = env.get("GITHUB_TOKEN", "")
    if fallback != "true" and not token:
        raise DeployError("registry token is required for pull-based deployment")

    persistent_dirs = env.get("PERSISTENT_DIRS", "").split()
    if any(not _safe_persistent_dir(path) for path in persistent_dirs):
        raise DeployError("invalid persistent directory configuration")

    compose_project = f"{project}_{env['TARGET_ENV']}"
    with recovery.writer_guard(compose_project) as guard:
        docker_prefix = ["docker"]
        probe_env = os.environ.copy()
        probe_env.pop("GITHUB_TOKEN", None)
        probe_env.pop("HRAM_ENGINE_READ_TOKEN", None)
        try:
            subprocess.run(
                ["docker", "info"], check=True, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=30, env=probe_env,
            )
        except (OSError, subprocess.SubprocessError):
            raise DeployError("the authorized deployment identity cannot reach Docker")

        with tempfile.TemporaryDirectory(prefix="deploy-docker-config-") as docker_config:
            child_env = os.environ.copy()
            child_env.update(env)
            child_env.pop("GITHUB_TOKEN", None)
            child_env.pop("HRAM_ENGINE_READ_TOKEN", None)
            child_env["DOCKER_CONFIG"] = docker_config

            def docker(*args: str) -> list[str]:
                return [*docker_prefix, *args]

            compose = docker("compose", "-f", "docker-compose.yml", "--env-file", ".env", "--project-name", compose_project)
            previous_ids = _captured([*compose, "ps", "-q"], guard=guard, env=child_env).split()
            previous_refs = _unique_lines(
                _captured(
                    [*docker("inspect", "--format", "{{.Config.Image}}"), *previous_ids],
                    guard=guard, env=child_env,
                )
            ) if previous_ids else []

            print("Starting deployment under the destination recovery guard.", flush=True)
            _completed(docker("image", "prune", "-f"), guard=guard, env=child_env)
            for path in persistent_dirs:
                Path(path).mkdir(parents=True, exist_ok=True)

            if fallback == "true":
                print("Recreating containers with the explicit server-build fallback.", flush=True)
                build_env = child_env.copy()
                build_env["DOCKER_BUILDKIT"] = "1"
                if env.get("HRAM_ENGINE_READ_TOKEN"):
                    build_env["HRAM_ENGINE_READ_TOKEN"] = env["HRAM_ENGINE_READ_TOKEN"]
                _completed([*compose, "build"], guard=guard, env=build_env)
            else:
                print("Logging in to the image registry and pulling the CI image.", flush=True)
                _completed(
                    docker("login", "ghcr.io", "--username", env.get("GITHUB_ACTOR", "github-actions"),
                           "--password-stdin"),
                    guard=guard, env=child_env, input=token, text=True,
                )
                _completed([*compose, "pull"], guard=guard, env=child_env)

            # Create without starting application commands, then compile their
            # existing argv into a fixed container-local startup gate.
            _completed([*compose, "create", "--force-recreate"], guard=guard, env=child_env)
            created_ids = _captured([*compose, "ps", "-a", "-q"], guard=guard, env=child_env).split()
            if not created_ids:
                raise DeployError("created container inventory is unavailable")
            inspected = json.loads(_captured(docker("inspect", *created_ids), guard=guard, env=child_env))
            override = recovery.gated_compose_override(compose_project, inspected)
            fd, temporary = tempfile.mkstemp(prefix=".recovery-compose-", dir=remote_path)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(override, stream, separators=(",", ":"))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, ".webapp-recovery-compose.json")
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            compose = docker("compose", "-f", "docker-compose.yml", "-f", ".webapp-recovery-compose.json",
                             "--env-file", ".env", "--project-name", compose_project)
            _completed([*compose, "up", "-d", "--force-recreate", "--remove-orphans"], guard=guard, env=child_env)

            if skip_migrate != "true":
                print("Waiting for backend initialization.", flush=True)
                time.sleep(3)
                _completed(
                    [*compose, "exec", "-T", "backend", "python", "manage.py", "migrate", "--noinput"],
                    guard=guard, env=child_env,
                )

            print("Checking for containers stuck restarting.", flush=True)
            current_ids = _captured([*compose, "ps", "-a", "-q"], guard=guard, env=child_env).split()
            if not current_ids or len(set(current_ids)) != len(current_ids):
                raise DeployError("post-deploy container inventory is unknown")
            raw = _captured(docker("inspect", *current_ids), guard=guard, env=child_env)
            try:
                observed = json.loads(raw)
            except (TypeError, ValueError) as exc:
                raise DeployError("post-deploy container observation is malformed") from exc
            if not isinstance(observed, list) or len(observed) != len(current_ids):
                raise DeployError("post-deploy container observation is incomplete")
            for item in observed:
                if not isinstance(item, dict):
                    raise DeployError("post-deploy container observation is malformed")
                state = item.get("State") or {}
                labels = (item.get("Config") or {}).get("Labels") or {}
                policy = ((item.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name")
                if (type(state.get("Running")) is not bool or type(state.get("Restarting")) is not bool
                        or labels.get("com.docker.compose.project") != compose_project
                        or policy not in {"no", "always", "unless-stopped", "on-failure"}):
                    raise DeployError("post-deploy container state is unknown")
                oneoff = str(labels.get("com.docker.compose.oneoff", "false")).lower() == "true"
                if not oneoff and (state["Restarting"] or (not state["Running"] and policy != "no")):
                    raise DeployError("application service is stopped or restart-looping")
            print("Container states observed; no restart loop detected.", flush=True)

            new_ids = current_ids
            new_refs = set(_unique_lines(
                _captured(
                    [*docker("inspect", "--format", "{{.Config.Image}}"), *new_ids],
                    guard=guard, env=child_env,
                )
            )) if new_ids else set()
            in_use_ids = _captured(docker("ps", "-aq"), guard=guard, env=child_env).split()
            in_use_refs = set(_unique_lines(
                _captured(
                    [*docker("inspect", "--format", "{{.Config.Image}}"), *in_use_ids],
                    guard=guard, env=child_env,
                )
            )) if in_use_ids else set()
            print("Removing the image this deploy replaced (best effort).", flush=True)
            for ref in previous_refs:
                if ref in new_refs:
                    continue
                if ref.endswith(":" + image_tag):
                    print(f"Refusing to remove the tag just deployed: {ref}", flush=True)
                    continue
                if ref in in_use_refs:
                    print(f"Image remains in use by a container: {ref}", flush=True)
                    continue
                try:
                    _completed(docker("image", "rm", ref), guard=guard, env=child_env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    print(f"Removed replaced image: {ref}", flush=True)
                except (OSError, subprocess.CalledProcessError):
                    print(f"Could not remove replaced image; left in place: {ref}", flush=True)

            print("Deployment finished successfully.", flush=True)


def main() -> int:
    try:
        recovery = _load_recovery()
        deploy(dict(os.environ), recovery)
    except DeployError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        try:
            Path(__file__).unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
