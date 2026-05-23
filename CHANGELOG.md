# Changelog

All notable changes to `workflow-templates` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Consumers should pin composite-action `uses:` references to a released
tag, not `@main`. The current stable tag is documented below.

---

## [Unreleased]

### Planned for v2.0.0

Once all composite actions that SSH have shipped opt-in Tailnet support
on the v1.x line, v2.0.0 will **remove the public-IP SSH fallback** and
require Tailnet-only operation. That removal is the breaking change.

Consumers who want to be ready for v2: pin to `@v1.x` today, set
`ts_oauth_client_id` + `ts_oauth_secret` on every caller workflow, point
`ssh_host` at the Tailnet hostname instead of the public IP. Then the
v2 cutover is a no-op for behavior, only a tag bump.

---

## [1.10.0] - 2026-05-23

### Added

`provision-server` composite action gains the same three optional Tailnet
inputs (`ts_oauth_client_id`, `ts_oauth_secret`, `ts_tag`). Less
commonly applicable than the other migrated actions — provision-server
typically runs against a brand-new host that hasn't joined the tailnet
yet, in which case the inputs stay empty and the action SSHes via the
caller-provided ssh_host (public IP). For the rare break-glass case
where Ansible-Foundation needs to be re-applied to an already-
tailnet-connected host, the Tailnet path is now available.

### Removed

The pre-create-wireguard-subdir block in the bootstrap script is gone.
WireGuard was retired across all bigler-webapps servers earlier today
in favor of Tailscale — a fresh-bootstrap host has no need for that
bind-mount directory.

Backwards-compatible. Existing callers without ts_oauth_* inputs see
no behavior change beyond the wireguard-subdir cleanup.

---

## [1.9.0] - 2026-05-23

### Added

`deploy-traefik` composite action gains the same three optional Tailnet
inputs (`ts_oauth_client_id`, `ts_oauth_secret`, `ts_tag`). When
provided, the runner joins the tailnet ephemerally before rsync + SSH
into the target server. The idempotent `up -d --remove-orphans` flow
introduced in v1.7.0 is unaffected by the network-path swap.

Last of the SSH-using composite actions to gain Tailnet support. After
this release, the v1.x line is feature-complete on Tailnet opt-in
across:

- janitor (v1.1.0)
- maintenance (v1.2.0)
- sync-ssh-access (v1.3.0)
- update-server (v1.4.0)
- sync-kuma-notifications (v1.5.0)
- backup (v1.6.0)
- restore + restore-dest-import (v1.8.0)
- deploy-traefik (v1.9.0)

Not migrated (intentional):
- restore-source-export — pulls restic directly from B2 on the runner,
  no SSH
- provision-server — bootstrap workflow, runs on a fresh host before
  it joins the tailnet; the public-IP path stays in for break-glass

Backwards-compatible. Callers without ts_oauth_* inputs keep public-IP
SSH behavior.

---

## [1.8.0] - 2026-05-23

### Added

Restore-family composite actions gain the same three optional Tailnet
inputs (`ts_oauth_client_id`, `ts_oauth_secret`, `ts_tag`):

- `restore` — single-app restore. SSHes to one server. Tailnet path
  added.
- `restore-dest-import` — destination-side of the cross-server restore.
  SSHes to the dest server. Tailnet path added.

**Not changed:** `restore-source-export` does not SSH (it pulls restic
snapshots directly from B2 on the runner, encrypts, uploads as
artifact). No Tailnet step needed and none added — the source-side
handoff path stays pure-internet → B2.

Backwards-compatible across both actions.

---

## [1.7.0] - 2026-05-23

### Fixed

`deploy-traefik` composite action: `docker compose up -d` is now run
idempotently so a no-op deploy doesn't trigger a routine container
recreate and a small downtime window. See #12 for the implementation
detail and motivation.

Backwards-compatible.

---

## [1.6.0] - 2026-05-23

### Added

`backup` composite action gains the same three optional Tailnet inputs
(`ts_oauth_client_id`, `ts_oauth_secret`, `ts_tag`). When provided, the
runner joins the tailnet ephemerally before the rsync + SSH-driven
restic backup, verify, and (optional) staging-sync steps. The
backup.py / verify_backup.py / restore.py invocations on the server
are unchanged — only the network path differs. Backwards-compatible.

---

## [1.5.0] - 2026-05-23

### Added

`sync-kuma-notifications` composite action gains the same three
optional Tailnet inputs (`ts_oauth_client_id`, `ts_oauth_secret`,
`ts_tag`). When provided, the runner joins the tailnet ephemerally
before opening the SSH tunnel into the Kuma server. The port-forward
mechanism is unchanged — `ssh -fNL` works identically over a tailnet
hop. Backwards-compatible.

---

## [1.4.0] - 2026-05-23

### Added

`update-server` composite action gains the same three optional Tailnet
inputs. The action uses `appleboy/ssh-action` for the actual remote
execution, which connects to whatever host string is provided — so
the path swap is transparent. Backwards-compatible.

