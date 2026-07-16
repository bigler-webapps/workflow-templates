# WORK_ORDERS.md — workflow-templates

Work-order register for this repo. Lightweight directory (not the full orders):
one row per WO with its implementation status. Convention, schema, and maintenance
rules are defined centrally in `webapps/AGENTS.md` → "Work-Order Register".

**Scope note:** this register starts with the 2026-07 tailnet-resilience workstream;
older work is in `git log`.

## Workstream prefixes

| Prefix | Workstream |
|---|---|
| `TS-*` | Tailnet/Tailscale CI resilience (probe-guard, joins, CDN dependency) |

Introduce a new prefix when none fits and add it here. New WOs always get a
prefixed ID; never reuse a bare flat number across workstreams.

## Register

| ID | Titel | Beschreibung | Datum | Status | Commit(s) | Notiz |
|---|---|---|---|---|---|---|
| TS-1 | Tailnet join resilience | staging-health raw join → tailnet-connect probe-guard; `use-cache` + re-probe-gated retry on cold joins; internal composite refs → v2.5.2 | 2026-07-16 | done (phase 1) | 67ba66f | Phase 1 landed on `main` (10 files: staging-health.yml, tailnet-connect/action.yml, 8 internal ref bumps); reviewed independently, no findings. Guardrail: never `tailscale reset` / unconditional `up` on the shared daemon (regression `40c70c4`, known-good `e196e6c`). Phase 2 = webapp-management `CI-1` (pin bumps), gated on tag `v2.5.2` (not yet created) and explicit operator go-ahead. deploy-app deliberately NOT converted (pinned `@v2.4.4` by app repos; convert next time it is touched) |
