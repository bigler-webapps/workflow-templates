"""Behavioral tests for the offline release-manifest contract."""

from copy import deepcopy
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
import io
import traceback
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_manifest as rm


IDENTITY = rm.ReleaseIdentity(
    repository="bigler-webapps/sample-app",
    sha="0123456789abcdef0123456789abcdef01234567",
    ref="refs/heads/develop",
    workflow=".github/workflows/release.yml",
    run_id=123456,
    attempt=2,
)
NAMESPACE = "ghcr.io/bigler-webapps/sample-app-backend"
TAG = "release-123456-2-0123456789abcdef0123456789abcdef01234567"
DIGEST = "sha256:" + "a" * 64
IMAGE = f"{NAMESPACE}@{DIGEST}"
BUNDLE = b"static bundle fixture\x00with bytes"
DEFAULT_RESOLUTION = object()


def expected_image_manifest():
    return {
        "version": 1,
        "kind": "image",
        "source_repository": "bigler-webapps/sample-app",
        "source_sha": "0123456789abcdef0123456789abcdef01234567",
        "source_ref": "refs/heads/develop",
        "source_workflow": ".github/workflows/release.yml",
        "run_id": 123456,
        "run_attempt": 2,
        "output": {"image": IMAGE},
    }


def registry_read(namespace, tag):
    return {"repository":namespace,"tag":tag,"digest":DIGEST}


def wire(value):
    """Independent test input/oracle encoder, never a production helper."""
    return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode("ascii")


def expected_static_manifest():
    value=expected_image_manifest()
    value["kind"]="static"
    value["output"]={"artifact_name":"release-static-123456-2","artifact_id":998877,
                     "byte_length":len(BUNDLE),"sha256":hashlib.sha256(BUNDLE).hexdigest()}
    return value


