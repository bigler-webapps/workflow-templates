"""WM-INF-122: the release-contract-v1 producer builds several images of one release."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_build
import release_manifest as rm
from test_release_build_action import ACTION, FAKE_TOKEN, IMAGE_NS, SHA, CommandStub, context

JAVA_NS = "ghcr.io/bigler-webapps/sample-java-backend"
ENGINE_TOKEN = "synthetic-build-token"
DIGESTS = {IMAGE_NS: "sha256:" + "a" * 64, JAVA_NS: "sha256:" + "b" * 64}
SPEC = [{"key": "java", "namespace": JAVA_NS, "dockerfile": "java_backend/Dockerfile", "context": "java_backend"}]


class PerImageStub(CommandStub):
    """Registry inspect answers with the digest of the namespace asked for."""

    def __call__(self, argv, *, env, timeout=release_build.TIMEOUT_SECONDS, capture=True, input_bytes=None):
        if argv[:4] == ["docker", "buildx", "imagetools", "inspect"]:
            self.calls.append((list(argv), dict(env), timeout, capture, input_bytes))
            namespace = argv[4].rsplit(":", 1)[0]
            return json.dumps({"name": argv[4], "digest": DIGESTS[namespace]}).encode("ascii")
        return super().__call__(argv, env=env, timeout=timeout, capture=capture, input_bytes=input_bytes)


class MultiImageProducerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = tempfile.TemporaryDirectory()
        self.old_cwd = Path.cwd()
        os.chdir(self.fixture.name)
        Path("backend").mkdir()
        Path("backend/Dockerfile").write_text("FROM scratch\nCOPY . /app\n", encoding="utf-8")
        Path("java_backend").mkdir()
        Path("java_backend/Dockerfile").write_text("FROM scratch\nCOPY . /app\n", encoding="utf-8")
        self.parent = Path(tempfile.mkdtemp())

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.fixture.cleanup()

    def produce(self, stub=None, **overrides):
        stub = stub or PerImageStub()
        env = context(RELEASE_ADDITIONAL_IMAGES=json.dumps(SPEC), **overrides)
        output = self.parent / "release-output"
        image, raw = release_build.produce(env, output_dir=output, run=stub)
        return stub, image, raw, output

    def test_both_images_are_built_pushed_and_pinned_by_the_digest_the_registry_reports(self):
        stub, image, raw, output = self.produce()
        identity = rm.ReleaseIdentity("bigler-webapps/sample-app", SHA, "refs/heads/develop",
                                      ".github/workflows/release.yml", 123456, 2)
        tag = rm.release_image_tag(identity)
        self.assertEqual(image, f"{IMAGE_NS}@{DIGESTS[IMAGE_NS]}")
        value = json.loads(raw)
        self.assertEqual(value["output"], {"image": image,
                                           "additional_images": {"java": f"{JAVA_NS}@{DIGESTS[JAVA_NS]}"}})
        self.assertEqual((output / "release.json").read_bytes(), raw)
        # one release, one tag: the same source commit built twice, never a tag of one image applied to the other
        docker = [call[0] for call in stub.calls if call[0][0] == "docker"]
        self.assertEqual([argv[:2] for argv in docker],
                         [["docker", "login"], ["docker", "build"], ["docker", "push"],
                          ["docker", "build"], ["docker", "push"], ["docker", "buildx"], ["docker", "buildx"]])
        primary_build, _, java_build, java_push = docker[1:5]
        self.assertEqual(primary_build[primary_build.index("-t") + 1], f"{IMAGE_NS}:{tag}")
        self.assertEqual(java_build[java_build.index("-t") + 1], f"{JAVA_NS}:{tag}")
        self.assertEqual(java_push[2], f"{JAVA_NS}:{tag}")
        self.assertEqual(java_build[-1], "java_backend")
        self.assertIn("--pull", java_build)
        # registry reads for both images, one per namespace
        inspected = [argv[4] for argv in docker[5:]]
        self.assertEqual(inspected, [f"{IMAGE_NS}:{tag}", f"{JAVA_NS}:{tag}"])

    def test_engine_token_is_a_build_secret_of_the_primary_image_only_and_never_in_output(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            stub, image, raw, output = self.produce()
        builds = [call for call in stub.calls if call[0][:2] == ["docker", "build"]]
        primary, java = builds
        self.assertIn("--secret", primary[0])
        self.assertEqual(primary[0][primary[0].index("--secret") + 1], "id=hram_engine_token,env=HRAM_ENGINE_READ_TOKEN")
        self.assertEqual(primary[1]["HRAM_ENGINE_READ_TOKEN"], ENGINE_TOKEN)
        # the second image never sees the engine token, a Vite value or any secret mount
        # the engine token is only ever a BuildKit secret (never a build argument, so never a layer's metadata)
        build_args = [primary[0][index + 1] for index, part in enumerate(primary[0]) if part == "--build-arg"]
        self.assertTrue(build_args)
        self.assertTrue(all(name.startswith("VITE_") and "=" not in name for name in build_args), build_args)
        self.assertEqual([part for part in primary[0] if "HRAM_ENGINE" in part and part != "id=hram_engine_token,env=HRAM_ENGINE_READ_TOKEN"], [])
        self.assertNotIn("--secret", java[0])
        self.assertNotIn("--build-arg", java[0])
        self.assertNotIn("HRAM_ENGINE_READ_TOKEN", java[1])
        self.assertNotIn("GITHUB_TOKEN", java[1])
        for argv, env, _, _, _ in stub.calls:
            self.assertFalse(any(ENGINE_TOKEN in part or FAKE_TOKEN in part for part in argv))
        written = raw + (output / "image.txt").read_bytes() + (output / "release.json").read_bytes()
        for secret in (ENGINE_TOKEN, FAKE_TOKEN):
            self.assertNotIn(secret.encode(), written)
            self.assertNotIn(secret, stdout.getvalue() + stderr.getvalue())

    def test_every_build_gets_its_own_protected_dockerfile_copy(self):
        stub, *_ = self.produce()
        files = [call[0][call[0].index("-f") + 1] for call in stub.calls if call[0][:2] == ["docker", "build"]]
        self.assertEqual([Path(name).name for name in files], ["Release.Dockerfile", "Release.java.Dockerfile"])

    def test_single_image_release_is_unchanged_without_the_input(self):
        stub = PerImageStub()
        output = self.parent / "single"
        image, raw = release_build.produce(context(), output_dir=output, run=stub)
        self.assertEqual(json.loads(raw)["output"], {"image": image})
        self.assertEqual(len([c for c in stub.calls if c[0][:2] == ["docker", "build"]]), 1)

    def test_invalid_additional_specs_fail_before_login_or_output(self):
        bad = [
            "not json",
            json.dumps({"key": "java"}),
            json.dumps([]),
            json.dumps([dict(SPEC[0], extra="x")]),
            json.dumps([{k: v for k, v in SPEC[0].items() if k != "context"}]),
            json.dumps([dict(SPEC[0], key="Java")]),
            json.dumps([dict(SPEC[0], namespace="ghcr.io/other-owner/sample-java-backend")]),
            json.dumps([dict(SPEC[0], namespace=IMAGE_NS)]),
            json.dumps([dict(SPEC[0], namespace="ghcr.io/bigler-webapps/notabackendname")]),
            json.dumps([SPEC[0], dict(SPEC[0], namespace="ghcr.io/bigler-webapps/more-backend")]),
            json.dumps([dict(SPEC[0], dockerfile="../Dockerfile")]),
            json.dumps([dict(SPEC[0], context="--help")]),
            json.dumps([dict(SPEC[0], dockerfile=".env/Dockerfile")]),
            json.dumps([dict(SPEC[0], dockerfile=7)]),
            "[" + ",".join(json.dumps(SPEC[0]) for _ in range(200)) + "]",
        ]
        for spec in bad:
            stub = PerImageStub()
            output = self.parent / "bad-output"
            with self.assertRaises(release_build.ReleaseBuildError, msg=spec[:40]):
                release_build.produce(context(RELEASE_ADDITIONAL_IMAGES=spec), output_dir=output, run=stub)
            self.assertEqual([c for c in stub.calls if c[0][0] == "docker"], [], spec[:40])
            self.assertFalse(output.exists(), spec[:40])

    def test_failed_additional_build_publishes_no_manifest(self):
        class FailJava(PerImageStub):
            def __call__(self, argv, **kwargs):
                if argv[:2] == ["docker", "build"] and "java_backend" in argv:
                    self.calls.append((list(argv), {}, 0, False, None))
                    raise release_build.ReleaseBuildError("command_failed", detail="exit_1")
                return super().__call__(argv, **kwargs)
        output = self.parent / "failed"
        with self.assertRaises(release_build.ReleaseBuildError) as caught:
            release_build.produce(context(RELEASE_ADDITIONAL_IMAGES=json.dumps(SPEC)), output_dir=output, run=FailJava())
        self.assertEqual((str(caught.exception), caught.exception.stage), ("release_production_failed", "docker_build"))
        self.assertFalse(output.exists())


class ActionWiringTests(unittest.TestCase):
    def test_action_passes_the_additional_images_to_the_producer_only(self):
        action = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
        self.assertEqual(action["inputs"]["release_additional_images"]["default"], "")
        steps = {step.get("id"): step for step in action["runs"]["steps"]}
        self.assertEqual(steps["release"]["env"]["RELEASE_ADDITIONAL_IMAGES"], "${{ inputs.release_additional_images }}")
        self.assertNotIn("RELEASE_ADDITIONAL_IMAGES", steps["publish"]["env"])


if __name__ == "__main__":
    unittest.main()
