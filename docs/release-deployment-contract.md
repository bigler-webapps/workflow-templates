# Release manifest contract v1

This document defines the offline manifest contract implemented by
`.github/scripts/release_manifest.py`. It is an evidence format for a future
trusted release consumer. A manifest match does not authorize deployment,
prove that a build was honest, or make artifact contents safe to extract.
WM-SEC-17 policy must independently check the allowlisted repository and
workflow, successful run and attempt, event, exact source head, branch state,
and deployment environment through authoritative GitHub metadata.

## Bounds and encoding

The manifest is UTF-8 JSON, at most 16 KiB, with no duplicate keys, non-finite
numbers, unknown keys, nested extra fields, or type coercion. Its canonical
encoding sorts object keys, uses compact separators, escapes non-ASCII text,
and has no trailing newline. Validators reject noncanonical bytes (including extra whitespace, key reordering or a trailing newline) before a registry read. Consumers must validate the original bytes before
using the decoded object. The generic encoder is private and accepts only the
bounded internal objects assembled by the typed producers or checked for canonicality during parsing; there is no public generic
manifest-dictionary serialization entry point. Diagnostics are stable error codes and do not include
untrusted values or resolver exception text.

The exact top-level fields are `version`, `kind`, `source_repository`,
`source_sha`, `source_ref`, `source_workflow`, `run_id`, `run_attempt`, and
`output`. Version is integer `1`; kind is `image` or `static`. Repository is a
ASCII `owner/name` (case and underscores preserved for estate names such as `survey_app` and `Gustav`); SHA is 40 lowercase hexadecimal characters; ref is
exactly `refs/heads/develop` or `refs/heads/main`; workflow is a repository
relative `.github/workflows/<name>.yml` path; run ID and attempt are positive
integers. These fields cannot contain control characters. Manifest fields for
environment, host, path, command, secret, or arbitrary URL are not allowed.

Image output has exactly one field, `image`, whose value is an immutable
`ghcr.io/<owner>/<name>-backend@sha256:<64 lowercase hex>` reference. Static
output has exactly `artifact_name`, `artifact_id`, `byte_length`, and `sha256`.
The static artifact name is fixed as `release-static-<run id>-<attempt>`;
artifact ID is positive; byte length is from zero through 64 MiB; SHA-256 is
64 lowercase hexadecimal characters. Trusted policy may set lower size limits,
but untrusted artifact metadata cannot raise the limit.

Example image manifest (canonical key ordering is applied by the serializer):

```json
{"version":1,"kind":"image","source_repository":"bigler-webapps/sample-app","source_sha":"0123456789abcdef0123456789abcdef01234567","source_ref":"refs/heads/develop","source_workflow":".github/workflows/release.yml","run_id":123456,"run_attempt":2,"output":{"image":"ghcr.io/bigler-webapps/sample-app-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}
```

Example static output:

```json
{"version":1,"kind":"static","source_repository":"bigler-webapps/sample-app","source_sha":"0123456789abcdef0123456789abcdef01234567","source_ref":"refs/heads/develop","source_workflow":".github/workflows/release.yml","run_id":123456,"run_attempt":2,"output":{"artifact_name":"release-static-123456-2","artifact_id":998877,"byte_length":32768,"sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}}
```

## Python interface

`ReleaseIdentity(repository, sha, ref, workflow, run_id, attempt)` carries
expected identity from a trusted source. Pass it independently to both
producers and `validate_manifest`; never construct it from the manifest being
validated.

`produce_image_manifest(identity, expected_namespace=..., pushed_tag=...,
resolve_pushed_image=...)` asks the injected registry-read function for the
exact expected namespace and pushed tag. `release_image_tag(identity)` derives the required tag as `release-<run id>-<attempt>-<full source SHA>`; `pushed_tag` must match it before any registry read. An arbitrary old tag cannot be relabelled as a new release by this API. The reader returns a mapping with
`repository`, `tag`, and `digest` obtained from registry metadata. All three
must match the request and a valid SHA-256 digest. A digest string supplied by
the build caller alone is insufficient. This module contains no registry
adapter, credential handling, HTTP client, command execution, or build/action
wiring.

`produce_static_manifest(identity, artifact_id=..., bundle=...,
bundle_length=...)` hashes actual bytes or a binary stream in bounded 64 KiB
reads. It does not extract, inspect, or execute the bundle. The trusted caller
must bind the selected artifact name and exact artifact ID from authoritative
run metadata. The name convention is not proof of artifact provenance.

`validate_manifest(raw, expected=..., expected_image_namespace=...,
resolve_pushed_image=...)` checks an image manifest and independently resolves
the expected run/attempt/SHA tag in the trusted namespace. Its own trusted
registry reader must observe the exact manifest digest; a caller digest string
or manifest claim alone cannot satisfy validation. One bounded registry read
seam is used per image validation; actual adapter timeouts remain integration
requirements. Static validation uses
`expected_static_artifact=StaticArtifactSelection(name, artifact_id)` instead.
Expected namespace and selected artifact metadata must come from trusted policy
and independently retrieved run metadata, never from the manifest. The
validator returns the decoded mapping only after the strict checks pass. Exactly one output expectation must be supplied; conflicting image/static selection is rejected before a payload can select a different mode.

`verify_static_bundle(stream, manifest=validated_manifest)` computes bounded
length and SHA-256 over the subsequently supplied bytes and compares them to
the validated metadata. It performs no archive extraction. A matching hash
proves byte identity only; it does not establish safe paths, link handling,
trusted content, or safe deployment.

## Retrieval and authorization provenance

The future operations consumer must retrieve the manifest and static artifact
only from the exact originating repository, workflow run ID, and run attempt
selected by its own authoritative GitHub checks. It must reject cross-repo or
cross-attempt artifacts. It must independently select and compare artifact ID,
source repository, exact head SHA, branch ref, workflow path, and successful
run status before using these bytes. Registry namespace comes from the trusted
allowlist. These functions cannot fetch or establish any of that provenance.

For images, the publish action still needs to push, then read the pushed
reference's registry digest and produce this manifest. For static releases, a
trusted artifact upload and retrieval path must bind the fixed artifact name
and ID to the selected run. WM-SEC-17 independently authorizes successful
allowlisted runs and checks source state; the manifest is evidence, not
authority. It cannot certify an honest build on a compromised root-equivalent
CI host.

## Remaining integration and acceptance gates

This file is only the offline producer/validator slice. It does not modify or
prove the publishing action's fresh `--pull` behavior, a real registry push or
digest lookup, artifact upload/retrieval binding, deployment action validation,
strict no-build fallback, static archive extraction safety, pinned operations
tooling, credential isolation, or consumer migration. The next producer batch
must wire fresh-base builds using that exact run/attempt/SHA tag, registry digest resolution, and artifact
publication into trusted workflow context. The later prebuilt-deployment batch
must validate before secret acquisition or target contact, preserve exact
image digests through the target compose command, safely extract static content
with traversal/link/control-file protections, and keep legacy pinned consumers
working during rollout. Toolchain pinning, hosted recovery parity, candidate
staging proofs for image and static apps, and operator-approved activation
remain separate gates. No live registry, workflow, runner, target, or
deployment evidence is established by the tests in this batch.