---

## [1.3.0] - 2026-05-23

### Added

`sync-ssh-access` composite action gains the same three optional
Tailnet inputs as janitor (v1.1.0) and maintenance (v1.2.0). The
key-sync flow itself (`scp` + remote `bash` block) is unchanged; only
the network path differs when the Tailnet inputs are provided.
Backwards-compatible.

---

## [1.2.0] - 2026-05-23

### Added

`maintenance` composite action gains the same three optional Tailnet
inputs as janitor (v1.1.0):

- `ts_oauth_client_id`
- `ts_oauth_secret`
- `ts_tag` (default `tag:ci-deploy`)

When provided, the runner joins the tailnet ephemerally before any SSH
step, and `ssh_host` is expected to be a Tailnet hostname (or IP).
When empty, behavior is unchanged — public-IP SSH continues to work.

Reboot/post-reboot SSH-reconnect behaviour is unaffected: the
ephemeral tailnet node stays up for the lifetime of the workflow job;
when the target server's `tailscaled` comes back after reboot, the
existing wait-for-host SSH loop reconnects via the tailnet route
without any additional handling.

Backwards-compatible. Existing callers see no change.

---

## [1.1.0] - 2026-05-23

### Added

`janitor` composite action gains three optional inputs that let the
runner join the tailnet ephemerally via OIDC before SSHing into the
target:

- `ts_oauth_client_id` — Tailscale OAuth client ID
- `ts_oauth_secret` — Tailscale OAuth client secret
- `ts_tag` — tailnet tag for the ephemeral node (default `tag:ci-deploy`)

When both `ts_oauth_client_id` and `ts_oauth_secret` are provided, the
action runs `tailscale/github-action@v3` before any SSH step. The caller
is then expected to pass a Tailnet hostname (or IP) as `ssh_host`
instead of a public IP. When the two inputs are empty (the default),
the action falls back to the previous public-IP SSH path unchanged —
**backwards-compatible**, no migration required for current callers.

This is the first composite action to support the Tailnet path. The
remaining SSH-using actions (`backup`, `deploy-app`, `deploy-traefik`,
`maintenance`, `provision-server`, `restore`, `restore-cross-server`,
`sync-ssh-access`, `sync-kuma-notifications`, `register-kuma-monitors`,
`update-server`) will gain the same opt-in inputs in subsequent v1.x
releases.

### Notes

- `tailscale/github-action@v3` is pinned to the floating tag with a
  `# TODO S33: SHA-pin via Renovate` comment, consistent with the
  existing pattern for other third-party actions in this repo.
- Pre-requisites for callers wanting to use the Tailnet path: a
  Tailscale OAuth client with `Auth Keys: Write` scope restricted to
  `tag:ci-deploy`, and a tailnet ACL that authorises `tag:ci-deploy`
  to reach the server tags on port 22.

---

## [1.0.0] - 2026-05-22

First disciplined SemVer release. Snapshots all composite actions at
their current state, including the two recently merged improvements
that were not yet in the older `v1` floating tag (see Notes below).

### Added
- `terraform-plan` and `terraform-apply` accept a new opt-in
  `cloudflare_access_api_token` input, wired through as
  `TF_VAR_cloudflare_access_api_token`. Lets Terraform modules manage
  Cloudflare Zero Trust Access resources with a token that's separate
  from the DNS/Tunnel one. Backwards-compatible: callers that do not
  set the input continue to work unchanged.

### Fixed
- `deploy-traefik` runs `docker compose down --remove-orphans` before
  `up` to kill stale hash-prefixed orphan containers. Prevents the
  container-name conflict that locked Traefik out during parallel
  deploy runs.

### Notes
- The older floating `v1` tag (pointing at the 2026-05-19 state) is
  outdated and **does not** contain the two changes above. Consumers
  pinned to `@v1` should migrate to `@v1.0.0` or wait for `v1` to be
  advanced (see migration note below).

### Migration

For consumers currently on `@main` or `@<commit-sha>`:

```diff
- uses: bigler-webapps/workflow-templates/.github/actions/deploy-traefik@main
+ uses: bigler-webapps/workflow-templates/.github/actions/deploy-traefik@v1.0.0
```

For consumers currently on `@v1` (the floating tag from 2026-05-19):

```diff
- uses: bigler-webapps/workflow-templates/.github/actions/deploy-traefik@v1
+ uses: bigler-webapps/workflow-templates/.github/actions/deploy-traefik@v1.0.0
```

The floating `v1` tag may be advanced to `v1.0.0` at a later point so
that consumers on `@v1` automatically pick up the current state without
re-pinning. That advancement will be announced explicitly.

---

## [v1] - 2026-05-19

Pre-SemVer floating tag. Captured the repository state when the
`staging-health` reusable workflow landed. Not maintained as a separate
branch — superseded by `v1.0.0`.

This tag is kept in place for any consumer that pinned to it explicitly,
but should not be used for new caller workflows.
