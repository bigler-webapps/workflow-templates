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
| CI-1 | Unique per-run CI image tag | app-ci backend job: replace the fixed `ci-backend:test` with `ci-backend:${{ github.run_id }}-${{ github.run_attempt }}` in build-push tags AND the `docker run` pytest step, plus an `if: always()` `docker rmi` cleanup step — the fixed tag races between concurrent backend jobs on the shared daemon and executes the WRONG repo's tests | 2026-07-17 | done | 53e862c | Proven cross-repo execution: jg-ferien PR #71 (run 28984827793, 00:16:39Z) collected hram's `runs/tests/test_golden_regression.py`, hram CI started 34s later; hram develop CI red 07-15/16 was MIXED: the 08:40 run executed survey_app's `surveys/test_ws_inventory.py` (image race), while runs after hram's 07:13 engine-v1.12.0 bump (`4c0e6ea0`) also fail on hram's OWN `calculate/` tests — a real regression tracked separately in hram; post-fix hram runs stay red on OWN tests until that lands. app-ci is consumed `@main` → fix is fleet-live on push. Companion issue (app repos, separate WO): jg-ferien test_settings lacks CHANNEL_LAYERS/CACHES overrides (dcm 2.25 defaults → `redis://redis` unresolvable under `--network host`). Residual assumption (implementer+reviewer+orchestrator concur): tag uniqueness relies on NO matrix on the backend job — verified for all 13 current callers; a future matrix must add `strategy.job-index` to `CI_IMAGE`. Orchestrator post-landing review 2026-07-17: clean, no findings |
| CI-2 | Maintenance action: no false failure on reboot / apt sshd restart | The `maintenance` composite action (`.github/actions/maintenance/action.yml`) red-flags a run (exit 255, "connection closed by remote host") when a target box legitimately reboots or when `apt-get upgrade` restarts sshd mid-session — the bare `ssh` calls in the apt step (`if apply_updates`) and the decide step (trailing `ssh "uname -a; uptime"`) run under `set -euo pipefail` with no reconnect tolerance. Make the run finish GREEN when the box returns healthy with expected containers present; red only on genuine problems (box never returns within the wait timeout, expected container tokens missing, real apt error). | 2026-07-19 | in-progress | 07897d5 | Root cause of the recurring webapp-management "production deploy failed" mail (cockpit mislabels any failed main workflow). Evidence: webapp-management run 29676082177 (2026-07-19 06:13 UTC), all 3 prod targets X on the maintenance step, `Permanently added to known hosts` → `Connection closed by remote host` → exit 255 (early ssh). Code fix landed on `main` (`07897d5`): apt upgrade decoupled from the SSH session (detached `setsid nohup` + status-dir marker file, polled by a new reconnect-tolerant "Wait for system updates to finish" step); decide-step's trailing informational `uname -a; uptime` hardened with `\|\| true`. Independent review: found + fixed a P1 (apt-run exit code was captured from the trailing group command, not the actual failing command — masked real apt failures as green; fixed via an inner `( set -e; … )` subshell with its own captured `$?`), re-reviewed and confirmed fixed. Remaining before `done`: tag a new version, bump webapp-management `maintenance.yml` pin (mirror TS-1/CI-1 flow), operator-present staging dispatch verification (no reboot, `force_reboot=true`, non-return-within-timeout stays red), then prod only after green staging + operator OK. Handed to Fable (infra). |
