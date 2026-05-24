# APP_FINDINGS.md — workflow-templates

Generated 2026-05-24 from a deep-security audit (sec_reviewer agent pass).
Cross-reference: `webapp-management/SECURITY_FINDINGS.md` (central tracking).

This repo hosts the reusable composite GitHub Actions consumed by all 7 app workflows + webapp-management. Any bug here is amplified platform-wide.

Already addressed at infra layer:
- S33 (third-party action SHA-pinning across all composites + workflows — 14 composites + 3 reusable workflows)
- S5/S21 (Denylist + double-filter in deploy-app/deploy-traefik)

---

## P2 — Major

### S176 — INFRA-NEW-tailscale-version-latest — `version: latest` on Tailscale install in 12 composites
**Severity:** P2
**File:** `.github/actions/{backup,deploy-app,deploy-traefik,janitor,maintenance,provision-server,register-kuma-monitors,restore,restore-dest-import,sync-kuma-notifications,sync-ssh-access,update-server}/action.yml`
**Confidence:** high
**Issue:** Every composite using `tailscale/github-action` passes `version: latest`. The action itself is digest-pinned to `@6cae46e...`, so action code is trusted — but it then downloads the Tailscale client binary from the Tailscale CDN at runtime without version pinning. A CDN compromise, DNS attack on `packages.tailscale.com`, or a regression in a Tailscale release silently affects ALL CI runs.
**Fix:** Pin `version:` to a specific Tailscale release string (e.g. `version: "1.80.3"`) across all 12 composite actions. Track via Renovate. The action validates the binary hash when an explicit version is supplied.

### S177 — INFRA-NEW-restore-xserver-stale-ssh-key — Reusable workflow still passes removed v2.0.0 input
**Severity:** P2
**File:** `.github/workflows/restore-cross-server.yml:176`
**Confidence:** high
**Issue:** Passes `ssh_private_key: ${{ secrets.SSH_PRIVATE_KEY }}` to `restore-dest-import@v2.0.0`. The v2.0.0 composite **removed** the `ssh_private_key` input — it's now Tailscale-SSH-only. GitHub Actions silently ignores undeclared inputs, but the secret reference still appears in the `with:` block. In debug-mode runs (`ACTIONS_STEP_DEBUG=true`) the key contents may be logged. If a future v2.x re-introduces the input, a bare SSH key would be unexpectedly activated.
**Fix:** Remove the `ssh_private_key: ${{ secrets.SSH_PRIVATE_KEY }}` line from `restore-cross-server.yml:176`. The composite no longer needs it.

### S178 — INFRA-NEW-ops-scripts-ref-mutable — `ops_scripts_ref` defaults to `'main'`
**Severity:** P2
**File:** `.github/workflows/restore-cross-server.yml:74`
**Confidence:** high
**Issue:** `ops_scripts_ref` default is `'main'` (moving ref). The destination-side `restore.py` (run by `--mode import-only` against production DBs) is therefore supplied from whatever HEAD is on `bigler-webapps/webapp-ops-scripts` at run time. A contributor with merge access to ops-scripts `main` can push and have arbitrary Python execute as `deploy` user on production hosts via the next cross-server restore. `webapp-management/.github/workflows/restore-cross-server.yml` correctly pins to a SHA — only the reusable workflow carries the unsafe default.
**Fix:** Change default to `''` and make the input required; or pick a pinned SHA as documented safe default and update deliberately on each tested ops-scripts bump.

### S179 — INFRA-NEW-provision-curl-pipe-sh — Legacy provision-server installs Docker via `curl | sh`
**Severity:** P2
**File:** `.github/actions/provision-server/action.yml:115`
**Confidence:** high
**Issue:** `curl -fsSL https://get.docker.com | sh` — executes unsigned, unversioned shell script from the internet as root. BGP/DNS hijack of `get.docker.com` during a bootstrap run → arbitrary root-level code execution on fresh server. Workflow is marked DEPRECATED (Ansible path preferred) but remains callable from `webapp-management/.github/workflows/provision-server.yml`.
**Fix:** Replace with Docker APT repository setup via verified GPG fingerprint. Or gate the DEPRECATED workflow behind an explicit "I-acknowledge-unsafe-path" input.

### S180 — INFRA-NEW-kuma-prune-injection — `prune` input inlined into shell condition
**Severity:** P2 (medium confidence)
**File:** `.github/actions/sync-kuma-notifications/action.yml:105`, `.github/actions/register-kuma-monitors/action.yml:131`
**Confidence:** medium
**Issue:** `if [ "${{ inputs.prune }}" = "true" ]` — `inputs.prune` is `required: false, default: 'false'`, accepting any string. Value containing `; malicious-command` is textually substituted before the shell sees it. The follow-up `PRUNE_FLAG` env-var binds either `--prune` or empty, so that downstream expansion is safe; the primary risk is the `if` condition itself.
**Fix:** `env: INPUT_PRUNE: ${{ inputs.prune }}` + `if [ "$INPUT_PRUNE" = "true" ]`.

---

## Residual risks

- Tailscale `version: latest` is the single largest supply-chain attack surface in CI right now. Pinning is straightforward but requires renovate-config setup.
- `restore-cross-server.yml`'s unsafe `ops_scripts_ref` default is mitigated as long as callers always override it — but a future caller forgetting to set it would re-introduce risk.
- The DEPRECATED `provision-server` composite is callable but should not be used. Consider archiving / gating behind an explicit `legacy: true` input.
