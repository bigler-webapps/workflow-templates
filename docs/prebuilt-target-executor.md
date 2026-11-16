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
| `temporary_parent` | Existing private directory for generated Compose inputs and retained deployment configurations. |
| `migrate_service` | Optional reviewed application service for the fixed Django migration command. |
| `additional_image_services` | Optional tuple of `(service, key, namespace)` triples (WM-INF-122). The service receives `output.additional_images[key]` of the manifest; `namespace` is the trusted GHCR backend namespace that digest must belong to. Empty for a single-image release. |
| `one_shot_services` | Optional tuple of service names (WM-INF-122) that are declared one-shot in the reviewed controls (`restart: "no"`). |

### Several images per release and one-shot helpers (WM-INF-122)

A release may carry further images next to the primary one (`release-deployment-contract.md`, "Several images per release"). The
set of additional images is named by the trusted `TargetSpec` only: a manifest that adds, drops or re-namespaces one is rejected
before any command, each digest is checked against the registry like the primary one, and the compose override pins each service
to its own digest. Everything else is unchanged: no build on the target, every service image is immutable.

A service named in `one_shot_services` is expected to run once and exit. It must not restart by itself (`one_shot_restart_invalid`
before activation). After `up`, a one-shot service passes only when it has exited with code 0; one that exited non-zero fails the
deployment as `one_shot_failed_<service>` and one that is still running (or restarting or created) as `one_shot_running_<service>`
(hyphens in the name become underscores; the name is at most 30 characters). A failed `up` caused by a helper that exited non-zero is
reported the same way. A service not declared one-shot that is stopped still fails (`compose_ps_state_invalid`). The one-shot
services are part of the observed service set and of the project inventory check.

A service whose rendered configuration has a healthcheck is `starting` right after `up` (a java engine takes up to its start_period). The
deployment waits for it: it re-reads `compose ps` every `HEALTH_POLL_SECONDS` (3 s) for at most `HEALTH_WAIT_SECONDS` (180 s, and never past
the whole-deployment deadline) and fails as `health_wait_timeout` when it is still starting. Any other failing row (`unhealthy`, a stopped
service, a failed one-shot helper) fails at once and wins over a service that is still starting; a stack without a healthcheck never waits.

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

1. `docker --host unix:///var/run/docker.sock compose ... config --format json`. Privately parse the bounded result,
   require the exact reviewed services and images, and refuse build configuration. Write the resolved
   model to a target-owned `0600` file in the private generation directory. Reload it without the
   provider's runtime input, require the same Compose model, and remove the render-only override.
2. Require the project's recovery gate state file (`/var/lib/webapp-recovery/gates/<project>/gate`: a first line `open`, then one JSON line naming the project, as
   `stack_recovery` writes and reads it; this API never writes it), then `docker compose ... pull`.
3. Put every service that restarts automatically behind the recovery startup gate (see below): `docker image inspect` of each
   such service's pulled image supplies its entrypoint and command, the retained model is rewritten once with the gate
   entrypoint and the read-only gate mount, and the rewritten model must reload to exactly the same Compose model.
4. `docker compose ... up --detach --no-build --pull never --force-recreate --remove-orphans`.
5. Optionally, `docker compose ... exec -T <service> python manage.py migrate --noinput`.
6. `docker compose ... ps --all --format json`, followed by fixed narrow
   `docker inspect` templates for image, state, restart flag, health, project
   and service labels, plus the Compose project working directory and config-file labels.
   The latter must identify the application directory and the retained model used for activation.
7. Compare those IDs with the Engine's complete project-label inventory before
   removing older configurations.

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
and processes. The target directory lock serializes SDK activations for that
target. A failure before activation removes the candidate. Once `up` is
attempted, both the candidate and earlier configurations remain available
because a partial recreation can leave containers with different labels.
After a complete deployment confirms the new labels, older configurations
created by this SDK for the same project are removed. The retained model
contains resolved runtime values and must stay private. Recovery can reuse
it even after the provider rotates its input. The executor never builds,
logs in, accepts a caller-selected Docker endpoint, joins a
network, uses SSH/sudo, installs tools, checks out source, executes artifact
code, rolls back, prunes old images or retires app files.

## Recovery startup gate (WM-INF-117)

The staging refresh and the recovery tooling (`stack_recovery`, ops-scripts) refuse an application whose automatically
restarting services are not startup-gated. A service is gated when its entrypoint ends in `webapp-recovery-gate-v1` and
`/var/lib/webapp-recovery/gates/<project>` is mounted read-only at `/run/webapp-recovery`. The legacy deploy gets that from the
overlay `stack_recovery.gated_compose_override` writes; this SDK renders the same contract itself, from reviewed code only:
the wrapper script, marker, mount and the services it applies to (every service with a restart policy other than `no`, except
the `db` service, which recovery never gates) are fixed here and are never read from a compose control, the application
repository or the host. The wrapper waits until the gate file reads `open` (the default state) and then `exec`s the command the
service would have run: the compose `entrypoint`/`command` when set (an entrypoint override clears the image command, as
Docker does), otherwise the image's entrypoint and command. A restart policy is `restart`, or `deploy.restart_policy.condition`
when `restart` is absent. A missing or malformed gate file rejects with `startup_gate_missing`, and a `blocked` gate (an unresolved
recovery) with `startup_gate_blocked`, both before `pull`; an image or service without a runnable command rejects with `gate_command_unavailable`;
neither activates anything. The gate directory itself is created by `stack_recovery` (the legacy overlay or the first recovery
transaction writes the initial `open` state); a project whose first ever deploy is a prebuilt one needs that state placed by the
operator-run provider apply first, because this API never writes under the recovery state root. The refusal on the recovery side
is unchanged: there is no exemption for prebuilt targets.
`webapp-management` pins this file and tests that its gate equals `stack_recovery`'s byte for byte.

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
