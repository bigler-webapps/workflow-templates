# A. Envelope

## Goal

`restore-source-export`'s `--host <source_target>` filter (INF-31) assumes the inventory target
name equals the source server's OS hostname. It does not, for `main-prod`: verified live via
`python3 -c "import os; print(os.uname().nodename)"` on both boxes during CI-12's own restore
precondition check — `innoservice-prod`'s OS hostname matches (`innoservice-prod`), `main-prod`'s
does not (`app-server`, a legacy naming leftover). `backup.py` tags every restic snapshot with
`os.uname().nodename`, so every `main-prod`-sourced restore has been failing
`No snapshot for source host 'main-prod' could be resolved` since the moment `v2.10.2`
(INF-31's fix) shipped — the fix is correct, it just has nothing to match against.

## Scope

Thread an optional `restic_host` override (default: fall back to `source_target`) from
webapp-management's own `project.yaml` through to the restic `--host` filter, for exactly the one
host that diverges.

## Tier

3 — shared restore composite in `workflow-templates`, consumed cross-repo. Same surface as INF-31.

## Non-goals

- Do not rename the server or its OS hostname — out of scope, unrelated risk.
- Do not add a general hostname-discovery mechanism — one documented deviation
  (`infra.servers.main-prod.restic_host` in `project.yaml`) is the whole fix; `resolve_inventory_targets.py`'s existing "only deviations need entries" convention already covers this.
- Do not touch `restore-dest-import` or `restore.yml`'s older `restore@v2.5.2` composite — neither
  does host-filtered `latest` resolution.

## Required tests

- `workflow-templates/.github/scripts/test_restore_source_export.py`: the `latest` branch falls
  back to `source_target` when `restic_host` is unset, and uses the override when set.
- `webapp-management/.github/scripts/test_resolve_inventory_targets.py` (new): `restic_host`
  present in the normalized target, defaulting to the target name.
- `webapp-management/.github/scripts/test_resolve_sync_staging_matrix.py`: matrix rows carry
  `restic_host`, sourced from `project.yaml`, defaulting to `source`.

---

# B/C. Implemented directly (operator-confirmed), not via Codex

Discovered and fixed inline during CI-12's restore-precondition verification, same day as INF-31.
Given the small, mechanical shape (thread one field through five files, no new mechanism), the
operator confirmed direct implementation over Codex-first. See `WORK_ORDERS.md` for the full
record: commits, independent `reviewer` verdict, and the tag/pin-bump sequence.
