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
| `CI-*` | CI workflows / composite actions (non-tailnet) |

Introduce a new prefix when none fits and add it here. New WOs always get a
prefixed ID; never reuse a bare flat number across workstreams.

## Register

| ID | Titel | Beschreibung | Datum | Status | Commit(s) | Notiz |
|---|---|---|---|---|---|---|
| TS-1 | Tailnet join resilience | staging-health raw join → tailnet-connect probe-guard; `use-cache` + re-probe-gated retry on cold joins; internal composite refs → v2.5.2 | 2026-07-16 | done | 67ba66f | Phase 1 on `main` (10 files), reviewed independently, no findings; tag `v2.5.2` = `2c59eab` (register commit on top of `67ba66f`, self-refs verified). Phase 2 = webapp-management `CI-1` (`a312525` + `0c581ef`, 8 pins); first live cold-join green with cache seeded (deploy-traefik run 29522808584: probe false → CDN download → "Cache saved … tailscale-1.98.4", no "Unexpected input" warning — subsequent runs restore from actions/cache). Guardrail: never `tailscale reset` / unconditional `up` on the shared daemon (regression `40c70c4`, known-good `e196e6c`). deploy-app deliberately NOT converted (pinned `@v2.4.4` by app repos; convert next time it is touched) |
| CI-1 | Unique per-run CI image tag | app-ci backend job: replace the fixed `ci-backend:test` with `ci-backend:${{ github.run_id }}-${{ github.run_attempt }}` in build-push tags AND the `docker run` pytest step, plus an `if: always()` `docker rmi` cleanup step — the fixed tag races between concurrent backend jobs on the shared daemon and executes the WRONG repo's tests | 2026-07-17 | done | 53e862c | Proven cross-repo execution: jg-ferien PR #71 (run 28984827793, 00:16:39Z) collected hram's `runs/tests/test_golden_regression.py`, hram CI started 34s later; hram develop CI red 07-15/16 was survey_app's `surveys/test_ws_inventory.py`. app-ci is consumed `@main` → fix is fleet-live on push. Companion issue (app repos, separate WO): jg-ferien test_settings lacks CHANNEL_LAYERS/CACHES overrides (dcm 2.25 defaults → `redis://redis` unresolvable under `--network host`) |
