# Changelog

All notable changes to `workflow-templates` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Consumers should pin composite-action `uses:` references to a released
tag, not `@main`. The current stable tag is documented below.

---

## [Unreleased]

### Removed (BREAKING)

Deleted the three deprecated root-SSH provisioning composite actions, fully
superseded by the Ansible path (`webapp-management/ansible/site.yml` via the
`biglerconsult.infra` collection) run from `ansible-provision.yml`:

- `actions/provision-server` — fresh-host bootstrap (used unsigned `curl | sh`
  Docker install; SSHed as root)
- `actions/update-server` — idempotent re-apply (SSHed as root)
- `actions/sync-ssh-access` — `authorized_keys` sync from `access/*.pub`
  (now handled by `site.yml` pre_tasks)

No consumers remained at deletion time (verified across all repos). Existing
release tags (≤ v2.0.4) still contain these actions, so pinned references are
unaffected; only `@main` consumers — of which there are none — would break.

## [2.0.3] - 2026-05-24

### Added

`kuma_sync.py` now auto-derives **staging** Kuma monitors from
`project.yaml` `environments.staging.domains`, in addition to the
existing production-monitor derivation.

**Background**: pre-v2.0.3, only production monitors were
auto-generated (`<prefix>-frontend`, `<prefix>-healthz` pointing at
`environments.production.domains[0]`). Staging deployments had no
uptime monitoring unless declared manually via `monitoring.extra`.

### Behaviour

For each project.yaml where `environments.staging.domains` is non-empty,
the following monitors are now auto-created on next
`register-kuma-monitors` run:

```
<name_prefix>-staging-frontend  → https://<staging-domain[0]>
<name_prefix>-staging-healthz   → https://<staging-domain[0]><healthz_path>
```

Defaults (interval, max_retries, retry_interval, healthz_path,
name_prefix) follow the same `monitoring:` overrides as production.

### Opt-out

Apps that don't want auto-staging monitors can opt out via:

```yaml
monitoring:
  skip_staging: true
```

Implicit-skip: when `environments.staging.domains` is missing or empty
(e.g. apps without a staging environment), no staging monitors are
created — no opt-out required.

### Migration

For each caller pinned to `register-kuma-monitors@v2.0.1`:

```diff
-uses: ...register-kuma-monitors@v2.0.1
+uses: ...register-kuma-monitors@v2.0.3
```

No input contract changes. After the bump, the next
`workflow_dispatch` (or any `monitoring/**` / `project.yaml` change
that trips the paths-filter) creates the staging monitors. Apps using
the legacy `monitoring/monitor.yml` (currently: `hram`) are unaffected
— derivation only runs when `monitor.yml` is absent.

### Security

Audit pass 2026-05-24 (sec_reviewer) addressed 5 P2 findings in
workflow-templates. Behaviour-preserving hardening; no input contract
changes for the kuma sync composites.

- **INFRA-NEW-tailscale-version-latest**: `tailscale/github-action` had
  `version: latest` in 12 composites, downloading the Tailscale client
  binary from the Tailscale CDN at runtime without pinning. Pinned to
  `version: "1.98.3"` across all 12 composites
  (`backup`, `deploy-app`, `deploy-traefik`, `janitor`, `maintenance`,
  `provision-server`, `register-kuma-monitors`, `restore`,
  `restore-dest-import`, `sync-kuma-notifications`, `sync-ssh-access`,
  `update-server`). The action validates the binary hash when an
  explicit version is supplied.
- **INFRA-NEW-restore-xserver-stale-ssh-key**: removed the now-unused
  `ssh_private_key: ${{ secrets.SSH_PRIVATE_KEY }}` line from
  `restore-cross-server.yml`. `restore-dest-import` is
  Tailscale-SSH-only since v2.0.0 and silently ignored the input, but
  the secret reference still appeared in the `with:` block.
