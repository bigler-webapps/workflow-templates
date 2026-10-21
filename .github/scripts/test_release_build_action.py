"""Offline behavioral coverage for the release-contract-v1 composite producer."""
from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
import io
import json
import shutil
import subprocess
import sys
import traceback
import yaml
from unittest import mock
import os
from pathlib import Path
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_build
from release_manifest import ReleaseIdentity, produce_image_manifest, release_image_tag

ROOT = Path(__file__).resolve().parents[2]
ACTION = ROOT / ".github" / "actions" / "publish-backend-image" / "action.yml"
SHA = "0123456789abcdef0123456789abcdef01234567"
IMAGE_NS = "ghcr.io/bigler-webapps/sample-backend"
FAKE_TOKEN = "synthetic-secret-token-must-not-leak"


def context(**overrides):
    result = {
        "RELEASE_CONTRACT": "v1",
        "RELEASE_IMAGE_NAMESPACE": IMAGE_NS,
        "RELEASE_WORKFLOW_REF": "bigler-webapps/sample-app/.github/workflows/release.yml@refs/heads/develop",
        "GITHUB_WORKFLOW_REF": "bigler-webapps/sample-app/.github/workflows/release.yml@refs/heads/develop",
        "GITHUB_REPOSITORY": "bigler-webapps/sample-app",
        "GITHUB_SHA": SHA,
        "GITHUB_REF": "refs/heads/develop",
        "GITHUB_RUN_ID": "123456",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_ACTOR": "github-actions[bot]",
        "GITHUB_TOKEN": FAKE_TOKEN,
        "HRAM_ENGINE_READ_TOKEN": "synthetic-build-token",
        "BACKEND_DOCKERFILE": "backend/Dockerfile",
        "BACKEND_BUILD_CONTEXT": ".",
        "GITHUB_OUTPUT": "",
    }
    result.update(overrides)
    return result


class CommandStub:
    def __init__(self, *, inspect=None, fail_at=None):
        self.calls = []
        self.inspect = {"digest": "sha256:" + "a" * 64} if inspect is None else inspect
        self.fail_at = fail_at

    def __call__(self, argv, *, env, timeout=release_build.TIMEOUT_SECONDS, capture=True, input_bytes=None):
        self.calls.append((list(argv), dict(env), timeout, capture, input_bytes))
        if self.fail_at == argv[:2]:
            raise release_build.ReleaseBuildError("command_failed")
        if argv[:2] == ["git", "rev-parse"]:
            return (SHA + "\n").encode("ascii")
        if argv[:4] == ["docker", "buildx", "imagetools", "inspect"]:
            return json.dumps(dict({"name": argv[4]}, **self.inspect)).encode("ascii")
        return b""


class ReleaseBuildActionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = tempfile.TemporaryDirectory()
        self.old_cwd = Path.cwd()
        os.chdir(self.fixture.name)
        Path("backend").mkdir()
        Path("backend/Dockerfile").write_text("FROM scratch\nCOPY . /app\n", encoding="utf-8")

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.fixture.cleanup()

    def test_pushed_tag_is_resolved_and_observed_digest_reaches_canonical_manifest(self):
        expected_digest = "sha256:" + "b" * 64
        stub = CommandStub(inspect={"digest": expected_digest})
        parent = Path(tempfile.mkdtemp())
        output = parent / "release-output"
        image, raw = release_build.produce(context(), output_dir=output, run=stub)
        identity = ReleaseIdentity("bigler-webapps/sample-app", SHA, "refs/heads/develop", ".github/workflows/release.yml", 123456, 2)
        tag = release_image_tag(identity)
        self.assertEqual(image, f"{IMAGE_NS}@{expected_digest}")
        self.assertEqual(json.loads(raw)["output"]["image"], image)
        self.assertEqual((output / "release.json").read_bytes(), raw)
        self.assertEqual((output / "image.txt").read_text(encoding="ascii"), image)
        docker = [call[0] for call in stub.calls if call[0][0] == "docker"]
        self.assertEqual([argv[:2] for argv in docker], [["docker", "login"], ["docker", "build"], ["docker", "push"], ["docker", "buildx"]])
        build, push, inspect = docker[1:]
        self.assertIn("--pull", build)
        self.assertEqual(build[build.index("-t") + 1], f"{IMAGE_NS}:{tag}")
        self.assertNotIn("tag", [argv[1] for argv in docker])
        self.assertEqual(push[2], f"{IMAGE_NS}:{tag}")
        self.assertEqual(inspect[4], f"{IMAGE_NS}:{tag}")
        self.assertEqual(stub.calls[1][4], FAKE_TOKEN.encode())
        self.assertEqual(stub.calls[-1][2], 30)

    def test_identity_and_malicious_inputs_fail_before_login_or_output_creation(self):
        cases = [
            {"GITHUB_SHA": "f" * 40},
            {"GITHUB_REF": "refs/pull/1/merge"},
            {"GITHUB_WORKFLOW_REF": "attacker/repo/.github/workflows/release.yml@refs/heads/develop", "RELEASE_WORKFLOW_REF": "attacker/repo/.github/workflows/release.yml@refs/heads/develop"},
            {"RELEASE_IMAGE_NAMESPACE": "ghcr.io/other-owner/sample-backend"},
            {"RELEASE_IMAGE_NAMESPACE": IMAGE_NS + ";touch"},
            {"BACKEND_DOCKERFILE": "../Dockerfile"},
            {"BACKEND_BUILD_CONTEXT": "backend\n--push"},
            {"BACKEND_BUILD_CONTEXT": "--help"},
            {"BACKEND_DOCKERFILE": "--file"},
            {"BACKEND_DOCKERFILE": ".env"},
            {"BACKEND_DOCKERFILE": ".env-dir/Dockerfile"},
            {"RELEASE_CONTRACT": "unknown"},
            {"EXISTING_IMAGE": "ghcr.io/old/backend:tag"},
            {"GITHUB_WORKFLOW_REF": "bigler-webapps/sample-app/.github/workflows/../bad.yml@refs/heads/develop", "RELEASE_WORKFLOW_REF": "bigler-webapps/sample-app/.github/workflows/../bad.yml@refs/heads/develop"},
        ]
        for override in cases:
            with self.subTest(override=override):
                parent = Path(tempfile.mkdtemp())
                output = parent / "never-created"
                stub = CommandStub()
                with self.assertRaises(release_build.ReleaseBuildError):
                    release_build.produce(context(**override), output_dir=output, run=stub)
                self.assertTrue(all(call[0][0] == "git" for call in stub.calls))
                self.assertFalse(any(call[0][0] == "docker" for call in stub.calls))
                self.assertFalse(output.exists())

    def test_missing_credentials_fail_before_registry_or_docker_commands(self):
        stub = CommandStub()
        env = context(GITHUB_TOKEN="")
        with self.assertRaisesRegex(release_build.ReleaseBuildError, "required_credential_missing"):
            release_build.produce(env, output_dir=Path(tempfile.mkdtemp()) / "output", run=stub)
        self.assertEqual(len(stub.calls), 1)

    def test_bad_or_missing_registry_metadata_fails_and_removes_output(self):
        for metadata in ({}, {"digest": "sha256:bad"}, {"digest": "sha256:" + "c" * 64, "name": "ghcr.io/other/backend:forged"}):
            with self.subTest(metadata=metadata):
                output = Path(tempfile.mkdtemp()) / "output"
                stub = CommandStub(inspect=metadata)
                with self.assertRaises(release_build.ReleaseBuildError):
                    release_build.produce(context(), output_dir=output, run=stub)
                self.assertFalse(output.exists())

    def test_command_failure_suppresses_raw_error_and_cleans_private_outputs(self):
        output = Path(tempfile.mkdtemp()) / "output"
        stub = CommandStub(fail_at=["docker", "push"])
        with self.assertRaisesRegex(release_build.ReleaseBuildError, "release_production_failed") as caught:
            release_build.produce(context(), output_dir=output, run=stub)
        self.assertNotIn(FAKE_TOKEN, str(caught.exception))
        self.assertFalse(output.exists())
        config_dir = stub.calls[1][1]["DOCKER_CONFIG"]
        self.assertFalse(Path(config_dir).exists())

    def test_inspect_output_is_bounded_and_manifest_has_no_secret(self):
        output = Path(tempfile.mkdtemp()) / "output"
        stub = CommandStub()
        image, raw = release_build.produce(context(), output_dir=output, run=stub)
        self.assertLessEqual(len(raw), 16 * 1024)
        self.assertNotIn(FAKE_TOKEN.encode(), raw)
        self.assertNotIn(b"synthetic-build-token", raw)
        for args, _, timeout, _, input_bytes in stub.calls:
            if args[:4] == ["docker", "buildx", "imagetools", "inspect"]:
                self.assertLessEqual(timeout, 30)
            if args[:2] != ["docker", "login"]:
                self.assertNotEqual(input_bytes, FAKE_TOKEN.encode())

    def test_composite_shell_upload_and_cleanup_wiring_is_explicit(self):
        action = ACTION.read_text(encoding="utf-8")
        self.assertIn("inputs.release_contract != ''", action)
        self.assertIn("python3 \"$ACTION_PATH/../../scripts/release_build.py\" --output-dir \"$OUTPUT_DIR\"", action)
        self.assertIn("actions/upload-artifact@65c4c4a1ddee5b72f698fdd19549f0f0fb45cf08", action)
        self.assertIn("if-no-files-found: error", action)
        self.assertIn("overwrite: false", action)
        self.assertIn("retention-days: 7", action)
        self.assertIn("always() && inputs.release_contract == 'v1'", action)
        self.assertIn("if: ${{ inputs.release_contract == '' }}", action)
        self.assertIn("printf 'image=%s:%s\\n' \"$PUBLISHED_IMAGE\" \"$IMAGE_TAG\"", action)
        self.assertIn("docker tag \"$EXISTING_IMAGE\"", action)


    def test_optional_build_token_and_registry_token_are_not_forwarded_unnecessarily(self):
        stub = CommandStub()
        with tempfile.TemporaryDirectory() as directory:
            release_build.produce(context(HRAM_ENGINE_READ_TOKEN=""), output_dir=Path(directory) / "output", run=stub)
        build = next(call for call in stub.calls if call[0][:2] == ["docker", "build"])
        self.assertNotIn("--secret", build[0])
        self.assertNotIn("GITHUB_TOKEN", build[1])
        for args, environment, _, _, _ in stub.calls[2:]:
            self.assertNotIn("GITHUB_TOKEN", environment)
        self.assertFalse(Path(stub.calls[1][1]["DOCKER_CONFIG"]).exists())

    def test_actual_head_mismatch_and_build_failure_never_push(self):
        for failure in ("head", "build", "login", "push", "inspect"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                stub = CommandStub()
                def command(argv, **kwargs):
                    if failure == "head" and argv[0] == "git":
                        return b"f" * 40 + b"\n"
                    if argv[:2] == ["docker", failure] or (failure == "inspect" and argv[1] == "buildx"):
                        raise RuntimeError(FAKE_TOKEN)
                    return stub(argv, **kwargs)
                output = Path(directory) / "output"
                with self.assertRaises(Exception) as caught:
                    release_build.produce(context(), output_dir=output, run=command)
                rendered = "".join(traceback.format_exception(caught.exception))
                self.assertNotIn(FAKE_TOKEN, rendered)
                self.assertFalse(output.exists())
                if failure in ("head", "login", "build"):
                    self.assertFalse(any(call[0][:2] == ["docker", "push"] for call in stub.calls))
                for args, environment, _, _, _ in stub.calls:
                    if "DOCKER_CONFIG" in environment:
                        self.assertFalse(Path(environment["DOCKER_CONFIG"]).exists())

    def test_bounded_adapter_real_small_processes_and_sanitized_stderr(self):
        # Only local Python children: never Docker, registry, network or a server.
        environment = dict(os.environ)
        self.assertEqual(release_build._run([sys.executable, "-c", "print('ok')"], env=environment, timeout=3), b"ok\r\n" if os.name == "nt" else b"ok\n")
        for program, expected in [
            ("import sys; sys.stdout.buffer.write(b'x'*200000)", "registry_metadata_too_large"),
            ("import time; time.sleep(10)", "command_failed"),
            ("import sys; sys.stderr.write('" + FAKE_TOKEN + "'); sys.exit(2)", "command_failed"),
        ]:
            with self.subTest(program=program), self.assertRaisesRegex(release_build.ReleaseBuildError, expected) as caught:
                release_build._run([sys.executable, "-c", program], env=environment, timeout=0.2)
            self.assertNotIn(FAKE_TOKEN, "".join(traceback.format_exception(caught.exception)))

    def test_main_diagnostics_and_artifacts_never_echo_command_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            stdout, stderr = io.StringIO(), io.StringIO()
            stub = CommandStub(fail_at=["docker", "push"])
            with mock.patch.dict(os.environ, context(), clear=True), mock.patch.object(sys, "argv", ["release-build", "--output-dir", str(output)]), mock.patch.object(release_build, "produce", side_effect=RuntimeError(FAKE_TOKEN)), redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(release_build.main(), 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "release-build failed: release_production_failed\n")
            self.assertFalse(output.exists())

    def test_credential_cleanup_failure_cannot_return_success(self):
        with tempfile.TemporaryDirectory() as directory:
            stub = CommandStub()
            output = Path(directory) / "output"
            original = shutil.rmtree
            failed = []
            def remove(path, *args, **kwargs):
                if Path(path).name.startswith("release-docker-"):
                    failed.append(Path(path))
                    raise OSError(FAKE_TOKEN)
                return original(path, *args, **kwargs)
            with mock.patch.object(release_build.shutil, "rmtree", side_effect=remove):
                with self.assertRaisesRegex(release_build.ReleaseBuildError, "credential_cleanup_failed") as caught:
                    release_build.produce(context(), output_dir=output, run=stub)
            self.assertNotIn(FAKE_TOKEN, "".join(traceback.format_exception(caught.exception)))
            self.assertFalse(output.exists())
            for path in failed:
                original(path)

    @unittest.skipUnless(os.name == "posix", "Real Bash composite harness runs on the Linux controller")
    def test_real_composite_shell_uses_installed_script_and_cleans_only_own_output(self):
        action = ACTION.read_text(encoding="utf-8")
        def shell_of(name):
            block = action.split("    - name: " + name + "\n", 1)[1].split("    - name:", 1)[0]
            raw = block.split("      run: |\n", 1)[1]
            return "\n".join(line[8:] for line in raw.splitlines())
        produce_shell = shell_of("Produce release-contract-v1 image and manifest")
        cleanup_shell = shell_of("Remove private release output directory")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); binary = base / "bin"; binary.mkdir()
            (base/"backend").mkdir(); (base/"backend/Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
            records = base / "commands.jsonl"
            fake = "#!/usr/bin/env python3\n" + '''import json,os,sys
from pathlib import Path
args=sys.argv[1:]
with open(RECORD_PATH,"a") as handle: handle.write(json.dumps([Path(sys.argv[0]).name,args])+"\\n")
if Path(sys.argv[0]).name=="git": print(SOURCE_SHA)
elif args[:3]==["buildx","imagetools","inspect"]: print(json.dumps({"name":args[3],"digest":"sha256:"+"d"*64}))
'''
            fake = fake.replace("RECORD_PATH", repr(str(records))).replace("SOURCE_SHA", repr(SHA))
            for name in ("git", "docker"):
                path = binary / name; path.write_text(fake, encoding="utf-8"); path.chmod(0o700)
            (binary / "python3").symlink_to(sys.executable)
            environment = dict(os.environ, **context())
            environment.update(PATH=str(binary)+os.pathsep+os.environ["PATH"], RUNNER_TEMP=str(base), ACTION_PATH=str(ACTION.parent), GITHUB_OUTPUT=str(base/"outputs"), TEST_RECORD=str(records))
            unrelated = base / "release-build-preexisting"; unrelated.mkdir(); (unrelated / "keep").write_text("keep")
            environment["RELEASE_CONTRACT"] = "unsupported"
            unsupported = subprocess.run(["bash","-e","-c",produce_shell], env=environment, cwd=base, capture_output=True, timeout=5)
            self.assertNotEqual(unsupported.returncode, 0)
            self.assertEqual(list(base.glob("release-build.*")), [])
            environment["RELEASE_CONTRACT"] = "v1"
            result = subprocess.run(["bash", "-e", "-c", produce_shell], env=environment, cwd=base, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertNotIn(FAKE_TOKEN.encode(), result.stdout+result.stderr)
            outputs = dict(line.split("=",1) for line in (base/"outputs").read_text().splitlines())
            self.assertEqual(outputs["image"], IMAGE_NS+"@sha256:"+"d"*64)
            self.assertEqual(outputs["artifact_name"], "release-manifest-123456-2")
            self.assertEqual(json.loads(Path(outputs["manifest_path"]).read_bytes())["output"]["image"], outputs["image"])
            commands = [json.loads(line) for line in records.read_text().splitlines()]
            self.assertEqual([args[0] for name,args in commands if name=="docker"], ["login","build","push","buildx"])
            environment["PRIVATE_DIR"] = outputs["private_dir"]
            cleaned = subprocess.run(["bash","-e","-c",cleanup_shell], env=environment, cwd=base, capture_output=True, timeout=5)
            self.assertEqual(cleaned.returncode,0,cleaned.stderr.decode())
            self.assertFalse(Path(outputs["private_dir"]).exists())
            self.assertEqual((unrelated/"keep").read_text(),"keep")
            self.assertEqual(action.count("steps.release.outputs.manifest_path"),2)
            self.assertIn("steps.manifest_upload.outputs.artifact-id", action)
            # Parse actual action structure: comments/no-op text cannot satisfy wiring.
            definition = yaml.safe_load(action)
            steps = {step["id"]: step for step in definition["runs"]["steps"] if "id" in step}
            upload = steps["manifest_upload"]
            self.assertEqual(upload["uses"], "actions/upload-artifact@65c4c4a1ddee5b72f698fdd19549f0f0fb45cf08")
            self.assertNotIn("run", upload)
            self.assertEqual(upload["if"], "${{ inputs.release_contract == 'v1' }}")
            options = upload["with"]
            self.assertEqual(options["path"], "${{ steps.release.outputs.manifest_path }}")
            self.assertEqual(options["name"], "${{ steps.release.outputs.artifact_name }}")
            self.assertEqual(options["if-no-files-found"], "error")
            self.assertIs(options["overwrite"], False)
            self.assertEqual(options["retention-days"], 7)
            self.assertEqual(definition["outputs"]["image"]["value"], "${{ steps.release.outputs.image || steps.publish.outputs.image }}")
            expected_environment = {
                "RELEASE_CONTRACT":"release_contract", "RELEASE_IMAGE_NAMESPACE":"release_image_namespace",
                "RELEASE_WORKFLOW_REF":"release_workflow_ref", "GITHUB_TOKEN":"github_token",
                "BACKEND_DOCKERFILE":"backend_dockerfile", "BACKEND_BUILD_CONTEXT":"backend_build_context",
                "HRAM_ENGINE_READ_TOKEN":"hram_engine_read_token", "EXISTING_IMAGE":"existing_image",
                "VITE_APP_MUI_LICENSE_KEY":"vite_app_mui_license_key",
                "VITE_SURVEY_APP_API_BASE_URL":"vite_survey_app_api_base_url",
                "VITE_SURVEY_APP_SITE_SLUG":"vite_survey_app_site_slug"}
            self.assertEqual(steps["release"]["env"], dict({key:"${{ inputs."+value+" }}" for key,value in expected_environment.items()}, ACTION_PATH="${{ github.action_path }}"))
            self.assertEqual(definition["outputs"]["artifact_id"]["value"], "${{ steps.manifest_upload.outputs.artifact-id }}")
            self.assertEqual(definition["outputs"]["manifest_path"]["value"], options["path"])
            cleanup = definition["runs"]["steps"][2]
            self.assertEqual(cleanup["if"], "${{ always() && inputs.release_contract == 'v1' }}")
            self.assertEqual(cleanup["env"]["PRIVATE_DIR"], "${{ steps.release.outputs.private_dir }}")



    def test_credentials_reach_only_their_necessary_command(self):
        stub = CommandStub()
        with tempfile.TemporaryDirectory() as directory:
            image, raw = release_build.produce(context(UNRELATED_RUNTIME_SECRET="fake-unrelated-runtime"), output_dir=Path(directory)/"output", run=stub)
        for args, environment, _, _, stdin in stub.calls:
            self.assertNotIn("GITHUB_TOKEN", environment)
            self.assertNotIn("UNRELATED_RUNTIME_SECRET", environment)
            if args[:2] == ["docker", "build"]:
                self.assertEqual(environment.get("HRAM_ENGINE_READ_TOKEN"), "synthetic-build-token")
                self.assertIn("id=hram_engine_token,env=HRAM_ENGINE_READ_TOKEN", args)
            else:
                self.assertNotIn("HRAM_ENGINE_READ_TOKEN", environment)
            self.assertEqual(stdin, FAKE_TOKEN.encode() if args[:2] == ["docker", "login"] else None)
            self.assertNotIn(FAKE_TOKEN, " ".join(args))
        self.assertNotIn(FAKE_TOKEN.encode(), raw)
        self.assertNotIn(b"fake-unrelated-runtime", raw)


    def test_private_build_control_forces_secret_exclusions_after_source_negations(self):
        Path(".dockerignore").write_text("node_modules\n!.env*\n", encoding="utf-8")
        source = Path("backend/Dockerfile").read_bytes()
        stub = CommandStub()
        seen = []
        def command(args, **kwargs):
            if args[:2] == ["docker","build"]:
                control = Path(args[args.index("-f")+1])
                self.assertEqual(control.read_bytes(), source)
                rules = Path(str(control)+".dockerignore").read_text(encoding="utf-8").splitlines()
                self.assertEqual(rules[:2], ["node_modules", "!.env*"])
                self.assertEqual(rules[-16:], [".env*", "**/.env*", ".ssh", "**/.ssh", ".secrets", "**/.secrets", "*.pem", "**/*.pem", "*.key", "**/*.key", "id_rsa*", "**/id_rsa*", "id_ed25519*", "**/id_ed25519*", ".git", "**/.git"])
                self.assertIn("**/.env*", rules[2:])
                self.assertIn("**/*.key", rules[2:])
                seen.append(control)
            return stub(args, **kwargs)
        with tempfile.TemporaryDirectory() as directory:
            release_build.produce(context(VITE_APP_MUI_LICENSE_KEY="fake-browser-license"), output_dir=Path(directory)/"output", run=command)
        self.assertEqual(len(seen),1)
        self.assertFalse(seen[0].exists())
        build = next(call for call in stub.calls if call[0][:2]==["docker","build"])
        self.assertIn("VITE_APP_MUI_LICENSE_KEY", build[0])
        self.assertNotIn("fake-browser-license", " ".join(build[0]))
        self.assertEqual(build[1]["VITE_APP_MUI_LICENSE_KEY"], "fake-browser-license")
        for args, environment, _, _, _ in stub.calls:
            if args[:2] != ["docker","build"]:
                self.assertNotIn("VITE_APP_MUI_LICENSE_KEY", environment)
        self.assertEqual(Path(".dockerignore").read_text(), "node_modules\n!.env*\n")


if __name__ == "__main__":
    unittest.main()
