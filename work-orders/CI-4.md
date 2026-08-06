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

### Execution directive

> **If you are the implementer reading this work order as your own specification: this section is
> NOT addressed to you.** It tells the Orchestrator how to invoke you. **You ARE that invocation —
> do NOT shell out to `codex exec`.**
>
> Implement through `codex exec` in the background — invoked directly via Bash (never the
> `debugger`/`*_coder` Agent wrappers) with BOTH flags `--skip-git-repo-check` and
> `--dangerously-bypass-approvals-and-sandbox`, prompt passed as a positional argument from this
> file. Fallback to direct Claude implementation only on Codex quota / rate-limit / non-zero exit.

### Target repo working directory (absolute)

`C:\Users\biglmi\Documents\webapps\workflow-templates`

### The Envelope's prime suspect is ruled out for the failing path — read this first

Part A names `notificationIDList` as the prime suspect, flagged "reasoned from the code, **not
proven**". Reading the landed code rules it out **for the path that actually failed**, and Part A's
scope item 0 explicitly anticipates this ("if the differing key turns out to be something else, fix
that instead and say so"). This is that correction; it is not a scope change.

`kuma-sync` runs the **`multi`** subcommand (one session, all ~57 monitors — that is the run in the
evidence). The two paths differ:

| | `sync_monitors` (single app) | `sync_monitors_multi` (what runs in CI) |
|---|---|---|
| resolves `notification_name` → `notification_ids` | **yes**, `kuma_sync.py:405-416` | **no** |
| so `notificationIDList` reaches `kwargs` | yes | **never** |

`_build_monitor_kwargs:202-203` only maps `notification_ids` → `notificationIDList`; a spec carrying
`notification_name` is silently dropped. `sync_monitors_multi:480-484` goes straight from
`_expand_env` to `_build_monitor_kwargs` with no resolution step. And `_monitor_changed:145` iterates
**`kwargs.items()`** only — a key that is never sent can never be compared, so it cannot be the
permanent diff in these runs.

**Therefore: do not start by normalising `notificationIDList`.** Scope item 0 is now load-bearing,
not a formality — instrument first, then fix what the log names.

**Ranked candidates for what the log will actually show.** Every monitor in the failing run sends
exactly these keys (`_build_monitor_kwargs:173-185`, from the `base` dict at `kuma_sync.py:296-302`
which sets only type/interval/max_retries/retry_interval): `name`, `type`, `url`, `interval`,
`maxretries`, `retryInterval`. The optional keys (`accepted_statuscodes`, `hostname`, `port`,
`keyword`, `method`, `body`, `headers`) appear only when declared in `monitoring.defaults`/`extra`,
so they cannot explain a diff on *every* monitor.

1. **`type`** — the strongest remaining candidate. We send the string `"http"`. If
   `api.get_monitors()` echoes it as a `MonitorType` enum member, `_monitor_changed`'s else-branch
   computes `str(current)`, which for an `(str, Enum)` mixin yields `"MonitorType.HTTP"`, not
   `"http"` → mismatch on every monitor, every run. That is exactly the observed signature (0
   `unchanged`, ever, in both runs). The comment at `kuma_sync.py:160` claims `str()` "normalises
   enum types" — if this is the cause, that comment is precisely the wrong assumption. **Unverified**:
   `uptime-kuma-api-v2` is not installed on the maintainer machine and may well return a plain
   string, in which case this is wrong too. Prove it, do not assume it.
2. **`url`** — server-side normalisation (a trailing slash) would also hit every monitor.
3. `interval` / `maxretries` / `retryInterval` — unlikely; these go through the int-coercion branch
   (`_monitor_changed:147-153`), which is robust.

If the log shows something else again, fix that and record it — the instrumentation is the point.

### The `_retry_call` trap (scope item 2)

Part A says to mirror `_login`'s `api.disconnect()`. **That alone does not work**, and the reason is
structural:

```python
# kuma_sync.py:122-140
def _retry_call(label, fn, *args, **kwargs):
    for attempt in range(1, 6):
        try:
            return fn(*args, **kwargs)
```

`fn` arrives as an **already-bound method of the old api object** — `api.edit_monitor`,
`api.get_monitors`, `api.add_monitor`, `api.delete_monitor` (call sites: `473`, `490`, `500`, `516`,
`547`, `551`, and `390`, `397`, `422`, `427`, `433` in the single-app path). Reconnecting inside
`_retry_call` produces a *new* api object, but `fn` still points at the dead one, so every retry
still fails — and the local rebind cannot propagate to the caller, which holds its own `api`.

So the seam is the **call convention**, not the retry body: `_retry_call` needs a way to re-resolve
the callable against a fresh connection (e.g. take the method *name* plus a holder/accessor for the
current api, or a `reconnect` callable it can invoke and then re-bind through). Pick one and apply
it consistently at every call site.

**Do not add a third retry layer.** There are already two, and they overlap:
- `_retry_call` (5 attempts, linear backoff, ~50 s) — the inner one;
- the `recovery_attempt` loop at `sync_monitors_multi:486-528`, which already detects
  `"not logged in"`/`"timeout"`, disconnects, re-logins, **and re-fetches `existing`** — the outer one.

Whatever you change, that outer loop's re-fetch of `existing` must stay correct (monitor ids come
from it). Preserve `_login`'s credential fast-path (`kuma_sync.py:108-109`) — Part A Risk 4.

