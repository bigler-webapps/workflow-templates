# Prebuilt image target executor

`.github/scripts/prebuilt_target.py` is an inert Python library for a future
trusted caller running on an application target. It accepts original release
manifest bytes, validates them with `release_manifest.validate_manifest`, and
requires an independent registry read to bind the expected run/attempt/source
tag to the exact immutable digest. A manifest is build evidence; it does not
authorize a run, source, environment, target, or promotion. The future caller
must complete WM-SEC-17 authorization and effect-time exclusion before invoking
this module or obtaining target credentials.

The existing manifest contract now exposes pure
`release_manifest.validate_manifest_structure` for schema/source/output-shape
preflight before any credential-profile read. Its result is not registry
verification or deployment authorization. The original `validate_manifest`
reuses that same preflight and still requires the independent pushed-image
resolver; the target executor always performs that full validation afterward.

## Public API

`TargetSpec` is a frozen caller configuration with these fields:

| Field | Meaning |
| --- | --- |
| `project_name` | Bounded Compose project identifier. |
| `environment` | `staging` (requires `refs/heads/develop`) or `production` (requires `refs/heads/main`). |
| `application_dir` | Existing absolute application directory on the target. |
| `compose` | Tuple of `ComposeControl(filename, sha256)` entries for reviewed existing Compose YAML files. |
| `image_services` | Exact Compose services that receive the manifest image. |
| `sidecar_images` | Tuple of `(service, image@sha256)` pairs for all other expected services. |
| `runtime_env_path` | Provider-created private runtime input. The executor only checks identity, owner and mode; it never reads its contents. |
| `docker_config_dir` | Existing private provider-created directory containing only the scoped `config.json` profile below. |
| `temporary_parent` | Existing private directory for the executor's owned override directory. |
| `migrate_service` | Optional reviewed application service for the fixed Django migration command. |

Call `execute_image_deployment(raw_manifest, *, expected_identity,
expected_image_namespace, resolve_pushed_image, target, executor=None,
clock=time.monotonic, deadline=None)`. `expected_identity`, namespace,
registry reader and every `TargetSpec` routing value come from a trusted
caller, never from the manifest or application artifact. The manifest must be
original bytes, not a caller-parsed mapping. The result is the immutable
`DeploymentResult` with the selected digest, checked source identity,
environment, complete service set and whether migration ran. Errors are
`PrebuiltTargetError` instances containing only fixed codes.

## Data and command boundary

Before a process or registry callback, the library validates complete scalar
values, service sets, supported release ref/environment, absolute normalized
paths, compose filenames and hashes, sidecar references and operation shape.
Runtime input, Docker configuration and temporary paths must be outside the
application tree, so approved application mounts cannot include job credentials.
It then checks existing target directories, bounded non-secret Compose
controls and the provider-created runtime file. The runtime file is only
`lstat`/metadata checked. It is not opened, parsed, hashed, printed or created.

