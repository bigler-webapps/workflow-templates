# Changelog

All notable changes to `workflow-templates` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Consumers should pin composite-action `uses:` references to a released
tag, not `@main`. The current stable tag is documented below.

---

## [Unreleased]

### Planned for v2.0.0

Tailnet-aware CI auth: composite actions will require
`tailscale/github-action` before SSH steps so that consumers can close
public SSH (port 22) and route CI access exclusively through the tailnet.

This will be a **breaking change**. Callers will need to provide a
Tailscale OAuth client and grant `tag:ci-deploy` (or similar) in the
tailnet ACL before they can upgrade.

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