- **INFRA-NEW-ops-scripts-ref-mutable**: `restore-cross-server.yml`
  `ops_scripts_ref` default changed from `'main'` (moving ref) to `''`,
  with a hard-fail in the `validate` job that rejects empty values and
  anything that isn't a FULL 40-char commit SHA or a SemVer tag (`v1.2.3`
  or `v1.2.3-rc1`). Short SHAs are deliberately rejected because 7-char
  hex strings can collide with branch names (`cafe123`, `feedbed1`),
  defeating the drift-prevention goal. Prevents silent prod-host drift
  via webapp-ops-scripts main. No external callers —
  `webapp-management/.github/workflows/restore-cross-server.yml` is a
  separate `workflow_dispatch` orchestrator and does not call this
  reusable workflow.
- **INFRA-NEW-provision-curl-pipe-sh**: `provision-server` composite is
  now formally DEPRECATED in the action description and refuses to run
  unless the caller passes `legacy_acknowledge: true`. The unsigned
  `curl https://get.docker.com | sh` pipeline remains as break-glass
  only; supported path is `webapp-management/.github/workflows/ansible-provision.yml`.
- **INFRA-NEW-kuma-prune-injection**: `sync-kuma-notifications` and
  `register-kuma-monitors` previously interpolated `inputs.prune`,
  `inputs.config_path`, and `inputs.project_yaml_path` directly into
  the shell body via `${{ ... }}`. Routed through `env:` (S20-pattern)
  so caller-supplied values containing shell metacharacters cannot
  escape the variable.

### Fixed (pre-existing, discovered during security review)

- `restore-cross-server.yml` reusable workflow's `dest_import` job did
  not pass `ts_oauth_client_id` / `ts_oauth_secret` to
  `restore-dest-import`, which has required these inputs since v2.0.0
  (Tailscale-SSH-only). The workflow had zero external callers, so the
  latent runtime breakage never surfaced. Added `TS_OAUTH_CLIENT_ID`
  and `TS_OAUTH_SECRET` to the workflow `secrets:` declaration and
  forwarded them into the composite call site.
- `restore-cross-server.yml` self-checkout `ref:` (used to load
  `./.github/actions/...` composites inside both `source_export` and
  `dest_import` jobs) was pinned to a pre-v2.0.0 SHA
  (`7177b1a6...`). That tree carried the legacy `restore-dest-import`
  with `ssh_private_key: required: true` and no Tailscale inputs —
  meaning the v2.0.0 composite contract documented in v2.0.0's
  CHANGELOG was never actually loaded by this workflow. Bumped to
  `e989af5fd68f6c8baa8ce24bba7ccd2a6735e6da` (v2.0.2). Phase 2 (v2.0.3
  release tag cut) will bump it again to the v2.0.3 SHA.

---

## [2.0.2] - 2026-05-24

### Fixed

`deploy-app` server-side lock-file is now **server-wide** instead of
per-REMOTE_PATH (per-app).

**Background**: pre-v2.0.2 the lock was `/tmp/deploy-${REMOTE_PATH-slug}.lock`,
so each app had its own lock. When 7 apps push dependency bumps to
`develop` simultaneously (e.g. a bulk `django-core-micha` version bump
across all tenants), 7 docker builds run in parallel on a single staging
server. With ~700MB peak memory per build, this exceeds a 3.7GiB server
and triggers OOM-cascades (systemd-journald died, full reboot needed).

GitHub Actions `concurrency.group` is repository-scoped — it doesn't
help across 7 different app repos. Server-side flock is the only
cross-app coordination point.

### Change

```diff
-LOCK_NAME="$(printf '%s' "$REMOTE_PATH" | sed 's|/|_|g; s|^_||')"
-LOCK_FILE="/tmp/deploy-${LOCK_NAME}.lock"
+LOCK_FILE="/tmp/deploy-server.lock"
```

Effect:
- rsync still runs parallel (low memory)
- docker build + compose up + migrate **serialize** across all apps
  on the same server
