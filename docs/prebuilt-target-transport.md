# Prebuilt target SSH transport

`prebuilt_target_transport` is the controller-side client for the fixed
`PTARGET1` binary stream handled by `prebuilt_target_entry`. Importing the
module and constructing its immutable configuration do not perform I/O. The
configuration is disabled by default; `deploy()` proceeds only when a future
protected caller explicitly supplies an enabled, independently authorized
route and release expectation.

For an image release, the controller validates the canonical manifest and
resolves its exact source/run/attempt tag through the trusted registry reader
before connecting. For a static release, it validates the independently
selected artifact and verifies the complete bounded bundle hash locally. The
controller then writes the existing 18-byte header (`!8sBBII`) followed by
the exact manifest and, for static releases, bundle bytes. It adds no routing,
authorization, or command fields to the frame.

The SSH invocation is fixed to `/usr/bin/ssh` and
`/usr/local/libexec/webapp-prebuilt-target-v1`. It disables SSH config loading,
TTY, ambient agents, password and interactive authentication, forwarding,
proxy commands, connection multiplexing, local commands, and host-key updates.
Host, port, user, identity, known-hosts and correlation are
strictly validated and passed as arguments. The remote command accepts only
the fixed `--correlation deploy-<40 lowercase hex>` pair. Provider paths also
reject whitespace and SSH percent expansion. OpenSSH expands `${...}` in its
`IdentityFile` and `UserKnownHostsFile` options, so both provider paths reject
`$` entirely (including `${HOME}`, `${UNSET}`, and bare `$`) before metadata
lookup ([OpenSSH `ssh_config(5)`, environment variables](https://man.openbsd.org/ssh_config#ENVIRONMENT_VARIABLES)). Identity and known-host files are
checked with filesystem metadata only; Python never opens either file. The
identity file must be current-user-owned, mode `0600`, and inside a private
`0700` directory. Known-host data may be root- or current-user-owned and may
not be writable by group or other users.
The entire directory chain must be owned by root or the controller UID and
must exclude group/other writers. A root-owned sticky directory is allowed
because other UIDs cannot replace its next root/controller-owned component;
the next component is checked independently. Directory inode, owner and mode
snapshots are checked again immediately before child creation. Root and the
controller UID remain trusted installation authorities; same-UID/root attacks
are outside this metadata boundary. Python never repairs these paths.

The client uses one monotonic deadline, capped at 1800 seconds and shortened
by the caller's budget. It streams bounded chunks through nonblocking pipes,
drains stdout while writing, discards stderr, and requires EOF, a zero exit
status, and one canonical JSON-LF receipt within the deadline. A successful
receipt must exactly match the independently expected source, project,
environment, correlation, output, immutable image or artifact hash, service
set, and health state. A clean protected-target refusal is returned only when
the receipt binds the same request and says that no effect occurred. Other
post-handoff failures are ambiguous; the client does not retry, roll back, or
claim exactly-once delivery. Cleanup signals only the child it created and
uses up to 0.2 seconds for graceful termination followed by up to 0.2 seconds
for kill/reap. Operating-system scheduling and blocked kernel I/O make these
cooperative bounds rather than hard real-time guarantees. The injected trusted
registry reader must enforce its own blocking deadline; Python checks the
shared budget again after that call returns but cannot interrupt a blocked
reader. Receipt validation reuses the request already validated before SSH;
it performs no second registry read after a possible target effect. Image
migration status must match the separately trusted target policy. The shared
clock is checked after receipt parsing as well as process completion.

The tests use real local pipes and a process-like test endpoint. Positive image
and static cases invoke the unchanged target stream entry and its actual SDKs
with the existing synthetic executor fixtures. They do not start SSH, open a
network connection, invoke Docker, read credentials, or contact a target.

## Prerequisites and limits

This source does not install the forced launcher, provision SSH credentials,
authorize a release or target, reserve a dispatch slot, exclude hosted
break-glass work, reconcile uncertain effects, or establish source custody.
The remote command is an installation contract only; the launcher and its
protected invocation-lease lookup are not present or verified by this change.
No workflow, action, CLI, service, toolchain, consumer pin, release, host, or
live deployment is activated here. A future controller must separately prove
WM-SEC-17 authorization and freshness, dispatch reservation, cross-channel
exclusion, target invocation lease, protected launcher installation, scoped
identity custody, pinned toolchain/adoption, and representative image and
static staging plus independent recovery.
