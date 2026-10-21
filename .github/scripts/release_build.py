"""CI-only release-contract-v1 image producer with fixed-argv adapters."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading

from release_manifest import ReleaseIdentity, produce_image_manifest, release_image_tag

TIMEOUT_SECONDS = 1800
MAX_INSPECT_BYTES = 128 * 1024
_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
_NAMESPACE = re.compile(r"ghcr\.io/[a-z0-9](?:[a-z0-9-]*[a-z0-9])?/[a-z0-9][a-z0-9._-]*-backend\Z")
_REPO = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_.-]{1,100}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_PATH = re.compile(r"(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\Z")


class ReleaseBuildError(RuntimeError):
    pass


def _scalar(value: str, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, str) and bool(pattern.fullmatch(value)) and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def _identity(env: dict[str, str], git_head: str) -> tuple[ReleaseIdentity, str]:
    repo, sha, ref = env.get("GITHUB_REPOSITORY", ""), env.get("GITHUB_SHA", ""), env.get("GITHUB_REF", "")
    run_id, attempt = env.get("GITHUB_RUN_ID", ""), env.get("GITHUB_RUN_ATTEMPT", "")
    wf_ref, supplied_wf_ref = env.get("GITHUB_WORKFLOW_REF", ""), env.get("RELEASE_WORKFLOW_REF", "")
    if not _scalar(repo, _REPO) or not _scalar(sha, _SHA) or git_head != sha:
        raise ReleaseBuildError("source_identity_invalid")
    if ref not in ("refs/heads/develop", "refs/heads/main"):
        raise ReleaseBuildError("source_ref_unsupported")
    if not run_id.isascii() or not run_id.isdecimal() or int(run_id) <= 0 or not attempt.isascii() or not attempt.isdecimal() or int(attempt) <= 0:
        raise ReleaseBuildError("run_identity_invalid")
    expected_prefix = repo + "/.github/workflows/"
    suffix = "@" + ref
    if not wf_ref.startswith(expected_prefix) or not wf_ref.endswith(suffix) or wf_ref != supplied_wf_ref:
        raise ReleaseBuildError("workflow_identity_invalid")
    workflow = wf_ref[len(repo) + 1 : -len(suffix)]
    if not workflow.startswith(".github/workflows/") or not _scalar(workflow, re.compile(r"\.github/workflows/[A-Za-z0-9][A-Za-z0-9._-]*\.yml\Z")):
        raise ReleaseBuildError("workflow_identity_invalid")
    owner = repo.split("/", 1)[0]
    namespace = env.get("RELEASE_IMAGE_NAMESPACE", "")
    if not _scalar(owner, _OWNER) or not _scalar(namespace, _NAMESPACE) or namespace.split("/")[1].lower() != owner.lower():
        raise ReleaseBuildError("image_namespace_invalid")
    identity = ReleaseIdentity(repo, sha, ref, workflow, int(run_id), int(attempt))
    release_image_tag(identity)  # Enforce the published identity contract before credentials.
    return identity, namespace


def _run(argv: list[str], *, env: dict[str, str], timeout: float = TIMEOUT_SECONDS, capture: bool = True, input_bytes: bytes | None = None) -> bytes:
    """Bound process duration and captured metadata; never expose child diagnostics."""
    if input_bytes is not None and capture:
        raise ReleaseBuildError("command_mode_invalid")
    process = None
    reader = None
    try:
        process = subprocess.Popen(argv, env=env,
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        collected: list[bytes] = []
        read_failed = threading.Event()
        overflow = threading.Event()
        if capture:
            def read_bounded():
                try:
                    data = process.stdout.read(MAX_INSPECT_BYTES + 1)
                    collected.append(data)
                    if len(data) > MAX_INSPECT_BYTES:
                        overflow.set()
                        process.kill()
                except OSError:
                    read_failed.set()
            reader = threading.Thread(target=read_bounded, daemon=True)
            reader.start()
        if input_bytes is not None:
            process.communicate(input=input_bytes, timeout=timeout)
        else:
            process.wait(timeout=timeout)
        if reader:
            reader.join(timeout=1)
            if reader.is_alive() or read_failed.is_set():
                raise ReleaseBuildError("command_failed")
        if overflow.is_set():
            raise ReleaseBuildError("registry_metadata_too_large")
        if process.returncode:
            raise ReleaseBuildError("command_failed")
        return collected[0] if collected else b""
    except (OSError, subprocess.TimeoutExpired):
        raise ReleaseBuildError("command_failed") from None
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)
            if reader:
                reader.join(timeout=1)
            if process.stdout is not None and (reader is None or not reader.is_alive()):
                process.stdout.close()
            if process.stdin is not None:
                process.stdin.close()


def _command_environment(env):
    """Only process/tool discovery and basic OS state, never job credentials."""
    allowed = ("PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
               "HOME", "USERPROFILE", "TMP", "TEMP", "TMPDIR", "LANG", "LC_ALL")
    return {key: env[key] for key in allowed if key in env}


# Last-match exclusions cannot be negated by source-controlled ignore rules.
_CONTEXT_EXCLUSIONS = (".env*", "**/.env*", ".ssh", "**/.ssh", ".secrets", "**/.secrets",
                       "*.pem", "**/*.pem", "*.key", "**/*.key", "id_rsa*", "**/id_rsa*",
                       "id_ed25519*", "**/id_ed25519*", ".git", "**/.git")


def _read_build_control(path: Path) -> bytes:
    # Read only Dockerfile/ignore control files, never an environment/key file.
    try:
        resolved = path.resolve()
        if path.is_symlink() or not resolved.is_relative_to(Path.cwd().resolve()) or any(part.lower().startswith(".env") for part in resolved.parts):
            raise ReleaseBuildError("build_control_invalid")
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ReleaseBuildError("build_control_invalid")
        with path.open("rb") as source:
            raw = source.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024 or b"\x00" in raw:
            raise ReleaseBuildError("build_control_invalid")
        return raw
    except Exception:
        raise ReleaseBuildError("build_control_invalid") from None


def _prepare_build_control(dockerfile: str, context: str, private: Path) -> Path:
    original = Path(dockerfile)
    if not re.fullmatch(r"(?:[A-Za-z0-9_.-]+\.)?Dockerfile(?:\.[A-Za-z0-9_-]+)?", original.name):
        raise ReleaseBuildError("build_control_invalid")
    raw = _read_build_control(original)
    specific = Path(str(original) + ".dockerignore")
    selected = specific if specific.exists() or specific.is_symlink() else Path(context)/".dockerignore"
    ignore = _read_build_control(selected) if selected.exists() or selected.is_symlink() else b""
    # Validate bytes rather than silently changing source ignore semantics.
    try:
        ignore.decode("utf-8", "strict")
    except UnicodeError:
        raise ReleaseBuildError("build_control_invalid") from None
    target = private / "Release.Dockerfile"
    target.write_bytes(raw)
    os.chmod(target, 0o600)
    protected = Path(str(target) + ".dockerignore")
    protected.write_bytes(ignore + b"\n" + "\n".join(_CONTEXT_EXCLUSIONS).encode("ascii") + b"\n")
    os.chmod(protected, 0o600)
    return target


def produce(env: dict[str, str], *, output_dir: Path, run=_run) -> tuple[str, bytes]:
    """Run trusted identity checks, build/push, independently inspect registry."""
    if env.get("RELEASE_CONTRACT") != "v1":
        raise ReleaseBuildError("release_contract_unsupported")
    if env.get("EXISTING_IMAGE", ""):
        raise ReleaseBuildError("existing_image_forbidden")
    identity, namespace = _identity(env, env.get("GITHUB_SHA", ""))
    try:
        head = run(["git", "rev-parse", "HEAD"], env=dict(_command_environment(env), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull), timeout=10).decode("ascii", "strict").strip()
    except Exception:
        raise ReleaseBuildError("source_lookup_failed") from None
    if head != identity.sha:
        raise ReleaseBuildError("source_identity_invalid")
    tag = release_image_tag(identity)
    dockerfile, context = env.get("BACKEND_DOCKERFILE", "backend/Dockerfile"), env.get("BACKEND_BUILD_CONTEXT", ".")
    if not _scalar(dockerfile, _PATH) or not _scalar(context, _PATH) or any(p in ("", ".", "..") for p in dockerfile.split("/")):
        raise ReleaseBuildError("build_path_invalid")
    if context != "." and any(p in ("", ".", "..") for p in context.split("/")):
        raise ReleaseBuildError("build_path_invalid")
    if not env.get("GITHUB_TOKEN"):
        raise ReleaseBuildError("required_credential_missing")
    for value in (dockerfile, context):
        if len(value) > 512 or any(part.startswith("-") or part.lower().startswith(".env") for part in value.split("/")):
            raise ReleaseBuildError("build_path_invalid")
        if not (Path.cwd() / value).resolve().is_relative_to(Path.cwd().resolve()):
            raise ReleaseBuildError("build_path_invalid")
    if any(ancestor.is_symlink() for ancestor in (output_dir.parent, *output_dir.parent.parents)):
        raise ReleaseBuildError("output_path_invalid")
    output_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    config_dir = None
    try:
        config_dir = Path(tempfile.mkdtemp(prefix="release-docker-"))
        os.chmod(config_dir, 0o700)
        build_control = _prepare_build_control(dockerfile, context, config_dir)
        docker_env = dict(_command_environment(env), DOCKER_CONFIG=str(config_dir), DOCKER_BUILDKIT="1")
        run(["docker", "login", "ghcr.io", "--username", env.get("GITHUB_ACTOR", "github-actions"), "--password-stdin"], env=docker_env, timeout=30, capture=False, input_bytes=env["GITHUB_TOKEN"].encode("utf-8"))
        tag_ref = f"{namespace}:{tag}"
        build = ["docker", "build", "--pull", "--build-arg", "VITE_APP_MUI_LICENSE_KEY",
                 "--build-arg", "VITE_SURVEY_APP_API_BASE_URL",
                 "--build-arg", "VITE_SURVEY_APP_SITE_SLUG",
                 "--label", f"org.opencontainers.image.source=https://github.com/{identity.repository}",
                 "-t", tag_ref, "-f", str(build_control), context]
        if env.get("HRAM_ENGINE_READ_TOKEN"):
            build[2:2] = ["--secret", "id=hram_engine_token,env=HRAM_ENGINE_READ_TOKEN"]
        build_env = dict(docker_env)
        for name in ("VITE_APP_MUI_LICENSE_KEY", "VITE_SURVEY_APP_API_BASE_URL", "VITE_SURVEY_APP_SITE_SLUG"):
            build_env[name] = env.get(name, "")
        if env.get("HRAM_ENGINE_READ_TOKEN"):
            build_env["HRAM_ENGINE_READ_TOKEN"] = env["HRAM_ENGINE_READ_TOKEN"]
        run(build, env=build_env, capture=False)
        run(["docker", "push", tag_ref], env=docker_env, timeout=600, capture=False)

        def resolve(repository: str, pushed_tag: str):
            raw = run(["docker", "buildx", "imagetools", "inspect", f"{repository}:{pushed_tag}", "--format", '{"name":{{json .Name}},"digest":{{json .Manifest.Digest}}}'], env=docker_env, timeout=30)
            try:
                if len(raw) > MAX_INSPECT_BYTES:
                    raise ReleaseBuildError("registry_metadata_too_large")
                data = json.loads(raw)
                if not isinstance(data, dict) or set(data) != {"name", "digest"} or data["name"] != f"{repository}:{pushed_tag}":
                    raise ReleaseBuildError("registry_identity_invalid")
                digest = data["digest"]
            except (UnicodeError, json.JSONDecodeError):
                digest = None
            return {"repository": repository, "tag": pushed_tag, "digest": digest}

        manifest = produce_image_manifest(identity, expected_namespace=namespace, pushed_tag=tag, resolve_pushed_image=resolve)
        (output_dir / "release.json").write_bytes(manifest)
        (output_dir / "image.txt").write_text(namespace + "@" + json.loads(manifest)["output"]["image"].split("@", 1)[1], encoding="ascii")
        os.chmod(output_dir / "release.json", 0o600)
        os.chmod(output_dir / "image.txt", 0o600)
        return namespace + "@" + json.loads(manifest)["output"]["image"].split("@", 1)[1], manifest
    except Exception:
        shutil.rmtree(output_dir, ignore_errors=True)
        raise ReleaseBuildError("release_production_failed") from None
    finally:
        if config_dir is not None:
            try:
                shutil.rmtree(config_dir)
            except OSError:
                shutil.rmtree(output_dir, ignore_errors=True)
                raise ReleaseBuildError("credential_cleanup_failed") from None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    try:
        produce(dict(os.environ), output_dir=Path(args.output_dir))
    except Exception as exc:
        code = str(exc) if isinstance(exc, ReleaseBuildError) else "release_production_failed"
        print(f"release-build failed: {code}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