- 7-app bulk-push wave: ~7 × 2min = ~14 min total wall time
  instead of failed-with-OOM at the 4th-5th simultaneous build

### Migration

For each caller pinned to `@v1.11.1` or `@v2.0.0` deploy-app:

```diff
-uses: ...deploy-app@v1.11.1
+uses: ...deploy-app@v2.0.2
```

No input contract changes from v1.11.1 → v2.0.2. Just bump the tag.

---

## [2.0.1] - 2026-05-24

### Fixed / Refactored

`register-kuma-monitors` and `sync-kuma-notifications` switch from
SSH-tunnel-into-Kuma (`ssh -fNL 3001:localhost:3001`) to **direct HTTPS
over Tailnet** via Tailscale Serve.

**Background**: v2.0.0 made these two composites Tailscale-SSH-only.
But the SSH-tunnel pattern continued to require ssh_host/ssh_user
inputs that pointed at the Kuma server. If the secret carried a
public IP (not a Tailnet hostname), v2.0.0 would fail at the
SSH-handshake step with `Permission denied (publickey,password)`.
v2.0.1 removes the SSH layer entirely.

**Architectural change**: Kuma host (today: staging) runs
```
tailscale serve --bg --https=8443 http://localhost:3001
```
which exposes Kuma as `https://<host>.tail990d7f.ts.net:8443` to any
tailnet member. CI joins tailnet ephemerally and calls the URL
directly — no SSH, no port-forward, no `pkill` cleanup.

### Inputs changed (breaking)

Both composites:

- **Removed**: `ssh_host`, `ssh_user`
- **Added**: `kuma_url` (default `https://staging.tail990d7f.ts.net:8443`)

Callers must drop ssh_host/ssh_user lines; kuma_url default is fine
for staging-hosted Kuma.

### Migration

For each caller pinned to `@v2.0.0`:

```diff
- uses: ...register-kuma-monitors@v2.0.0
  with:
    config_path: monitoring/monitor.yml
-   ssh_host: ${{ secrets.KUMA_SSH_HOST }}
-   ssh_user: ${{ secrets.KUMA_SSH_USER }}
    ts_oauth_client_id: ${{ secrets.TS_OAUTH_CLIENT_ID }}
    ts_oauth_secret: ${{ secrets.TS_OAUTH_SECRET }}
    kuma_user: ${{ secrets.KUMA_AUTOMATION_USER }}
    kuma_password: ${{ secrets.KUMA_AUTOMATION_PASSWORD }}
+   # kuma_url defaults to staging-tailnet; no override needed
```

Plus: `KUMA_SSH_HOST`, `KUMA_SSH_USER`, `KUMA_SSH_PRIVATE_KEY` GH-env
secrets become orphaned. Cleanup in a follow-up pass.

### Why this is "the right way"

v1.x: SSH-tunnel to private port. Works, but requires SSH-tunnel
plumbing, key auth, port-forwarding, cleanup-pkill. Three layers of
abstraction for what is fundamentally "talk HTTP to a service".

v2.0.0: same SSH-tunnel-pattern but with Tailscale-SSH auth. Inherits
all of v1.x's complexity, fails when KUMA_SSH_HOST is a public-IP.

v2.0.1: direct HTTPS via Tailnet. Auth is Tailnet-identity (ACL-gated),
transport is plain HTTPS with Let's-Encrypt-issued cert. Two layers
instead of five.

---

## [2.0.0] - 2026-05-24

### Breaking — Tailscale-SSH-only across all SSH-using composites

`ssh_private_key` (and `ssh_private_key_root` on update-server) input
is **removed** from every SSH-using composite. Authentication is
Tailnet-identity-based via tag:ci-deploy and the tailnet ACL
`ssh:`-section (Tailscale-SSH server-side, OpenSSH client reports
`Authenticated using "none"`).