### Scope item 3 — the budget

Bound the whole run in `sync_monitors_multi` (437-562). Pick the ceiling against the observed healthy
duration, not a round number (Part A Risk 3): the passing run in the evidence did 57 monitors and the
failing one burned 24 minutes. Note that a healthy run **after** the item-1 fix should be far faster
than either, since it will issue almost no writes — so do not calibrate against today's numbers
alone; say what you calibrated against. The budget must produce a clear message and a non-zero exit,
and it must NOT bypass the prune-skip safety at `533-540` (explicit non-goal).

### Testing — the import guard blocks `import kuma_sync`

`scripts/kuma_sync.py:48-60` does `from uptime_kuma_api import UptimeKumaApi` at module level and
**`sys.exit(1)`** on ImportError. `uptime_kuma_api` is NOT installed on the maintainer machine
(verified), so a test that simply imports the module kills the test process.

Default approach: inject a stub into `sys.modules["uptime_kuma_api"]` in a `conftest.py` **before**
importing `kuma_sync`, so no production code changes for testability. (Alternative: make the guard
lazy by moving the exit into `_login`. Cleaner production hygiene but a behaviour change to the
import path — if you take it, say so.)

There is no pytest infrastructure for `scripts/` yet. The repo's only precedent is
`ansible/roles/base/tests/` (added by INF-5). Put the new suite somewhere consistent, e.g.
`scripts/tests/`.

### Context package — named seams

- `_monitor_changed` `scripts/kuma_sync.py:143-168` — scope 0 (log the differing key **and both
  values**) and scope 1 (normalise).
- `_retry_call` `scripts/kuma_sync.py:122-140` — scope 2, per the trap above.
- `_login` `scripts/kuma_sync.py:74-119` — the reconnect pattern to mirror; keep the credential
  fast-path.
- `_build_monitor_kwargs` `scripts/kuma_sync.py:171-204` — what is actually sent.
- `sync_monitors_multi` `scripts/kuma_sync.py:437-562` — the CI path; budget goes here; prune safety
  at `533-540` is a non-goal.
- `sync_monitors` `scripts/kuma_sync.py:364-435` — the single-app path; the only place
  `notification_name` is resolved.
- `monitors_from_project_yaml` `scripts/kuma_sync.py:248-361` — where specs (and `notification_name`
  at `322`) come from.

Work from this package; do not explore broadly from scratch; open only the named files to verify.

### Out of scope — surfaced, not fixed here

While mapping: because `sync_monitors_multi` never resolves `notification_name`, monitors synced by
the CI path appear to get **no relay notification assigned at all**. If so, that is a separate and
arguably more serious defect than the one this WO fixes (a monitor that goes down with no
notification attached alerts nobody), and it is NOT in this Envelope's scope. Do not fix it here —
it needs its own WO and an operator decision. Flag it, do not touch it.

### Progress contract

Emit a `PLAN: <step1> | <step2> | …` line up front, then a single-line
`PROGRESS: [<n>/<total>] <present-tense action>` before every relevant action (and `… done` on
completion), spaced so no gap exceeds ~2 min, stdout unbuffered, plus exactly one final
`RESULT: DONE|BLOCKED <reason>`.

### Mini-handover

> Orchestrator: implement `work-orders/CI-4.md` in `workflow-templates` (`main`). Read Part B's
> "prime suspect is ruled out" and "`_retry_call` trap" sections before planning — the Envelope's
> stated suspect cannot be the cause on the CI path. `git pull`, follow `orchestrate-codex`.
