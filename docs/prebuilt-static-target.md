# Prebuilt static target executor

`.github/scripts/prebuilt_static_target.py` is an inert target-only library for
static releases with no backend. `execute_static_deployment` accepts original
manifest bytes, the selected artifact identity, bounded archive bytes, and an
independently trusted `StaticTargetSpec`. The manifest is evidence only; the
future caller remains responsible for source/artifact retrieval binding,
WM-SEC-17 authorization and effect-time exclusion before credentials or target
effects.

`StaticTargetSpec` supplies the project and environment, target application
directory, hashed Compose controls, static service and its digest-pinned runtime
image, digest-pinned sidecars, allowed archive roots, read-only asset mount
destinations, hashed preserved control mounts, and private asset/temporary
parents. None of those values can come from the artifact. `StaticMount` keeps
reviewed controls such as a Caddyfile mounted read-only, with an external SHA256
binding. An optional provider-created runtime input is only metadata-checked;
the helper never opens or logs its content.

The module delegates manifest handling to `release_manifest` and all archive
parsing/extraction to `prebuilt_deploy.prepare_static_assets`. It reuses the
image target executor's bounded Compose parser, fixed local Docker endpoint,
subprocess adapter, JSON handling, container observation and health checks.
Static artifact bytes cannot select a Docker image, service, path, mount,
credential, command, or routing value. No backend image, migration, build,
registry login, package-write capability, SSH, sudo, network join, installation,
or source hook is involved. Docker receives only an isolated **nonempty** auth
map whose approved registry entries are empty anonymous objects, with no values,
helpers or stores. An empty auth map would permit Docker's automatic default
credential-store detection. No private registry credentials are supported by
this static API; the selected service images must permit anonymous pulls.
Ambient Docker context and credential helpers are excluded. The generated
override sets every service to its trusted digest and resets inherited build
fields before rendered configuration is checked. No actual build is run.

Compose always receives an explicit interpolation input: a provider-created
metadata-checked path, or a private empty `interpolation-input.txt`. It never
falls back to a project-local environment file. Preserved control sources must
stay under the reviewed application tree and exclude runtime/secret paths.

Verified files are materialized beneath a trusted private asset parent. Their
directories become `0755` and regular files `0644` so the target container can
read mounted assets. Compose rendering must show exactly the expected services,
digest-pinned images, read-only bind mounts and no build fields before pull or
activation. The executor then pulls, runs `up --detach --no-build --pull never`
and verifies the exact project/service/image and healthy running state through
the shared narrow observation path.

Asset lifecycle is deliberately conservative. Before activation, failures remove
only this invocation's asset directory. Once the `up` command is attempted, the
asset directory remains on success and failure because a container may still
use it. The immutable `StaticDeploymentResult` includes that retained path,
source/run identity and verified bundle hash. There is no automatic rollback,
prior-release deletion, retirement sweep, or claim of atomic distributed
recovery. Failed activation requires a later trusted recovery decision; old and
new trees remain available until that integration defines safe retirement.
On post-activation failure, `StaticTargetError.retained_asset_directory` exposes
the retained path to the trusted caller; the exception text remains a fixed safe
code. Parent inode/mode/owner and allocated-root identity guard cleanup; an
unverified initial temporary path is removed only when empty, without recursion.
Asset trees remain writable by their trusted host owner; artifact hashes and
read-only container mounts do not claim protection from that owner or root.
Configured container healthchecks must report healthy; absent healthchecks only
establish running container state, not application HTTP health. Actual HTTP and
Docker/Compose acceptance remain part of the later staging/recovery proof.

`test_prebuilt_static_target.py` uses actual manifest validation and archive
preparation with a synthetic executor. It checks no-build argv, pinned images,
control and asset mounts, retained readable assets, pre-activation cleanup,
post-activation retention, and failure without false success. These fixtures do
not prove real Docker/Compose compatibility, registry behavior, artifact-service
retrieval, authorization, secret custody, host/CI/repository containment,
pre-provisioned toolchain compatibility, live staging, consumer migration, or
hosted break-glass recovery. Those remain separate CI45/WM-INF-90/WM-SEC-17
acceptance work.