`ts_oauth_client_id` + `ts_oauth_secret` change from optional to
**required** on every SSH-using composite. The runner joins the
tailnet ephemerally via OIDC at the start of each composite, then all
remote operations use plain `ssh user@<host>.tail990d7f.ts.net`.

The previously-experimental `tailscale ssh`-wrapper + rsync-flag-
translation script in `deploy-app` v1.11.x is **removed**. v2.0.0
uses plain `ssh` everywhere — simpler, no wrapper. Tailscale-SSH
server-side intercepts SSH connections at the WireGuard layer and
authorises the Tailnet identity before any OpenSSH auth method is
tried; the client doesn't need to know about Tailscale's SSH CA.

### Composites in this release

| Composite | Notes |
|---|---|
| deploy-app | Simplified — drops the /tmp/ts-ssh-rsync wrapper. rsync uses `-e ssh`. |
| deploy-traefik | Tailscale-SSH-only. |
| janitor | Tailscale-SSH-only. **GHA-validated on main-prod.** |
| update-server | Tailscale-SSH-only. SSHes as **root** — requires Tailnet ACL ssh:-rule to include `users: ["root"]`. |
| sync-ssh-access | Tailscale-SSH-only. SSHes as **root** — same ACL requirement. |
| maintenance | Tailscale-SSH-only. Uses deploy + sudo (NOPASSWD via biglerconsult.infra.deploy_user role). |
| backup | Tailscale-SSH-only. |
| restore + restore-dest-import | Tailscale-SSH-only. |
| sync-kuma-notifications | Tailscale-SSH-only. `ssh -fNL` port-forward works over plain ssh (not `tailscale ssh`). |
| register-kuma-monitors | **First Tailnet adoption** — was raw-SSH only in v1.x. Now Tailscale-SSH-only. |

### Not migrated (intentional)