class ReleaseManifestTests(unittest.TestCase):
    def test_pure_image_preflight_has_no_registry_effect_and_preserves_binding_requirement(self):
        raw = wire(expected_image_manifest())
        with patch.object(rm,"_resolved_image",side_effect=AssertionError("pure preflight must not read registry")):
            self.assertEqual(rm.validate_manifest_structure(raw,expected=IDENTITY,
                expected_image_namespace=NAMESPACE),expected_image_manifest())
            with self.assertRaises(rm.ManifestError):
                rm.validate_manifest_structure(raw,expected=replace(IDENTITY,sha="f"*40),expected_image_namespace=NAMESPACE)
        calls=[]
        def mismatch(namespace,tag):
            calls.append((namespace,tag));return {"repository":namespace,"tag":tag,"digest":"sha256:"+"b"*64}
        with self.assertRaises(rm.ManifestError):
            rm.validate_manifest(raw,expected=IDENTITY,expected_image_namespace=NAMESPACE,resolve_pushed_image=mismatch)
        self.assertEqual(calls,[(NAMESPACE,TAG)])

    def test_pure_static_preflight_retains_selected_artifact_contract(self):
        raw=wire(expected_static_manifest())
        selected=rm.StaticArtifactSelection("release-static-123456-2",998877)
        self.assertEqual(rm.validate_manifest_structure(raw,expected=IDENTITY,
            expected_static_artifact=selected),expected_static_manifest())
        with self.assertRaises(rm.ManifestError):
            rm.validate_manifest_structure(raw,expected=IDENTITY,
                expected_static_artifact=rm.StaticArtifactSelection(selected.name,998878))

    def make_image(self, result=DEFAULT_RESOLUTION):
        calls = []

        def resolver(namespace, tag):
            calls.append((namespace, tag))
            if isinstance(result, Exception):
                raise result
            return {"repository": NAMESPACE, "tag": TAG, "digest": DIGEST} if result is DEFAULT_RESOLUTION else result

        raw = rm.produce_image_manifest(
            IDENTITY, expected_namespace=NAMESPACE, pushed_tag=TAG,
            resolve_pushed_image=resolver,
        )
        return raw, calls

    def make_static(self, bundle=BUNDLE):
        return rm.produce_static_manifest(
            IDENTITY, artifact_id=998877, bundle=bundle, bundle_length=len(BUNDLE),
        )

    def test_image_roundtrip_uses_exact_registry_request_and_independent_oracle(self):
        raw, calls = self.make_image()
        self.assertEqual(calls, [(NAMESPACE, TAG)])
        self.assertEqual(raw,wire(expected_image_manifest()))
        self.assertEqual(json.loads(raw), expected_image_manifest())
        self.assertEqual(rm.validate_manifest(raw, expected=IDENTITY, expected_image_namespace=NAMESPACE,resolve_pushed_image=registry_read), expected_image_manifest())

    def test_producer_canonical_serialization_matches_independent_literal(self):
        raw,_=self.make_image()
        self.assertEqual(raw,b'{"kind":"image","output":{"image":"'+IMAGE.encode()+b'"},"run_attempt":2,"run_id":123456,"source_ref":"refs/heads/develop","source_repository":"bigler-webapps/sample-app","source_sha":"0123456789abcdef0123456789abcdef01234567","source_workflow":".github/workflows/release.yml","version":1}')
        self.assertLessEqual(len(raw),rm.MAX_MANIFEST_BYTES)

    def test_registry_missing_malformed_mismatch_and_exception_fail_safely(self):
        cases = [{}, {"repository": NAMESPACE, "tag": TAG, "digest": "tag"},
                 {"repository": "ghcr.io/other/app-backend", "tag": TAG, "digest": DIGEST},
                 {"repository": NAMESPACE, "tag": "other", "digest": DIGEST}, RuntimeError("synthetic-secret")]
        for result in cases:
            with self.subTest(result=type(result).__name__):
                with self.assertRaises(rm.ManifestError) as caught:
                    self.make_image(result)
                self.assertNotIn("synthetic-secret", str(caught.exception))

    def test_caller_digest_alone_is_not_a_registry_result(self):
        with self.assertRaises(rm.ManifestError):
            rm.produce_image_manifest(IDENTITY, expected_namespace=NAMESPACE, pushed_tag=TAG, resolve_pushed_image=lambda *_: DIGEST)

    def test_validator_binds_expected_source_run_workflow_ref_and_namespace(self):
        raw, _ = self.make_image()
        for field, replacement in (("source_repository", "elsewhere/app"), ("source_sha", "f" * 40),
                                   ("source_ref", "refs/heads/main"), ("source_workflow", ".github/workflows/other.yml"),
                                   ("run_id", 123457), ("run_attempt", 3)):
            with self.subTest(field=field):
                forged = expected_image_manifest()
                forged[field] = replacement
                with self.assertRaises(rm.ManifestError):
                    rm.validate_manifest(wire(forged), expected=IDENTITY, expected_image_namespace=NAMESPACE,resolve_pushed_image=registry_read)
        with self.assertRaisesRegex(rm.ManifestError, "image_namespace_mismatch"):
            rm.validate_manifest(raw, expected=IDENTITY, expected_image_namespace="ghcr.io/other/app-backend")

    def test_static_roundtrip_binds_selected_artifact_and_bundle_hash(self):
        raw = self.make_static()
        self.assertEqual(raw,wire(expected_static_manifest()))
        selected = rm.StaticArtifactSelection(rm.static_artifact_name(IDENTITY), 998877)
        parsed = rm.validate_manifest(raw, expected=IDENTITY, expected_static_artifact=selected)
        self.assertEqual(parsed["output"], {
            "artifact_name": "release-static-123456-2", "artifact_id": 998877,
            "byte_length": len(BUNDLE), "sha256": hashlib.sha256(BUNDLE).hexdigest(),
        })
        self.assertEqual(rm.verify_static_bundle(BytesIO(BUNDLE), manifest=parsed), (len(BUNDLE), hashlib.sha256(BUNDLE).hexdigest()))
        with self.assertRaisesRegex(rm.ManifestError, "static_bundle_hash_mismatch"):
            rm.verify_static_bundle(BUNDLE[:-1] + b"!", manifest=parsed)
        with self.assertRaises(rm.ManifestError):
            rm.validate_manifest(raw, expected=IDENTITY, expected_static_artifact=rm.StaticArtifactSelection("release-static-123456-2", 998878))

    def test_bundle_rejects_truncation_extra_data_and_oversize(self):
        with self.assertRaisesRegex(rm.ManifestError, "bundle_length_mismatch"):
            rm.produce_static_manifest(IDENTITY, artifact_id=1, bundle=BUNDLE[:-1], bundle_length=len(BUNDLE))
        with self.assertRaisesRegex(rm.ManifestError, "bundle_length_mismatch"):
            rm.produce_static_manifest(IDENTITY, artifact_id=1, bundle=BUNDLE + b"x", bundle_length=len(BUNDLE))
        with self.assertRaisesRegex(rm.ManifestError, "bundle_length_invalid"):
            rm.produce_static_manifest(IDENTITY, artifact_id=1, bundle=b"", bundle_length=rm.MAX_BUNDLE_BYTES + 1)

    def test_strict_parser_rejects_invalid_utf8_json_duplicates_unknown_nested_bool_and_controls(self):
        malformed = [b"\xff", b"{", b'{"version":1,"version":1}',
                     wire({**expected_image_manifest(), "extra": 1}),
                     wire({**expected_image_manifest(), "version": True}),
                     wire({**expected_image_manifest(), "output": {"image": IMAGE, "extra": {"x": 1}}}),
                     b'{"kind":"image","output":{},"run_attempt":2,"run_id":1,"source_ref":"refs/heads/develop","source_repository":"x/y","source_sha":"0123456789abcdef0123456789abcdef01234567","source_workflow":".github/workflows/release.yml","version":NaN}']
        for raw in malformed:
            with self.subTest(raw=raw[:20]):
                with self.assertRaises(rm.ManifestError):
                    rm.validate_manifest(raw, expected=IDENTITY, expected_image_namespace=NAMESPACE,resolve_pushed_image=registry_read)
        controlled = expected_image_manifest()
        controlled["source_repository"] = "x\ny"
        with self.assertRaises(rm.ManifestError):
            rm.validate_manifest(wire(controlled), expected=IDENTITY, expected_image_namespace=NAMESPACE,resolve_pushed_image=registry_read)

    def test_estate_repository_case_and_underscores_survive_exact_binding(self):
        for repository in ("bigler-webapps/survey_app", "bigler-webapps/survey_contact_app", "bigler-webapps/Gustav"):
            identity=replace(IDENTITY, repository=repository)
            raw=rm.produce_image_manifest(identity,expected_namespace=NAMESPACE,pushed_tag=TAG,
                resolve_pushed_image=lambda namespace,tag:{"repository":namespace,"tag":tag,"digest":DIGEST})
            self.assertEqual(json.loads(raw)["source_repository"],repository)
            self.assertEqual(rm.validate_manifest(raw,expected=identity,expected_image_namespace=NAMESPACE,resolve_pushed_image=registry_read)["source_repository"],repository)
        calls=[]
        for bad in ("bigler-webapps/..", "bigler-webapps/.", "bigler-webapps/app;touch", "bigler-webapps/app\n"):
            with self.assertRaises(rm.ManifestError):
                rm.produce_image_manifest(replace(IDENTITY,repository=bad),expected_namespace=NAMESPACE,
                    pushed_tag=TAG,resolve_pushed_image=lambda *args:calls.append(args))
        self.assertEqual(calls,[])

    def test_output_kind_is_selected_by_exactly_one_external_expectation(self):
        raw,_=self.make_image()
        selected=rm.StaticArtifactSelection("release-static-123456-2",998877)
        for kwargs in ({},{"expected_image_namespace":NAMESPACE,"expected_static_artifact":selected}):
            with self.assertRaisesRegex(rm.ManifestError,"^exactly_one_output_expectation_required$"):
                rm.validate_manifest(raw,expected=IDENTITY,**kwargs)
        with self.assertRaises(rm.ManifestError):
            rm.validate_manifest(raw,expected=IDENTITY,expected_static_artifact=selected)
        with self.assertRaises(rm.ManifestError):
            rm.validate_manifest(self.make_static(),expected=IDENTITY,expected_image_namespace=NAMESPACE,resolve_pushed_image=registry_read)

    def test_bad_expected_identity_is_rejected_before_registry_read(self):
        calls=[]
        for identity in (replace(IDENTITY,run_id=True),replace(IDENTITY,attempt=True),
                         replace(IDENTITY,sha="f"*39),replace(IDENTITY,ref="refs/pull/1/merge"),
                         replace(IDENTITY,workflow=".github/workflows/../bad.yml")):
            with self.assertRaises(rm.ManifestError):
                rm.produce_image_manifest(identity,expected_namespace=NAMESPACE,pushed_tag=TAG,
                    resolve_pushed_image=lambda *args:calls.append(args))
        self.assertEqual(calls,[])

    def test_manifest_size_mutable_images_and_independent_artifact_name_are_rejected(self):
        with self.assertRaisesRegex(rm.ManifestError,"^manifest_too_large$"):
            rm.validate_manifest(b" "*(rm.MAX_MANIFEST_BYTES+1),expected=IDENTITY,expected_image_namespace=NAMESPACE,resolve_pushed_image=registry_read)
        for image in (NAMESPACE+":latest",NAMESPACE+"@sha256:"+"a"*63,NAMESPACE+"@sha256:"+"A"*64,
                      "ghcr.io/other/app-backend@"+DIGEST):
            data=expected_image_manifest();data["output"]["image"]=image
            with self.assertRaises(rm.ManifestError):
                rm.validate_manifest(wire(data),expected=IDENTITY,expected_image_namespace=NAMESPACE,resolve_pushed_image=registry_read)
        for selection in (rm.StaticArtifactSelection("release-static-123456-1",998877),
                          rm.StaticArtifactSelection("release-static-123456-2",True)):
            with self.assertRaises(rm.ManifestError):
                rm.validate_manifest(self.make_static(),expected=IDENTITY,expected_static_artifact=selection)

    def test_streaming_reads_are_bounded_and_reject_wrong_return_types(self):
        class RecordingStream(BytesIO):
            sizes=[]
            def read(inner,size):
                inner.sizes.append(size)
                return super().read(size)
        stream=RecordingStream(BUNDLE)
        raw=rm.produce_static_manifest(IDENTITY,artifact_id=998877,bundle=stream,bundle_length=len(BUNDLE))
        self.assertEqual(json.loads(raw)["output"]["sha256"],hashlib.sha256(BUNDLE).hexdigest())
        self.assertTrue(stream.sizes)
        self.assertTrue(all(0<size<=min(rm.CHUNK_BYTES,len(BUNDLE)+1) for size in stream.sizes))
        class BrokenStream:
            def __init__(inner,result):inner.result=result
            def read(inner,size):return inner.result
        for result in (None,"",b"x"*(rm.CHUNK_BYTES+1)):
            with self.assertRaises(rm.ManifestError):
                rm.produce_static_manifest(IDENTITY,artifact_id=1,bundle=BrokenStream(result),bundle_length=0)
        with self.assertRaisesRegex(rm.ManifestError,"^bundle_length_mismatch$"):
            rm.produce_static_manifest(IDENTITY,artifact_id=1,bundle=BytesIO(BUNDLE+b"extra"),bundle_length=len(BUNDLE))

    def test_actual_resolver_and_stream_failures_have_safe_tracebacks_and_no_logs(self):
        secret="synthetic-secret-observed-at-boundary"
        class FailingRead:
            def read(inner,size):raise OSError(secret)
        class FailingMetadata(dict):
            def get(inner,key):raise RuntimeError(secret)
        def fail_resolver(*args):raise RuntimeError(secret)
        operations=(lambda:self.make_image(RuntimeError(secret)),
            lambda:self.make_image(rm.ManifestError(secret)),
            lambda:self.make_image(FailingMetadata(repository=NAMESPACE)),
            lambda:rm.produce_static_manifest(IDENTITY,artifact_id=1,bundle=FailingRead(),bundle_length=1),
            lambda:rm.validate_manifest(b'{"'+secret.encode()+b'":',expected=IDENTITY,expected_image_namespace=NAMESPACE,resolve_pushed_image=registry_read))
        for operation in operations:
            stdout,stderr=io.StringIO(),io.StringIO()
            with redirect_stdout(stdout),redirect_stderr(stderr),self.assertRaises(rm.ManifestError) as caught:
                operation()
            self.assertEqual(stdout.getvalue()+stderr.getvalue(),"")
            self.assertNotIn(secret,str(caught.exception))
            self.assertNotIn(secret,"".join(traceback.format_exception(caught.exception)))
        raw=rm.produce_image_manifest(IDENTITY,expected_namespace=NAMESPACE,pushed_tag=TAG,
            resolve_pushed_image=lambda namespace,tag:{"repository":namespace,"tag":tag,"digest":DIGEST,"token":secret})
        self.assertNotIn(secret.encode(),raw)
        self.assertEqual(set(json.loads(raw)),set(expected_image_manifest()))
        self.assertEqual(set(json.loads(raw)["output"]),{"image"})

    def test_registry_digest_is_taken_from_each_valid_resolution(self):
        for character in ("b","c"):
            digest="sha256:"+character*64
            raw,calls=self.make_image({"repository":NAMESPACE,"tag":TAG,"digest":digest})
            expected=expected_image_manifest();expected["output"]["image"]=NAMESPACE+"@"+digest
            self.assertEqual(raw,wire(expected))
            self.assertEqual(calls,[(NAMESPACE,TAG)])

    def test_release_registry_tag_binds_sha_run_and_attempt_before_lookup(self):
        self.assertEqual(rm.release_image_tag(IDENTITY),TAG)
        for changed in (replace(IDENTITY,sha="f"*40),replace(IDENTITY,run_id=123457),replace(IDENTITY,attempt=3)):
            calls=[]
            with self.assertRaisesRegex(rm.ManifestError,"^release_image_tag_mismatch$"):
                rm.produce_image_manifest(changed,expected_namespace=NAMESPACE,pushed_tag=TAG,
                    resolve_pushed_image=lambda *args:calls.append(args))
            self.assertEqual(calls,[])
            selected=rm.release_image_tag(changed)
            raw=rm.produce_image_manifest(changed,expected_namespace=NAMESPACE,pushed_tag=selected,
                resolve_pushed_image=lambda namespace,tag:{"repository":namespace,"tag":tag,"digest":DIGEST})
            validated=rm.validate_manifest(raw,expected=changed,expected_image_namespace=NAMESPACE,resolve_pushed_image=registry_read)
            self.assertEqual(validated["source_sha"],changed.sha)
            self.assertEqual(validated["run_id"],changed.run_id)
            self.assertEqual(validated["run_attempt"],changed.attempt)

    def test_static_output_reflects_different_actual_bytes_and_selected_ids(self):
        for bundle,artifact_id in ((b"!"*len(BUNDLE),998878),(b"?"*len(BUNDLE),998879)):
            raw=rm.produce_static_manifest(IDENTITY,artifact_id=artifact_id,bundle=BytesIO(bundle),bundle_length=len(bundle))
            expected=expected_static_manifest();expected["output"]["artifact_id"]=artifact_id
            expected["output"]["sha256"]=hashlib.sha256(bundle).hexdigest()
            self.assertEqual(raw,wire(expected))
            selected=rm.StaticArtifactSelection("release-static-123456-2",artifact_id)
            parsed=rm.validate_manifest(raw,expected=IDENTITY,expected_static_artifact=selected)
            self.assertEqual(rm.verify_static_bundle(bundle,manifest=parsed),(len(bundle),hashlib.sha256(bundle).hexdigest()))
            with self.assertRaises(rm.ManifestError):
                rm.verify_static_bundle(BUNDLE,manifest=parsed)

    def test_consumer_independently_resolves_and_checks_the_selected_release_digest(self):
        raw,_=self.make_image()
        with self.assertRaisesRegex(rm.ManifestError,"^registry_reader_required$"):
            rm.validate_manifest(raw,expected=IDENTITY,expected_image_namespace=NAMESPACE)
        calls=[]
        def different_digest(namespace,tag):
            calls.append((namespace,tag))
            return {"repository":namespace,"tag":tag,"digest":"sha256:"+"b"*64}
        with self.assertRaisesRegex(rm.ManifestError,"^image_digest_mismatch$"):
            rm.validate_manifest(raw,expected=IDENTITY,expected_image_namespace=NAMESPACE,resolve_pushed_image=different_digest)
        self.assertEqual(calls,[(NAMESPACE,TAG)])
        forged=expected_image_manifest();forged["output"]["image"]=NAMESPACE+"@sha256:"+"f"*64
        with self.assertRaisesRegex(rm.ManifestError,"^image_digest_mismatch$"):
            rm.validate_manifest(wire(forged),expected=IDENTITY,expected_image_namespace=NAMESPACE,resolve_pushed_image=registry_read)

    def test_large_actual_stream_is_chunked_and_actual_over_limit_input_is_rejected(self):
        payload=b"x"*(2*rm.CHUNK_BYTES+17)
        class Recording(BytesIO):
            def __init__(inner,data):super().__init__(data);inner.sizes=[]
            def read(inner,size):inner.sizes.append(size);return super().read(size)
        stream=Recording(payload)
        raw=rm.produce_static_manifest(IDENTITY,artifact_id=1,bundle=stream,bundle_length=len(payload))
        self.assertEqual(json.loads(raw)["output"]["sha256"],hashlib.sha256(payload).hexdigest())
        self.assertGreaterEqual(len(stream.sizes),4)
        self.assertTrue(all(0<size<=rm.CHUNK_BYTES for size in stream.sizes))
        class Generated:
            def __init__(inner):inner.remaining=rm.MAX_BUNDLE_BYTES+1;inner.total=0;inner.calls=0
            def read(inner,size):
                self.assertGreater(size,0);self.assertLessEqual(size,rm.CHUNK_BYTES)
                count=min(size,inner.remaining);inner.remaining-=count;inner.total+=count;inner.calls+=1
                return b"x"*count
        oversized=Generated()
        with self.assertRaisesRegex(rm.ManifestError,"^bundle_length_mismatch$"):
            rm.produce_static_manifest(IDENTITY,artifact_id=1,bundle=oversized,bundle_length=rm.MAX_BUNDLE_BYTES)
        self.assertEqual(oversized.total,rm.MAX_BUNDLE_BYTES+1)
        self.assertEqual(oversized.remaining,0)
        self.assertGreater(oversized.calls,1)

    def test_validator_rejects_noncanonical_bytes_before_registry_read(self):
        data=expected_image_manifest()
        canonical=wire(data)
        malformed=(json.dumps(data).encode(),canonical+b"\n",wire(data).replace(b'"kind":"image",',b'"kind": "image",'))
        for raw in malformed:
            calls=[]
            with self.assertRaisesRegex(rm.ManifestError,"^manifest_noncanonical$"):
                rm.validate_manifest(raw,expected=IDENTITY,expected_image_namespace=NAMESPACE,
                    resolve_pushed_image=lambda *args:calls.append(args))
            self.assertEqual(calls,[])

if __name__ == "__main__":
    unittest.main()
