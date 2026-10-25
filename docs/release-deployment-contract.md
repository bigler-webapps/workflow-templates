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

For images, the composite `publish-backend-image` action exposes an explicit
`release_contract: v1` opt-in. Its `release_image_namespace` must be an
externally selected `ghcr.io/<source-owner>/<name>-backend` namespace, and
`release_workflow_ref` must exactly match the triggering
`GITHUB_WORKFLOW_REF`. It accepts the established Dockerfile/context and Vite
build inputs plus the per-run GHCR token and optional BuildKit private-engine
token. It rejects `existing_image`, and it does not read `project.yaml`,
`.env`, runtime-secret bundles, or caller-local scripts. With no release
contract selected, the legacy action inputs, tag behavior, survey build args,
and `image` output remain on the original path.

The v1 action calls `.github/scripts/release_build.py` from its installed
`github.action_path`. The script checks `GITHUB_REPOSITORY`, `GITHUB_SHA`,
`GITHUB_REF`, `GITHUB_WORKFLOW_REF`, run ID/attempt, and the actual `git
rev-parse HEAD` before login or build. Only `refs/heads/develop` and
`refs/heads/main` are accepted. A manual checkout whose HEAD does not match
the triggering SHA fails closed; callers must select a release whose source
matches the checked-out release. The output tag is derived as
`release-<run id>-<attempt>-<full source SHA>`; Docker receives fixed argv,
`--pull`, the validated repository-relative build paths, and existing named Vite
arguments passed from build-only environment (values are not placed in argv). The GHCR login token is supplied only on login stdin, never in a child environment. Git receives only basic OS/tool-discovery environment; Docker login/push/inspect receive no job credentials. Only the build receives the optional private-engine token environment for the named BuildKit secret. Unrelated job credentials are not forwarded. Docker writes its
credential state into a private per-run config directory that is removed on
success and failure. Build and command error text is suppressed from action
logs.

