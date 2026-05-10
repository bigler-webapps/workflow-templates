# workflow-templates

Shared composite actions for bigler-webapps infrastructure.

## Usage

Call any composite action from your infra repo's `.github/workflows/` job — always from a job with `runs-on:` + `environment:` + `steps:` (NOT from a `uses:`-only job, since `environment:` is incompatible with `uses:` at job level):

```yaml
jobs:
  deploy:
    runs-on: ubuntu-latest
    environment: main-prod
    steps:
      - uses: actions/checkout@v4
      - uses: bigler-webapps/workflow-templates/.github/actions/backup@main
        with:
          target: main-prod
          ssh_host: ${{ secrets.SSH_HOST }}
          # ... explicit secret inputs
```

This pattern is required because reusable workflows (`uses: org/repo/.github/workflows/X.yml@main`) cannot access the caller's environment-level secrets when the caller can't set `environment:` (e.g., matrix-driven dispatch).

## Composite actions

| Action | Purpose |
|---|---|
| `actions/deploy-app` | Deploy a Django/React app via rsync + Docker |
| `actions/deploy-traefik` | Deploy Traefik infrastructure stack |
| `actions/backup` | Backup + verify + optional staging sync (Restic to B2) |
| `actions/restore` | Restore app DB to staging from Restic |
| `actions/sync-ssh-access` | Sync SSH authorized_keys from `access/` |
| `actions/provision-server` | Bootstrap base server (packages, Docker, deploy user, firewall) |
| `actions/maintenance` | Weekly apt updates + conditional reboot |
| `actions/janitor` | Monthly Docker prune + disk report |

## Calling repo requirements

Each infra repo that calls these actions must provide:

- `inventory/inventory.yaml` — server targets and roles
- `access/` — SSH public keys (`root/`, `deploy/`, `infrastructure/`)
- `.github/scripts/resolve_inventory_targets.py` — target resolver (copy from this repo)
- `backup/infra_paths.txt` — static server paths for restic (used by `actions/backup`)

## Scripts

Runtime ops scripts live in
[bigler-webapps/webapp-ops-scripts](https://github.com/bigler-webapps/webapp-ops-scripts).
The `actions/backup`, `actions/restore`, and `actions/janitor` actions expect them checked out at a path provided via the `ops_scripts_path` input (default `ops-scripts`).
