# GitHub runner role profiles

`github_runner_profile: ci` remains the default. `tasks/ci.yml` is the complete
pre-profile task file, including its comments; the existing CI service and JIT
templates are unchanged. No WFT-CI-43 hardening is implemented: the operator
dropped that WO on 2026-10-19. Base-image freshness remains WFT-CI-45 work.

`operations` is an opt-in profile for the isolated `ops` inventory host in the
`operations` group, outside `runners`. Set both slot counts to integer 1 and
supply the verified target OS/runtime package manifest with exact `name=version`
pins, including Git, SSH, rsync, Python, curl and jq. Only those tools and the
explicitly allowlisted certificate/timezone and runtime-library packages are
accepted; a verified flag cannot add Docker, Node/npm or arbitrary tools. Other
deployment clients need a reviewed toolchain change under WFT-CI-45. Target OS/runtime-library
pins are not guessed here. The separate runner archive is pinned at 2.338.0 and
its SHA256; CI's existing runner pin is unaffected. Missing package evidence
fails before any provisioning. The role refuses provisioning an active slot;
use the separately approved drain/recovery lifecycle first.

The protected download cache has one fixed `runner.tar.gz` path. A new verified
pin replaces that file instead of accumulating a versioned archive on every
upgrade; extraction consumes the same fixed path. Repeated provisioning can
reuse its checksum-verified content.

The fixed users are `gh-operations`, `gh-runner-issuer` and
`gh-operations-poller`. The profile installs no App key, Docker access, sudo,
Node/npm package manager or AI CLI. GitHub's bundled action runtimes in
`externals` remain necessary for JavaScript Actions. Their files, the runner
binaries, launcher and unit are root-owned. The issuer and poller have separate
0700 state directories and are not activated by this role.

The unit creates a fresh service-private tmpfs for writable JIT configuration,
home, work, temporary files and `_diag`, capped at 256 MiB by default and 32768
inodes. Separate `/tmp`, `/var/tmp` and private `/dev/shm` mounts are capped at
16 MiB each. Devices are private; the whole job service has a 1 GiB cgroup
memory limit and no swap allowance. All
these mounts disappear when the service stops, including forced termination.
Read-only bind mounts expose root-owned `bin` and `externals` inside the writable
runner root. Jobs cannot replace those mount points or mutate their files;
there is no job-owned executable copy. An empty capability set and
`NoNewPrivileges` prevent the job from remounting them. `RuntimeMaxSec` is at
most one hour. Successful one-job exits do not consume a startup quota;
restart attempts wait 30 seconds, with registration rate/capacity controlled by
the separate issuer policy. Persistent local stdout/stderr is disabled; runner diagnostics
share the tmpfs cap. These limits bound storage, not measured host suitability:
representative deployment/remote-backup memory and recovery proofs remain due.

This scaffolding always disables/stops the service and rejects activation even
if inventory flags claim authorization or containment. WM-SEC-18 must implement
the separate activation path after recorded authorization, effective early
containment and a verified, exact-version,
exact-digest trusted client at `/usr/local/libexec/wm-sec18/issue-runner-jit`.
That client must be root-owned, group `gh-operations`, mode 0750, in root-owned
non-writable, non-symlink ancestors. The launcher checks every ancestor through
the root directory before executing the client. The group permits the unprivileged runner to execute
this credential-free client; the issuer's keys and policy remain isolated under
its separate identity. The role does not install or implement this client.

The fixed request is `issue-runner-jit --repository
bigler-webapps/webapp-operations --runner-name ops-slot-1`. The client/issuer
must authenticate the fixed caller, choose and enforce repository, group,
labels, single-slot capacity and one-use lifetime server-side, and emit one
encoded JIT configuration on stdout. Its `.runner` settings must encode
`ephemeral=true`, `disableUpdate=true` and `workFolder=work`; job code must not
select these values. Failure diagnostics must contain no credentials. The
launcher checks the client file and digest, decodes only the known JIT files
from stdin into exclusive 0600 files, and checks the ephemeral/no-update/local
work-folder settings before invoking `Runner.Listener run`. Credential-bearing
JIT data never enters the runner's process arguments. Short-lived configuration
is beside `bin` in the private tmpfs, as required by the runner's path selection.
The launcher never invokes `config.sh --jitconfig` or enables self-update. Jobs
share the runner UID and can read that one-job runtime state; this is not a
sandbox for hostile jobs. Trusted repository policy and issuer/revocation
boundaries remain mandatory.

The layout follows the pinned upstream [JIT run implementation](https://github.com/actions/runner/blob/v2.338.0/src/Runner.Listener/Runner.cs)
and [root/configuration path selection](https://github.com/actions/runner/blob/v2.338.0/src/Runner.Common/HostContext.cs).
The unit uses documented [systemd filesystem isolation](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html).
Offline tests execute real Ansible assertions/routing with every host-mutating
module replaced by an instrumented debug action, and a fake local JIT client
and runtime exercise the real shell launcher. They do not prove actual systemd
mounts, registration, target package compatibility, API policy or live isolation.

This is inactive shared-role source only. Its collection release/consumer pin,
WM provisioning play, verified host access and OS manifest, issuer/client,
GitHub group exclusion, trusted-source protections, deployments, monitoring and
recovery remain separate work and gates. No host, registration or credential
is activated by publishing this role.
