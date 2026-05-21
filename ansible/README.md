# biglerconsult.infra — Ansible collection

Reusable Ansible roles for base server provisioning of Ubuntu LTS hosts
running containerised webapp workloads. **Ubuntu LTS only (24.04 verified).**
Debian support is not implemented; the Docker apt repository URL is hard-coded
to the Ubuntu archive.

## Roles

| Role | Purpose |
|---|---|
| `base` | apt update/upgrade, common packages, kernel sysctl tuning for high-container-density hosts |
| `docker` | Docker CE + compose plugin from the official Docker apt repository |
| `deploy_user` | Create the `deploy` user, sudoers entry for maintenance, SSH `authorized_keys` |
| `ufw` | UFW default policies and a configurable list of allowed ports (default: SSH only) |
| `fail2ban` | fail2ban with a default sshd jail |
| `tailscale` | Tailscale install from the official apt repository plus idempotent `tailscale up` with pre-authorized key |

Each role wraps its tasks in a `become: true` block. Consumers do not need
to set `become` at the play level — the roles request privilege escalation
themselves.

Ordering matters: `docker` must run before `deploy_user`, because the
`deploy` user is added to the `docker` group which the Docker installation
creates. The `tailscale` role is independent of the others and may run at
any point in the play.

## Differences vs the legacy `provision-server` composite action

This collection is a deliberate, near-1:1 port of the inline bash in
`.github/actions/provision-server/action.yml`, with three intentional
behavior changes:

- **`ufw` defaults open only port 22.** The legacy action also opened
  80 and 443. The web stack is not in scope of this collection — the
  consuming tenant playbook is responsible for adding 80/443 (or any
  other ports) via the `ufw_allow_in` variable when a web stack is
  deployed on the host.
- **`deploy_user` requires an explicit `deploy_user_authorized_keys`
  list.** The legacy action implicitly copied `/root/.ssh/authorized_keys`
  to the deploy user. The new role takes an explicit list of public-key
  strings — the consumer must pass them. This is a deliberate move from
  implicit to explicit.
- **`restic` is not installed by any role here.** The legacy action
  installed it as part of the base package set. Restic is backup-tooling
  tightly coupled to the webapp-management tenant's backup layout and
  belongs in a tenant-specific role, not in a reusable base collection.

## Consumption

From a tenant repo's `requirements.yml`:

```yaml
collections:
  - name: biglerconsult.infra
    source: git+https://github.com/bigler-webapps/workflow-templates.git#/ansible
    type: git
    version: <tag-or-sha>
```

Install: `ansible-galaxy collection install -r requirements.yml`

Use roles as `biglerconsult.infra.base`, `biglerconsult.infra.docker`, etc.

## Out of scope

- Tenant-specific directory trees (e.g. `/srv/apps`, `/srv/backups`) — in
  the tenant playbook.
- Web stack (Traefik, application containers) — tenant responsibility.
- SSH host restrictions (e.g. binding SSH to a VPN interface) — deferred
  until a VPN role exists.
- Backup tooling (restic) — tenant responsibility.