After push, the producer runs the equivalent of
`docker buildx imagetools inspect <namespace>:<derived-tag>` with a fixed JSON template selecting `.Name` and `.Manifest.Digest`. Both the observed reference name and digest are checked; the [Docker CLI reference](https://docs.docker.com/reference/cli/docker/buildx/imagetools/inspect/) documents the registry operation and JSON template fields.
It obtains the digest from the registry response and passes repository, tag,
and digest to `produce_image_manifest`; neither the local image ID nor a
caller-supplied digest is used. The canonical `release.json` is written with
mode 0600 into a private directory below `RUNNER_TEMP`, then uploaded by the
existing pinned `actions/upload-artifact@65c4c4a1ddee5b72f698fdd19549f0f0fb45cf08`
action as `release-manifest-<run id>-<attempt>` with a seven-day retention and
missing-file failure. An always-running cleanup step removes the private
directory after upload, including upload failure. The composite action output
`image` is the immutable digest reference; `manifest_path` and `artifact_name`
are safe outputs for the in-job upload step. `artifact_id` is the upload action result and must be independently checked against the originating repository/run/attempt by the consumer. The local path is removed after upload, so later jobs retrieve the artifact rather than reuse that path. Private directories are allocated randomly and cleanup selects only the directory created by this action invocation; pre-existing similarly named files are not removed. Unknown modes fail closed. Login has a30-second deadline; build1800 seconds; push600 seconds; inspect30 seconds and128KiB maximum captured metadata. Raw child stdout/stderr are suppressed except the bounded registry-read response parsed internally. Credential cleanup failure fails the producer and suppresses output publication; it requires investigation rather than claiming cleanup succeeded.

These are source-level producer mechanics and offline stub coverage. The
supported Docker/buildx/Python versions, real GHCR login/push/inspect behavior,
artifact provenance and retrieval binding, and WM-SEC-17's authoritative
successful-run/source authorization still require their own acceptance
evidence. Static releases still need their producer. WM-SEC-17 independently
authorizes successful runs and checks source state; the manifest is evidence,
not authority. It cannot certify an honest build on a compromised
root-equivalent CI host.

## Static asset preparation

`prebuilt_deploy.prepare_static_assets(raw_manifest, expected_identity=..., selected_artifact=..., bundle=..., allowed_asset_roots=("report",), destination_parent=...)` consumes original manifest bytes, externally selected source/artifact identity and actual bounded bundle bytes. It reuses the published manifest validator and byte verifier; there is no prevalidated-dictionary bypass. Roots and a trusted temporary parent come from reviewed caller policy. Source archive data cannot select routing or overwrite deployment configuration. The return value is a newly allocated private directory for later transfer, never evidence of deployment success or authorization.

The accepted bundle format is uncompressed USTAR only. Producers must normalize uid/gid and device numbers to zero, leave user/group/link fields empty and use USTAR00 headers without GNU/PAX/sparse extensions. Bounds:64MiB bundle,512 members,16MiB per file and48MiB total extracted content. Every header/checksum/path/type/mode/extent is checked before destination creation. The archive must have a complete two-block terminator and only zero trailing padding. Gzip and other compressed formats are rejected. Real regular file content is copied in at most64KiB writes; no extractall, executable evaluation, subprocess or target access is used.

Paths are relative under exact trusted roots. Traversal, absolute/drive/backslash paths, links/devices/FIFOs/sparse entries, executable file modes, duplicates and parent-child conflicts fail closed. Casefolded collisions are rejected for portability. Control files including .git/.github, .env variants, compose/project/secrets configuration, Dockerfile, Caddyfile and shell/Python/PowerShell/Windows command files are excluded even under an allowed root. Ordinary browser JavaScript is asset data. Caddy/compose configuration remains separately reviewed, never taken from this bundle. Directories are0700 and files0600 on the Linux controller; this API does not promise restrictive Windows ACLs or authorize use there as a privileged executor.

The parent directory must already exist, be absolute and have no symlink ancestor. The preparer creates its own unique private directory, never reuses an existing destination. Validation failures write nothing; write failures attempt removal of all paths created by the call and preserve pre-existing content. An OS cleanup failure raises `asset_cleanup_failed` and may leave a private staging directory requiring caller cleanup/investigation; it is never reported as success. The trusted integration owns successful-directory cleanup after transfer/cancellation and must not publish assets until preparation succeeds. Hash equality alone does not imply safe extraction or trusted content; this preparer applies those additional path/type/size rules.

## Remaining integration and acceptance gates

This file records the offline manifest plus image-producer source slice; it
does not prove a real registry push or digest lookup, artifact
upload/retrieval binding, deployment action validation, strict no-build
fallback, real static deployment and remote atomic publication, pinned operations tooling,
credential isolation on live runners, or consumer migration. The later
prebuilt-deployment batch
must validate before secret acquisition or target contact, preserve exact
image digests through the target compose command, safely extract static content
with traversal/link/control-file protections, and keep legacy pinned consumers
working during rollout. Toolchain pinning, hosted recovery parity, candidate
staging proofs for image and static apps, and operator-approved activation
remain separate gates. No live registry, workflow, runner, target, or
deployment evidence is established by the tests in this batch.

## Build-context hygiene in v1

The source Dockerfile and effective Dockerfile-specific or context ignore file are read as bounded control data (at most128KiB each). The producer never opens an environment/key file. A private copy of the identical Dockerfile receives a generated adjacent ignore file containing the effective source rules followed by final exclusions for .env variants, .ssh/.secrets directories, PEM/key/SSH key files and Git state at any depth. Source ignore negations cannot undo the appended exclusions. The checkout and its ignore files are unchanged. [Docker documents specific-ignore precedence and last-match rules](https://docs.docker.com/build/concepts/context/); supported builder versions and an actual candidate build must still prove this integration. Missing/unsafe Dockerfile/ignore controls fail before login. Unknown runtime secrets under arbitrary filenames cannot be discovered by filename filtering: release jobs must receive no runtime secret and the CI-host compromise residual remains open. This guard neither certifies honest builds nor replaces containment. [Named build arguments use the build environment](https://docs.docker.com/reference/cli/docker/buildx/build/#build-arg); browser license/build values may still appear in intended application build output/image and are not promised to be private runtime secrets.

The dedicated CI-side static producer is documented in [Static release producer](static-release-producer.md). Its additive source passed the WFT-CI-45 scoped source gate; real uploaded-artifact compatibility, strict deployment, versioned release and consumer acceptance remain separate gates. Existing callers keep their pinned paths.

## Bounded connection diagnostics

The legacy `deploy-app` action emits fixed phase/result codes in its three
controlled connection blocks. The warm probe privately reads at most 65,537
bytes per status attempt and accepts at most 65,536 bytes, counting trailing
newlines before command substitution can strip them. Only
a successful status read with `Running` and the requested tag yields
`connected=true`; unavailable, malformed, oversized or failed reads select the
existing join path. Failed direct reads are discarded before the scoped
`sudo -n` read fallback. Neither status JSON nor raw backend state, tags,
targets, usernames, peer topology or provider stderr is printed by these blocks.

`runner_connection` reports `reused` or `join_required`.
`runner_diagnostics=bounded_summary` is informational and executes no full
status/netcheck dump. `target_datapath` reports `observed` or `failed` after
direct ping and its existing scoped sudo fallback. Ping failure remains
nonfatal; the existing SSH `true` probe decides success through
`target_ssh=success` or `target_ssh=failed`, with the latter failing the step.
All status, ping and SSH invocations use a 25-second GNU timeout with TERM
and a five-second kill grace. The two attempts are sequential and each has
its own cap; this is not a 25-second bound on the whole step. Underlying
probe stdout/stderr is suppressed, apart from the private bounded JSON read.

The join action, warm-daemon reuse, existing legacy OAuth inputs, read-only
sudo fallback and SSH host-key policy are retained. This change does not
establish an operations mode, CI exclusion, protected credentials or host
custody. It does not redact third-party join logs or other deployment steps.
Local Bash tests execute the actual YAML blocks with private synthetic CLI
stubs; representative connectivity, cancellation and live log acceptance,
strict prebuilt integration and independent recovery remain open.

## Whole-scalar validation in the legacy deploy action

The legacy `deploy-app` caller-input validation checks each complete scalar
before later environment generation, transfer or remote-shell steps. Project
and environment names, SSH users, optional host overrides, file-secret names
and modes, and file-secret target subdirectories reject multiline suffixes,
control characters, shell syntax and values outside their bounded grammars.
The optional file-secret subdirectory still accepts safe relative paths such as
`.ssh` and `.ssh/app`; filenames remain safe basenames, and empty optional
values retain their existing meaning.
The initial caller guard uses Bash builtins and requires no Python before the
existing setup step. IPv6 lexical checks there are followed by full IP parsing
in the configuration step before `GITHUB_ENV` is written.

Configuration parsing validates the top-level mapping and the selected
deployment fields' mapping shapes and scalar types before appending any
generated entries to `GITHUB_ENV`. Unrelated configuration fields are not
recursively type-validated. Explicit duplicate mapping keys are rejected;
ordinary YAML anchors and merge overrides remain supported. The project name uses
the existing lowercase project grammar; environment mappings, server names,
tailnet suffixes, persistent-directory lists and each relative path are
validated without trimming invalid input or coercing non-string YAML values.
Declared environment mappings cannot be null. A named file secret must have
a valid octal mode before any remote-shell step; unnamed optional entries may
retain empty modes.
An empty tailnet suffix retains the existing default, explicit valid host
overrides retain precedence, and the derived remote path remains
`/srv/apps/<project>` with `_stage` only for staging. Rejections use fixed
diagnostics and do not echo rejected values. These checks cover only the
legacy source-level action blocks. They do not establish strict prebuilt mode,
private routing/custody, credential handling, caller migration, live host
behavior, or closure of the broader release-deployment contract.
