# CI-4 — kuma-sync rewrites every monitor on every run, and retries on a dead socket

Target repo: `workflow-templates` (branch `main`) — `scripts/kuma_sync.py`
Tier: **2** (the script that keeps the estate's entire monitor set declarative; a regression here
silently desynchronises monitoring from `project.yaml`)

---

## A. Envelope (authoritative WHAT/WHY)

### Goal

Make `kuma-sync` a no-op when nothing changed, and make it fail fast instead of slowly when Kuma is
unresponsive.

Right now it does the opposite of both: it rewrites **every** monitor on **every** run, and when the
connection wedges it spends ~50 seconds per monitor retrying over the same dead socket.

### Evidence (2026-08-06)

Two consecutive runs, counted from their logs:

| | run 31081103662 (07:29, failed) | run 31083824555 (08:10, passed) |
|---|---|---|
| `✏️ updated monitor` | 39 | 57 |
| **`✓ unchanged`** | **0** | **0** |
| `➕ created monitor` | 0 | 0 |
| retry storms (`attempt 1/5`) | 18 | 2 |

**Not a single `unchanged`, in either run.** `_monitor_changed()` reports every monitor as different
every time, so each run performs a full write pass of all ~57 monitors against Kuma's
single-threaded Socket.IO server — the very bottleneck `kuma-sync.yml`'s own header calls out
("concurrent deploys no longer race over the single-threaded Kuma Socket.IO server").

The failed run's first timeout hit at **07:30:37**, one minute after start, on the first monitor
(`cockpit-frontend`) — Kuma was already slow. It then burned 24 minutes and gave up on four apps
(`cockpit`, `hpc-bridge`, `innoservice`, `kerzenziehen`), which correctly skipped the prune:

> `⚠️ Prune skipped — 4 app(s) failed … Running prune with an incomplete declared-name set would delete valid monitors.`

Failure rate over the last 15 runs: 11 green, 4 red — intermittent, and denser since the monitor
count grew (INF-2 added 18, INF-3 removed 6, net +12 → 57).

### Three defects, in order of leverage

**1. The permanent diff — the amplifier.**
`_monitor_changed(kwargs, existing_monitor)` compares each key it *sends* against what Kuma echoes
back. Prime suspect: **`notificationIDList`**. `sync_monitors` resolves `notification_name` into
`spec["notification_ids"] = [notif_id]` (a list), `_build_monitor_kwargs` passes it through as
`notificationIDList`, and Kuma's API represents that field as an **object keyed by id**, not a list.
The comparison then hits `if not isinstance(current, list): return True` and short-circuits to
"changed" on the very first monitor with a notification — which is most of them.

Second candidate: `accepted_statuscodes` (`["200"]` sent; Kuma may normalise to a range form).

This is reasoned from the code, **not proven** — see scope item 0.

**2. The retry cannot succeed — the amplifier's amplifier.**
`_retry_call()` retries five times with linear backoff (5+10+15+20 = ~50 s) **on the same API
object, without reconnecting.** Compare `_login()`, which explicitly calls `api.disconnect()` before
each retry. Once the Socket.IO connection wedges, every remaining attempt for every remaining
monitor is guaranteed to fail — which is exactly the observed signature: attempt 1 `TimeoutError`,
attempts 2-4 `UptimeKumaException`, repeated across 18 monitors.

**3. No overall budget.**
Nothing bounds the run as a whole, so a wedged Kuma produces a 24-minute red run instead of a fast,
legible failure.

### Scope

0. **First: prove defect 1 rather than assume it.** Make `_monitor_changed` log *which key* differed
   and both values. Two lines, and the next run answers it definitively. Everything below is built
   on that answer — if the differing key turns out to be something else, fix that instead and say so.
1. **Normalise the comparison so an unchanged monitor reports unchanged.** Normalise both sides of
   the differing field(s) before comparing — for `notificationIDList`, compare the *set of ids*
   regardless of whether the value arrives as a list, a dict keyed by id, or a dict of id→bool.
   **Do not "fix" this by excluding the field from the comparison**: that would stop detecting real
   notification-assignment drift, which is a thing this sync exists to catch.
2. **`_retry_call` must re-establish the connection between attempts**, mirroring `_login`'s
   `api.disconnect()`. Retrying a wedged socket is time spent on a guaranteed failure.
3. **Add an overall budget** — a wall-clock or consecutive-failure ceiling for the whole run, so a
   dead Kuma fails in a minute or two with a clear message instead of after 24 minutes.
4. **Add tests.** `scripts/kuma_sync.py` currently has **none**. `_monitor_changed` is pure logic
   over two dicts and is the highest-value thing in this repo to pin down.

### Non-goals / do not touch

- **Do not remove or weaken the prune safety.** Skipping the prune when apps failed is correct
  behaviour and is the only thing standing between a partial sync and deleted monitors.
- Do not reduce the monitor set, change any app's `project.yaml`, or touch Kuma's own configuration
  or host.
- Do not paper over this at the workflow level (more retries, `continue-on-error`, a longer
  schedule). That converts a fixable defect into an accepted flake.
- Do not change the login/auth path beyond reusing its reconnect pattern.

### Risks

1. **Getting the comparison wrong in the other direction.** If normalisation is too eager, a real
   drift (a monitor whose notification assignment or accepted status codes were changed by hand in
   the Kuma UI) stops being corrected, silently. The test set must contain a genuinely-changed case
   for each normalised field, not only an unchanged one.
2. **This script is the single source of truth for monitoring.** A regression does not announce
   itself — monitors simply stop matching `project.yaml`, and the first symptom is a missing alert.
   That is the argument for item 4 rather than a quick patch.
3. **A fast-fail budget can turn a slow-but-working run red** where it previously limped to green.
   Pick the ceiling against the observed healthy-run duration, not a round number.
4. **Reconnect-per-retry could mask a credential problem** by re-logging in repeatedly. `_login`
   already fast-paths on credential errors; keep that property.

### Tests to WRITE (narrow — run only these)

New unit tests for `scripts/kuma_sync.py`:

- `_monitor_changed`: `notificationIDList` sent as `[3]` versus Kuma returning `{"3": true}` (and
  `[3]`, and `{"3": false}`) — equal cases report **unchanged**, a genuinely different id set
  reports **changed**.
- `accepted_statuscodes`: the equivalent unchanged/changed pair for whatever normalisation the
  scope-0 logging reveals.
- Existing behaviour preserved: int coercion, list comparison, the absent/None-vs-empty-string rule.
- `_retry_call`: reconnects between attempts; still fast-paths on "not found"/"permission"; still
  re-raises the last exception when exhausted.
- The overall budget aborts with a clear message and a non-zero exit.

### Verification (the acceptance criterion)

**Two consecutive real runs: the second must report `updated: 0` and all monitors `unchanged`.**
That is the whole point — anything less means the permanent diff is still there. Record the run
duration before and after; a healthy no-op run should take a small fraction of today's.

### Why this is blocking

INF-3's remaining step is an operator-gated `kuma-sync` dispatch to remove the six `*-health-swap`
monitors. **That dispatch is the run that failed at 07:29** — the prune was skipped, so the monitors
are still there, and INF-3 and INF-2 cannot reach `done`.

Interim option, not a fix: the workflow accepts an `app_filter`. A run limited to
`webapp-management` writes only its own monitors instead of all 57, and its prune blast radius is
exactly the one INF-3's Risk 2 asks to be checked anyway.

---

## B. Implementation map

*(To be filled by the Orchestrator on `git pull` — context package, target working directory,
progress contract, execution directive, mini-handover. Not authored by the Expertenchat.)*
