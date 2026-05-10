# workflow-templates

Shared reusable GitHub Actions workflows for bigler-webapps infrastructure.

## Usage

Call any workflow from your infra repo's `.github/workflows/`:

```yaml
jobs:
  backup:
    uses: bigler-webapps/workflow-templates/.github/workflows/backup.yml@main
    secrets: inherit
```

## Reusable workflows

| Workflow | Trigger | Purpose |
|---|---|---|
| `backup.yml` | `workflow_call` | Backup + verify + optional staging sync |
| `actions/restore` | composite action | Restore app DB to staging from Restic (called from a job with `environment:`) |
| `actions/deploy-app` | composite action | Deploy a Django/React app via rsync + Docker (called from a job with `environment:`) |
| `actions/deploy-traefik` | composite action | Deploy Traefik infrastructure stack (called from a job with `environment:`) |
| `actions/sync-ssh-access` | composite action | Sync SSH authorized_keys from `access/` (called from a job with `environment:`) |
| `provision-server.yml` | `workflow_call` | Full server provisioning (users, Docker, firewall) |
| `actions/maintenance` | composite action | Weekly apt updates + conditional reboot (called from a job with `environment:`) |
| `actions/janitor` | composite action | Monthly Docker prune + disk report (called from a job with `environment:`) |

## Calling repo requirements

Each infra repo that calls these workflows must provide:

- `inventory/inventory.yaml` — server targets and roles
- `access/` — SSH public keys (`root/`, `deploy/`, `infrastructure/`)
- `.github/scripts/resolve_inventory_targets.py` — target resolver (copy from this repo)
- `backup/infra_paths.txt` — static server paths for restic (used by `backup.yml`)

## Scripts

Runtime ops scripts live in
[bigler-webapps/webapp-ops-scripts](https://github.com/bigler-webapps/webapp-ops-scripts).
The `backup.yml` and `janitor.yml` workflows check them out automatically.
