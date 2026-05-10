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
| `restore.yml` | `workflow_call` | Restore app DB to staging from Restic |
| `deploy-app.yml` | `workflow_call` | Deploy a Django/React app via rsync + Docker |
| `actions/deploy-traefik` | composite action | Deploy Traefik infrastructure stack (called from a job with `environment:`) |
| `sync-ssh-access.yml` | `workflow_call` | Sync SSH authorized_keys from `access/` |
| `provision-server.yml` | `workflow_call` | Full server provisioning (users, Docker, firewall) |
| `maintenance.yml` | `workflow_call` | Weekly apt updates + conditional reboot |
| `maintenance-permissions-sync.yml` | `workflow_call` | Fix cert/sync directory permissions |
| `janitor.yml` | `workflow_call` | Monthly Docker prune + disk report |
| `show_vpn_qr.yml` | `workflow_call` | Show WireGuard peer QR code |
| `bootstrap-server.yml` | `workflow_dispatch` | Bootstrap users/groups/dirs on existing server |
| `setup-server.yml` | `workflow_dispatch` | Legacy initial server setup |

## Calling repo requirements

Each infra repo that calls these workflows must provide:

- `inventory/inventory.yaml` — server targets and roles
- `access/` — SSH public keys (`root/`, `deploy/`, `infrastructure/`, `certfetcher/`)
- `.github/scripts/resolve_inventory_targets.py` — target resolver (copy from this repo)
- `backup/infra_paths.txt` — static server paths for restic (used by `backup.yml`)

## Scripts

Runtime ops scripts live in
[bigler-webapps/webapp-ops-scripts](https://github.com/bigler-webapps/webapp-ops-scripts).
The `backup.yml` and `janitor.yml` workflows check them out automatically.
