"""WM-INF-122: a release that carries several images, and one-shot helper services, in the strict image target."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import prebuilt_target as pt
import release_manifest as rm
from test_prebuilt_target import DIGEST, IDENTITY, IMAGE, NAMESPACE, SIDECAR, TAG, TargetFixture

JAVA_NAMESPACE = "ghcr.io/bigler-webapps/sample-java-backend"
JAVA_DIGEST = "sha256:" + "e" * 64
JAVA_IMAGE = JAVA_NAMESPACE + "@" + JAVA_DIGEST
ALPINE = "docker.io/library/alpine@sha256:" + "f" * 64
ONE_SHOT = "init-app-volumes"
ADDITIONAL = (("java_backend", "java", JAVA_NAMESPACE),)
IDS = {"web": "a" * 64, "java_backend": "c" * 64, "db": "d" * 64, ONE_SHOT: "9" * 64}


def resolver(namespace, tag):
    digest = {NAMESPACE: DIGEST, JAVA_NAMESPACE: JAVA_DIGEST}[namespace]
    return {"repository": namespace, "tag": tag, "digest": digest}


def manifest(additional=True):
    return rm.produce_image_manifest(
        IDENTITY, expected_namespace=NAMESPACE, pushed_tag=TAG, resolve_pushed_image=resolver,
        additional_namespaces={"java": JAVA_NAMESPACE} if additional else None)


def rows(one_shot_state="exited", one_shot_exit=0):
    return [
        {"ID": IDS["web"], "Project": "sample", "Service": "web", "State": "running", "Health": "healthy", "ExitCode": 0},
        {"ID": IDS["java_backend"], "Project": "sample", "Service": "java_backend", "State": "running", "Health": "healthy", "ExitCode": 0},
        {"ID": IDS["db"], "Project": "sample", "Service": "db", "State": "running", "Health": "", "ExitCode": 0},
        {"ID": IDS[ONE_SHOT], "Project": "sample", "Service": ONE_SHOT, "State": one_shot_state, "Health": "", "ExitCode": one_shot_exit},
    ]


CONFIG = {"services": {
    "web": {"image": IMAGE}, "java_backend": {"image": JAVA_IMAGE}, "db": {"image": SIDECAR},
    ONE_SHOT: {"image": ALPINE, "restart": "no"},
}}


class Executor:
    """Compose/Docker stub for a four-service project: two released images, a database and one one-shot helper."""

    def __init__(self, *, config=None, ps=None, fail_up=False, up_ps=None):
        self.config = config or CONFIG
        self.ps = ps or rows()
        self.fail_up = fail_up
        self.up_ps = up_ps
        self.calls = []
        self.activated = None

    def run(self, argv, *, cwd, env, timeout, capture=False):
        self.calls.append(argv)
        joined = " ".join(argv)
        if "up" in argv:
            self.activated = Path(argv[argv.index("--file") + 1])
            if self.fail_up:
                return pt._CommandResult(1, b"")
        if "config --format json" in joined:
            if "--env-file" in argv:
                return pt._CommandResult(0, json.dumps(self.config, separators=(",", ":")).encode())
            return pt._CommandResult(0, Path(argv[argv.index("--file") + 1]).read_bytes())
        if "ps --all --format json" in joined:
            data = b"".join(json.dumps(row, separators=(",", ":")).encode() + b"\n" for row in self.ps)
            return pt._CommandResult(0, data)
        if "inspect" in argv and "image" not in argv:
            service = next(name for name, cid in IDS.items() if cid == argv[-1])
            image = {"web": IMAGE, "java_backend": JAVA_IMAGE, "db": SIDECAR, ONE_SHOT: ALPINE}[service]
            state = next(row["State"] for row in self.ps if row["Service"] == service)
            values = (image, state, False, None, "sample", service, cwd, str(self.activated))
            return pt._CommandResult(0, "".join(json.dumps(value) + "\n" for value in values).encode())
        if argv[3] == "ps":
            return pt._CommandResult(0, "\n".join(IDS.values()).encode("ascii"))
        return pt._CommandResult(0, b"")


class MultiImageFixture(TargetFixture):
    def setUp(self):
        super().setUp()
        self.target = replace(
            self.target, image_services=("web",), sidecar_images=(("db", SIDECAR), (ONE_SHOT, ALPINE)),
            migrate_service=None, additional_image_services=ADDITIONAL, one_shot_services=(ONE_SHOT,))
        self.raw = manifest()
        self.calls = []

        def recording(namespace, tag):
            self.calls.append((namespace, tag))
            return resolver(namespace, tag)
        self.resolver = recording

    def failure(self, **kwargs):
        with self.assertRaises(pt.PrebuiltTargetError) as caught:
            self.execute(**kwargs)
        return caught.exception.code


class MultiImageDeploymentTests(MultiImageFixture):
    def test_both_images_are_pinned_by_digest_and_the_one_shot_helper_passes(self):
        executor = Executor()
        result = self.execute(executor=executor)
        self.assertEqual(result.image, IMAGE)
        self.assertEqual(result.services, ("db", ONE_SHOT, "java_backend", "web"))
        self.assertEqual(self.calls, [(NAMESPACE, TAG), (JAVA_NAMESPACE, TAG)])
        # the compose override that selected the images carried each service's own digest (it is deleted after rendering,
        # so read what the rendered configuration check compared: every service matched its image)
        self.assertTrue(any("up" in call for call in executor.calls))

    def test_override_assigns_each_released_service_its_own_image(self):
        seen = {}

        class Capturing(Executor):
            def run(self, argv, **kwargs):
                if "config" in argv and "--env-file" in argv:
                    for index, part in enumerate(argv):
                        if part == "--file" and argv[index + 1].endswith("override.yaml"):
                            seen["override"] = Path(argv[index + 1]).read_text(encoding="utf-8")
                return super().run(argv, **kwargs)
        self.execute(executor=Capturing())
        override = seen["override"]
        self.assertIn(f'  web:\n    image: "{IMAGE}"\n', override)
        self.assertIn(f'  java_backend:\n    image: "{JAVA_IMAGE}"\n', override)
        self.assertNotIn(ONE_SHOT, override)

    def test_failed_one_shot_fails_the_deployment_by_name(self):
        code = self.failure(executor=Executor(ps=rows("exited", 3)))
        self.assertEqual(code, "one_shot_failed_init_app_volumes")

    def test_running_one_shot_fails_the_deployment_by_name(self):
        for state in ("running", "restarting", "created", "paused"):
            code = self.failure(executor=Executor(ps=rows(state, 0)))
            self.assertEqual(code, "one_shot_running_init_app_volumes", state)

    def test_one_shot_without_an_exit_code_is_not_a_success(self):
        broken = rows()
        del broken[3]["ExitCode"]
        self.assertEqual(self.failure(executor=Executor(ps=broken)), "one_shot_failed_init_app_volumes")

    def test_undeclared_stopped_service_still_fails(self):
        target = replace(self.target, one_shot_services=())
        self.assertEqual(self.failure(target=target, executor=Executor(ps=rows("exited", 0))), "compose_ps_state_invalid")

    def test_long_running_service_that_exited_is_not_excused_by_a_one_shot_declaration(self):
        stopped = rows()
        stopped[0]["State"] = "exited"
        self.assertEqual(self.failure(executor=Executor(ps=stopped)), "compose_ps_state_invalid")

    def test_failed_up_names_the_failed_one_shot_helper(self):
        executor = Executor(fail_up=True, ps=rows("exited", 1))
        self.assertEqual(self.failure(executor=executor), "one_shot_failed_init_app_volumes")

    def test_failed_up_without_a_failed_helper_keeps_the_plain_command_failure(self):
        executor = Executor(fail_up=True, ps=rows("exited", 0))
        self.assertEqual(self.failure(executor=executor), "command_failed")

    def test_one_shot_that_restarts_by_itself_is_rejected_before_activation(self):
        config = json.loads(json.dumps(CONFIG))
        config["services"][ONE_SHOT]["restart"] = "always"
        executor = Executor(config=config)
        self.assertEqual(self.failure(executor=executor), "one_shot_restart_invalid")
        self.assertFalse(any("up" in call for call in executor.calls))

    def test_one_shot_whose_deploy_restart_policy_restarts_it_is_rejected_before_activation(self):
        for condition in ("always", "on-failure", "any"):
            config = json.loads(json.dumps(CONFIG))
            config["services"][ONE_SHOT]["deploy"] = {"restart_policy": {"condition": condition}}
            executor = Executor(config=config)
            self.assertEqual(self.failure(executor=executor), "one_shot_restart_invalid", condition)
            self.assertFalse(any("up" in call for call in executor.calls))
        # the explicit "none" condition is the same as no restart policy
        config = json.loads(json.dumps(CONFIG))
        config["services"][ONE_SHOT]["deploy"] = {"restart_policy": {"condition": "none"}}
        self.assertEqual(self.execute(executor=Executor(config=config)).services, ("db", ONE_SHOT, "java_backend", "web"))

    def test_rendered_image_of_the_additional_service_must_match_the_manifest(self):
        config = json.loads(json.dumps(CONFIG))
        config["services"]["java_backend"]["image"] = IMAGE
        executor = Executor(config=config)
        self.assertEqual(self.failure(executor=executor), "compose_image_mismatch")
        self.assertFalse(any("up" in call for call in executor.calls))

    def test_service_missing_from_the_compose_control_is_rejected(self):
        config = json.loads(json.dumps(CONFIG))
        del config["services"]["java_backend"]
        self.assertEqual(self.failure(executor=Executor(config=config)), "compose_services_mismatch")


class SequencedExecutor(Executor):
    """`compose ps` answers a different set of rows on each call; the last one repeats."""

    def __init__(self, sequence, **kwargs):
        super().__init__(ps=sequence[-1], **kwargs)
        self.sequence = list(sequence)
        self.ps_calls = 0

    def run(self, argv, **kwargs):
        if "ps --all --format json" in " ".join(argv):
            self.ps_calls += 1
            self.ps = self.sequence.pop(0) if len(self.sequence) > 1 else self.sequence[0]
        return super().run(argv, **kwargs)


def with_health(rows_, service, health):
    return [dict(row, Health=health) if row["Service"] == service else row for row in rows_]


class HealthWaitTests(MultiImageFixture):
    """A service with a healthcheck is `starting` right after `up`: the deployment waits for it, bounded."""

    def setUp(self):
        super().setUp()
        self.pauses = []
        patcher = patch.object(pt, "_pause", side_effect=self.pauses.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_service_that_becomes_healthy_is_waited_for(self):
        executor = SequencedExecutor([with_health(rows(), "java_backend", "starting")] * 3 + [rows()])
        result = self.execute(executor=executor)
        self.assertEqual(result.services, ("db", ONE_SHOT, "java_backend", "web"))
        self.assertEqual((executor.ps_calls, self.pauses), (4, [pt.HEALTH_POLL_SECONDS] * 3))

    def test_a_service_that_never_becomes_healthy_times_out_by_a_fixed_code(self):
        seconds = iter(range(0, 10**6, 40))
        executor = SequencedExecutor([with_health(rows(), "java_backend", "starting")])
        self.assertEqual(self.failure(executor=executor, clock=lambda: float(next(seconds))), "health_wait_timeout")
        self.assertTrue(0 < executor.ps_calls <= 6)

    def test_an_unhealthy_service_fails_at_once_without_waiting(self):
        executor = SequencedExecutor([with_health(rows(), "java_backend", "unhealthy")])
        self.assertEqual(self.failure(executor=executor), "compose_ps_state_invalid")
        self.assertEqual((executor.ps_calls, self.pauses), (1, []))

    def test_a_real_failure_wins_over_a_service_that_is_still_starting(self):
        stopped = with_health(rows(), "java_backend", "starting")
        stopped[0]["State"] = "exited"
        executor = SequencedExecutor([stopped])
        self.assertEqual(self.failure(executor=executor), "compose_ps_state_invalid")
        self.assertEqual((executor.ps_calls, self.pauses), (1, []))
        # a failed one-shot helper wins too
        executor = SequencedExecutor([with_health(rows("exited", 5), "java_backend", "starting")])
        self.assertEqual(self.failure(executor=executor), "one_shot_failed_init_app_volumes")
        self.assertEqual(self.pauses, [])

    def test_without_a_pending_health_state_nothing_is_paused_and_ps_runs_once(self):
        executor = SequencedExecutor([rows()])
        self.execute(executor=executor)
        self.assertEqual((executor.ps_calls, self.pauses), (1, []))


class MultiImageManifestAtTheTargetTests(MultiImageFixture):
    def test_manifest_without_the_expected_additional_image_is_rejected_before_any_command(self):
        executor = Executor()
        self.assertEqual(self.failure(raw_manifest=manifest(additional=False), executor=executor), "manifest_invalid")
        self.assertEqual(executor.calls, [])

    def test_manifest_with_an_additional_image_the_target_does_not_name_is_rejected(self):
        target = replace(self.target, image_services=("web", "java_backend"), additional_image_services=(),
                         sidecar_images=(("db", SIDECAR), (ONE_SHOT, ALPINE)))
        executor = Executor()
        self.assertEqual(self.failure(target=target, executor=executor), "manifest_invalid")
        self.assertEqual(executor.calls, [])

    def test_an_image_from_another_namespace_is_rejected(self):
        target = replace(self.target, additional_image_services=(("java_backend", "java", "ghcr.io/bigler-webapps/other-backend"),))
        executor = Executor()
        self.assertEqual(self.failure(target=target, executor=executor), "manifest_invalid")
        self.assertEqual(executor.calls, [])

    def test_registry_digest_that_differs_from_the_manifest_is_rejected(self):
        def moved(namespace, tag):
            value = resolver(namespace, tag)
            if namespace == JAVA_NAMESPACE:
                value["digest"] = "sha256:" + "0" * 64
            return value
        executor = Executor()
        self.assertEqual(self.failure(resolve_pushed_image=moved, executor=executor), "manifest_invalid")
        self.assertEqual(executor.calls, [])

    def test_single_image_target_still_deploys_a_single_image_release(self):
        target = replace(self.target, additional_image_services=(), one_shot_services=(),
                         sidecar_images=(("db", SIDECAR),))
        config = {"services": {"web": {"image": IMAGE}, "db": {"image": SIDECAR}}}
        ps = [row for row in rows() if row["Service"] in ("web", "db")]

        class Single(Executor):
            def run(self, argv, **kwargs):
                if argv[3] == "ps" and "--quiet" in argv:
                    return pt._CommandResult(0, "\n".join((IDS["web"], IDS["db"])).encode("ascii"))
                return super().run(argv, **kwargs)
        result = self.execute(raw_manifest=manifest(additional=False), target=target, executor=Single(config=config, ps=ps))
        self.assertEqual(result.services, ("db", "web"))


class TargetSpecValidationTests(MultiImageFixture):
    def invalid(self, **changes):
        return self.failure(target=replace(self.target, **changes), executor=Executor())

    def test_additional_service_shapes(self):
        self.assertEqual(self.invalid(additional_image_services=[("java_backend", "java", JAVA_NAMESPACE)]),
                         "additional_services_invalid")
        self.assertEqual(self.invalid(additional_image_services=(("java_backend", "java"),)), "additional_services_invalid")
        self.assertEqual(self.invalid(additional_image_services=(("java_backend", "Java", JAVA_NAMESPACE),)),
                         "additional_services_invalid")
        self.assertEqual(self.invalid(additional_image_services=(("java_backend", "java", "ghcr.io/x/y"),)),
                         "additional_services_invalid")
        # the primary namespace cannot be reused for another image
        self.assertEqual(self.invalid(additional_image_services=(("java_backend", "java", NAMESPACE),)),
                         "additional_services_invalid")

    def test_additional_service_cannot_collide_with_other_services_or_keys(self):
        self.assertEqual(self.invalid(additional_image_services=(("web", "java", JAVA_NAMESPACE),)), "service_conflict")
        self.assertEqual(self.invalid(additional_image_services=(("db", "java", JAVA_NAMESPACE),)), "service_conflict")
        two = (("java_backend", "java", JAVA_NAMESPACE), ("other_service", "java", "ghcr.io/bigler-webapps/more-backend"))
        self.assertEqual(self.invalid(additional_image_services=two), "service_conflict")

    def test_one_shot_services_must_name_a_known_service_of_a_valid_shape(self):
        self.assertEqual(self.invalid(one_shot_services=["init"]), "one_shot_services_invalid")
        self.assertEqual(self.invalid(one_shot_services=("unknown",)), "one_shot_services_invalid")
        self.assertEqual(self.invalid(one_shot_services=(ONE_SHOT, ONE_SHOT)), "one_shot_services_invalid")
        self.assertEqual(self.invalid(one_shot_services=("db",)), "one_shot_services_invalid")
        long_name = "a" * 31
        self.assertEqual(self.invalid(sidecar_images=(("db", SIDECAR), (long_name, ALPINE)), one_shot_services=(long_name,)),
                         "one_shot_services_invalid")


class MultiImageManifestContractTests(unittest.TestCase):
    def parse(self, raw, **kwargs):
        return rm.validate_manifest(raw, expected=IDENTITY, expected_image_namespace=NAMESPACE,
                                    resolve_pushed_image=resolver, **kwargs)

    def test_two_image_manifest_carries_both_digests_and_validates(self):
        raw = manifest()
        value = json.loads(raw)
        self.assertEqual(value["output"], {"image": IMAGE, "additional_images": {"java": JAVA_IMAGE}})
        parsed = self.parse(raw, expected_additional_namespaces={"java": JAVA_NAMESPACE})
        self.assertEqual(parsed["output"]["additional_images"]["java"], JAVA_IMAGE)
        self.assertEqual(raw, rm._encode_manifest(value))

    def test_single_image_manifest_is_unchanged_and_still_valid(self):
        raw = manifest(additional=False)
        self.assertEqual(json.loads(raw)["output"], {"image": IMAGE})
        self.assertEqual(self.parse(raw)["output"], {"image": IMAGE})
        self.assertEqual(self.parse(raw, expected_additional_namespaces={})["output"], {"image": IMAGE})

    def test_missing_unexpected_or_foreign_images_are_rejected(self):
        two, one = manifest(), manifest(additional=False)
        cases = [
            (one, {"java": JAVA_NAMESPACE}, "image_output_fields_invalid"),
            (two, None, "image_output_fields_invalid"),
            (two, {"other": JAVA_NAMESPACE}, "additional_images_mismatch"),
            (two, {"java": "ghcr.io/bigler-webapps/other-backend"}, "image_namespace_mismatch"),
        ]
        for raw, expected, code in cases:
            with self.assertRaises(rm.ManifestError) as caught:
                self.parse(raw, expected_additional_namespaces=expected)
            self.assertEqual(str(caught.exception), code)

    def test_a_moved_additional_digest_is_rejected(self):
        def moved(namespace, tag):
            value = resolver(namespace, tag)
            if namespace == JAVA_NAMESPACE:
                value["digest"] = "sha256:" + "1" * 64
            return value
        with self.assertRaises(rm.ManifestError) as caught:
            rm.validate_manifest(manifest(), expected=IDENTITY, expected_image_namespace=NAMESPACE,
                                 resolve_pushed_image=moved, expected_additional_namespaces={"java": JAVA_NAMESPACE})
        self.assertEqual(str(caught.exception), "image_digest_mismatch")

    def test_producer_rejects_bad_additional_namespace_maps(self):
        for bad in ({"Java": JAVA_NAMESPACE}, {"java": "ghcr.io/x/y"}, {"java": NAMESPACE},
                    {"a": JAVA_NAMESPACE, "b": JAVA_NAMESPACE}, ["java"]):
            with self.assertRaises(rm.ManifestError):
                rm.produce_image_manifest(IDENTITY, expected_namespace=NAMESPACE, pushed_tag=TAG,
                                          resolve_pushed_image=resolver, additional_namespaces=bad)

    def test_producer_reads_each_image_from_the_registry_not_from_the_caller(self):
        def wrong_tag(namespace, tag):
            return {"repository": namespace, "tag": "latest", "digest": DIGEST}
        with self.assertRaises(rm.ManifestError):
            rm.produce_image_manifest(IDENTITY, expected_namespace=NAMESPACE, pushed_tag=TAG,
                                      resolve_pushed_image=wrong_tag, additional_namespaces={"java": JAVA_NAMESPACE})

    def test_additional_images_are_not_accepted_for_a_static_expectation(self):
        with self.assertRaises(rm.ManifestError):
            rm.validate_manifest_structure(
                manifest(), expected=IDENTITY,
                expected_static_artifact=rm.StaticArtifactSelection(rm.static_artifact_name(IDENTITY), 5),
                expected_additional_namespaces={"java": JAVA_NAMESPACE})


if __name__ == "__main__":
    unittest.main()