Reviewed control files use a bounded YAML subset parsed with pre-provisioned
`PyYAML==6.0.3`; the exact fragment is
`toolchains/compose-parser-requirements.txt`. The target helper refuses a missing
or different parser and never installs it. The existing controller already had
this version; its [upstream release](https://github.com/yaml/pyyaml/releases/tag/6.0.3)
and [PyPI record](https://pypi.org/project/PyYAML/6.0.3/) were checked on2026-10-22.
This is only a parser fragment; the complete pinned deploy toolchain and its
installation/acceptance remain separate work.

The parser limits events/depth, accepts no aliases, anchors, explicit tags or
duplicate/non-string mapping keys, and constructs no tagged Python objects.
Compose `include`, service `extends`/`env_file`/`label_file`, and file-backed
`configs`/`secrets` are rejected rather than loading unbound external files.
Those legacy controls require reviewed flattening/adaptation at their individual
cutover; no existing caller is switched by this library. Ordinary named volumes
and environment interpolation remain data in the approved controls. The owned
override alone supplies the validated private runtime input as `env_file` for
image services. Control validation precedes private Docker-profile reading.

The separate Docker profile is read privately by the target process, never by
an agent with real credentials. It must be a single regular non-link `0600`
file no larger than16KiB, containing only `auths` with exactly a `ghcr.io`
entry. That entry is empty for explicitly anonymous pulls, or contains only
bounded valid base64 `auth` for the separately provisioned scoped identity.
No credential values are hashed, returned or logged. Helpers, credential
stores, contexts, plugin paths, extra registry credentials and sibling files
are rejected before Docker or the registry callback. The runtime input cannot
alias this profile.

The non-empty registry map is deliberate: Docker's default config loader can
auto-detect a platform credential store for a config without authentication.
Its current [`LoadDefaultConfigFile`](https://github.com/docker/cli/blob/master/cli/config/config.go)
and [`ContainsAuth`](https://github.com/docker/cli/blob/master/cli/config/configfile/file.go)
implementations were checked on2026-10-22. Exact installed-version compatibility
and provider custody remain live acceptance requirements; this does not install
or select a real registry credential.

The executor creates one random `0700` directory below the configured private
parent and one `0600` Compose override YAML file. The override selects the exact
manifest digest for application image services and removes Compose build
configuration using the documented `!reset null` tag and binds image-service
`env_file` only to the metadata-checked private runtime input. Source Compose
files are never rewritten. The fixed command
sequence is:

Every Docker invocation starts with the fixed target-local endpoint
`docker --host unix:///var/run/docker.sock`; saved `currentContext` cannot
select another engine and inherited Docker host/context variables are absent.
The target's reviewed toolchain must provide this local rootful socket.

1. `docker --host unix:///var/run/docker.sock compose ... config --format json`; privately parse the bounded result
and require the exact reviewed services, images and no build configuration.
2. `docker compose ... pull`.
3. `docker compose ... up --detach --no-build --pull never --force-recreate --remove-orphans`.
4. Optionally, `docker compose ... exec -T <service> python manage.py migrate --noinput`.
5. `docker compose ... ps --all --format json`, followed by fixed narrow
   `docker inspect` templates for image, state, restart flag, health, project
   label and service label.

Rendered application images and post-up observed container images must both
equal the immutable selected digest. Sidecars must match their reviewed pinned
references. The observed service set must be complete and unique; all required
containers must be running and non-restarting, and health must be healthy when
configured; enabled rendered healthchecks require an observed healthy state,
while disabled or absent checks may have no health state. Compose JSON Lines
and a bounded legacy JSON array are accepted. The outside-application boundary
also rejects runtime/control aliases before any control is opened.
The helper never captures `Config.Env`, full inspect objects, logs or diagnostic
text. Bounded Compose ps metadata may include `Command`; it is ignored and never
returned or printed. The default adapter uses fixed argv, `shell=False`,
a small explicit child environment, suppressed stderr and bounded capture and
runtime. Tests inject a fake `executor.run` and never launch Docker, SSH or
network calls.

Command-output overflow and timeout issue a kill and use bounded child waits;
owned pipes close on success and failure. Cleanup permits up to one second of
bounded reaping after a timeout, never an unbounded `wait()`. This does not
prove termination of uninterruptible kernel work or undo a target-side effect.
Captured pipes are unbuffered, so closing them cannot wait on a buffered
reader lock. A stalled reader retains the process-wide capture gate until
its read actually exits; another capture fails before spawning a child.

The whole operation uses a finite monotonic deadline, checked immediately
before and after the registry callback as well as manifest validation
and processes. Failure, exception and Python cancellation paths attempt to
remove only the executor-owned override directory; cleanup failure returns a
safe error. This source does not claim hard-kill cleanup or installed signal
handling. It never builds, logs in, accepts a caller-selected Docker endpoint, joins a
network, uses SSH/sudo, installs tools, checks out source, executes artifact
code, rolls back, prunes old images or retires app files.

## Evidence and remaining integration

`test_prebuilt_target.py` exercises actual manifest production/validation,
filesystem controls, override bytes, exact no-build argv, rendered config,
container observations, sidecars, migration, failure cleanup, unsafe metadata,
deadline handling, process suppression and timeout cleanup through synthetic
executors and a fake `Popen`. It is not evidence of Docker/Compose version
compatibility or a live deployment.

This component does not provide the source/workflow/artifact retrieval binding,
trusted authorization or exclusion, runtime-secret generation/custody,
controller transfer/action, approved target policy, target installation or
pre-provisioned toolchain release. It also does not publish or deploy static
assets, prove host/CI/repository containment, run staging or hosted break-glass
acceptance, migrate consumer pins, retire legacy behavior, or close programme
findings. Those remain later WFT-CI-45 and WM-INF-90/WM-SEC-17 work and their
separate approval and live-evidence gates. The actual runtime invocation must
be assembled and reviewed before this library can have effect.

Docker references checked for the source contract on 2026-10-22:
[Compose reset merge semantics](https://docs.docker.com/reference/compose-file/merge/#reset-value)
and [Compose ps JSON Lines](https://docs.docker.com/reference/cli/docker/compose/ps/#format-the-output---format).
The reviewed target toolchain must support the reset tag; no real Docker or
Compose compatibility has yet been proved by these synthetic tests.