- **provision-server** — bootstrap-before-Tailnet-membership. Public-IP
  SSH-key path stays (the host hasn't joined the tailnet yet on first run).
  v1.10.x line continues.
- **restore-source-export** — never SSHes (pulls restic directly from B2
  to runner). No change needed.

### Caller migration

For each caller pinned to `@v1.x.x`:

1. Add `ts_oauth_client_id` + `ts_oauth_secret` inputs (sourced from
   `proton://Projekt Webapp-Management/Cloudflare API/ts_oauth_*` via
   sync-secrets to GH-env-secrets).
2. Drop `ssh_private_key` / `ssh_private_key_root` input.
3. Set `ssh_host` to the Tailnet hostname (`<target>.tail990d7f.ts.net`).
4. Bump `@v1.x.x` → `@v2.0.0` (or `@v2` floating).

### Tailnet ACL prerequisite

Composites that SSH as `root` (update-server, sync-ssh-access) require
the tailnet ACL `ssh:`-section to include `root` in the users[] list
for tag:ci-deploy → tag:server-*. The security posture is unchanged
from v1.x (CI had `ssh_private_key_root` in secrets); the auth method
just moves from key-based to Tailnet-identity-based.

### Errata

The v1.0.0 CHANGELOG-note ("v2.0.0 will remove the public-IP SSH
fallback") understated the scope. v2.0.0 doesn't just remove the
fallback — it removes the SSH-key auth entirely. The public-IP path
was already a defensive fallback in v1.x; with v2.0.0 there's no
key-based auth path at all (except provision-server, which is
out-of-scope).

---

## [1.11.1] - 2026-05-24

### Fixed

`deploy-app` v1.11.0 rsync step failed because `rsync -e "tailscale ssh"`
invokes the remote-shell tool with ssh-style flags (`-l <user> <host>`),
and `tailscale ssh` only accepts `[user@]<host>` form. Symptom:

```
flag provided but not defined: -l
SSH to a Tailscale machine
USAGE
  tailscale ssh [user@]<host> [args...]
rsync error: error in rsync protocol data stream (code 12)
```

v1.11.1 installs a small wrapper script `/tmp/ts-ssh-rsync` before the
rsync step that translates `-l user host args...` to
`tailscale ssh user@host args...`. The wrapper also silently drops
ssh-style `-p`/`-i` flags rsync may inject (Tailnet doesn't need them).
File-secret and docker-compose steps are unaffected — they invoke
`tailscale ssh user@host bash <<EOF` directly with the canonical
`user@host` form.

Callers already on `@v1.11.0` should bump to `@v1.11.1`. The
ts_oauth_* secrets and project.yaml-derived ssh_host remain unchanged.

---

## [1.11.0] - 2026-05-23

### Added

`deploy-app` composite action gains **Tailscale-SSH-only** auth path. The
runner joins the tailnet ephemerally via OIDC (`tailscale/github-action@v3`,
`ts_oauth_client_id` + `ts_oauth_secret` inputs, now **required**) and uses
`tailscale ssh deploy@<host>` for rsync + remote docker compose orchestration.
Tag-based identity (`tag:ci-deploy → tag:server-*` as `deploy`) replaces
key-based auth — auth decisions live in the tailnet ACL `ssh:`-section.

`ssh_host` becomes optional. When empty, the composite derives the Tailnet
hostname from `project.yaml` → `environments[env].server` +
`.tail990d7f.ts.net` suffix. Explicit `ssh_host` input still works as an
override (e.g. for ad-hoc Tailnet hostnames).

`ssh_user` becomes optional with default `deploy` (matches the
`biglerconsult.infra.deploy_user` ansible role default).

### Removed

`ssh_private_key` input. v1.11.0 has no key-based SSH path at all.

`🔑 Write SSH key` and `⚙️ Add known hosts` steps. Tailscale-SSH cert flow
replaces both — host identity is handled by Tailscale's SSH-CA, and no
per-runner private key exists.

`appleboy/ssh-action` calls in `🔐 Provision optional file secrets` and
`🐳 Build, restart & migrate`. Both steps now use inline `tailscale ssh
deploy@<host> bash <<EOF`-heredoc pattern. File-secret contents are
base64-encoded locally and decoded remote-side, so multi-line PEMs survive
the stdin transport intact.

### Errata

The v1.9.0 CHANGELOG-note ("last of SSH-using composite actions to gain
Tailnet support") was incorrect — `deploy-app` was overlooked. v1.11.0
closes that gap. The v1.x Tailnet migration is now genuinely complete
across:

- janitor (v1.1.0)
- maintenance (v1.2.0)
- sync-ssh-access (v1.3.0)
- update-server (v1.4.0)
- sync-kuma-notifications (v1.5.0)
- backup (v1.6.0)
- deploy-traefik (v1.7.0 idempotent up + v1.9.0 Tailnet)
- restore + restore-dest-import (v1.8.0)
- provision-server (v1.10.0)
- **deploy-app (v1.11.0)** ← this release

`register-kuma-monitors` still uses raw SSH and is scoped for a future
v1.12.0 release. `restore-source-export` has no SSH and remains unchanged.

### Breaking

`ssh_private_key` removal is a breaking change for any caller that has not
already populated `ts_oauth_client_id` + `ts_oauth_secret`. Callers must:

1. Pin to `@v1.11.0` (or `@v1`-floating once advanced).
2. Drop `ssh_host` / `ssh_user` / `ssh_private_key` lines from the caller's
   `uses:` block — or keep `ssh_host` only as an explicit Tailnet-hostname
   override.
3. Pass `ts_oauth_client_id` + `ts_oauth_secret` (synced from
   `proton://Projekt Webapp-Management/Cloudflare API/ts_oauth_*` via
   `sync-secrets` into the app's per-environment GitHub secrets).
4. Server-side prerequisite: `tailscale up --ssh` is active and the tailnet
   ACL `ssh:`-section authorises `tag:ci-deploy → tag:server-*` as `deploy`
   (see webapp-management ansible group_vars/all.yml).

The v1.10.x line keeps working for callers that need more migration runway.

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
