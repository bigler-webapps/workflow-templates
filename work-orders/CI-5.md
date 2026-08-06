# CI-5 — Monitors created by the sync have no notification channel at all

Target repo: `workflow-templates` (branch `main`) — `scripts/kuma_sync.py`
Companion declarations: `webapp-management/monitoring/notifications.yml`, `project.yaml`
Tier: **2** (this is the difference between an alert reaching a human and not)

---

## A. Envelope (authoritative WHAT/WHY)

### Goal

Make every monitor the sync creates actually notify someone.

Today none of them do. Everything built across INF-2, INF-3 and INF-6 — probes, responders,
thresholds, twelve green Kuma monitors — ends in silence.

### Evidence (2026-08-06, Kuma UI, monitor `main-prod-health-disk`)

The Notifications section of a monitor created by this sync, hours old:

| Notification | Badge | Toggle |
|---|---|---|
| `discord-alerts` | **Standard** (isDefault) | **off** |
| `claude-relay` | **Standard** | off |
| `claude-relay-staging` | — | off |
| `claude-relay-main-prod` | — | off |
| `claude-relay-contact-prod` | — | off |
| `claude-relay-innoservice-prod` | — | off |

**Every toggle is off, including the one flagged as the default.**

That settles a question that was assumed rather than tested: **Kuma does not apply a `default: true`
notification to a monitor created through the socket API.** `notifications.yml`'s own comment claims
it does ("auto-applied to every new monitor") — that is the UI's behaviour, not the API's. The
`uptime-kuma-api` client always sends `notificationIDList` on create, as `{}` when nothing is passed,
so every monitor is created with an explicitly empty assignment.

### Scope of the damage

Not just the twelve health monitors. **Every monitor created by the `multi` path since the estate
moved to the central `kuma-sync.yml`** was created the same way. Monitors that predate that move, or
that were edited rather than created, keep whatever they already had — `edit_monitor` reads then
merges (`data.update(kwargs)`), so omitted fields survive. The gap is specific to creation.

Two independent defects produce it:

**1. Defaults are never attached.** Nothing in the sync adds the `isDefault` notifications, and Kuma
does not do it for API-created monitors. Result: zero channels.

**2. `notification_name` is resolved on only one of the two code paths.** `sync_monitors` resolves it
at `kuma_sync.py:560-565`; the `multi` path does not. And `prod_relay`
(`notification_name: claude-relay-<server>`) is merged only into the auto-derived production
`frontend`/`healthz` specs (`:476-477`) — never into `monitoring.extra` entries. So even a monitor
that *declares* a relay does not get one on that path, and because the field is never sent,
`_monitor_changed` never compares it: a relay removed by hand in the UI is invisible to the sync.

This is the same defect the notification-drift chip describes. Fixing it once closes both, and closes
INF-6's scope item 4, whose stated fallback ("declare one explicitly") is currently unavailable.

### Scope

1. **Attach the default notifications on creation.** The sync must resolve which notifications are
   `isDefault` and include their ids in `notificationIDList` for every monitor it creates, because
   Kuma will not. This is the change that makes the alerting real.
2. **Resolve `notification_name` on the `multi` path too**, mirroring `:560-565`. One path resolving
   and the other silently dropping the field is the underlying asymmetry.
3. **Let `monitoring.extra` entries carry a `notification_name`.** `prod_relay` is merged only into
   auto-derived specs; explicit entries have no way to declare a relay today. Without this, the
   server-health monitors cannot be given a relay even deliberately.
4. **Repair the existing monitors, not only future ones.** A fix that only affects creation leaves
   every already-created monitor silent forever. The sync must bring existing monitors up to the
   declared state as well — which item 2 makes possible, since the field then participates in the
   comparison again.
5. **Reconcile the undeclared notification.** Kuma shows a `claude-relay` with no server suffix,
   marked default; `notifications.yml` declares only `discord-alerts` and four
   `claude-relay-<server>`. Decide whether it is a leftover to remove or something to declare — but
   do not leave an undeclared default sitting in the alerting path.

### Non-goals / do not touch

- Do not weaken the prune safety, the retry/reconnect behaviour or the run budget from CI-4.
- Do not attach relays by guessing. A monitor gets the relay its `project.yaml` declares, or the
  estate default — never an inferred one.
- Do not solve this by turning every notification into `default: true`. That would route every app's
  alert to every relay.
- Do not edit monitors by hand in the Kuma UI as the fix. The whole point is that the declared state
  wins.

### Risks

1. **Alert storm on the repair pass.** Item 4 touches every existing monitor. If a channel is
   attached to a monitor that is currently down, it fires immediately. Know which monitors are down
   before running it — right now `monitoring-health-disk` and `-mem` are at 0 % (see INF-7).
2. **Wrong routing is worse than no routing.** Attaching `claude-relay-main-prod` to a
   contact-prod monitor sends an incident to a relay that fetches logs from the wrong host. The
   per-server relays are not interchangeable.
3. **CI-4 interaction, expected not regression.** Once `notificationIDList` is actually sent, CI-4's
   id-set comparison starts seeing the field, and the first run after this reports drift for every
   affected monitor and rewrites it once. Anticipated; do not read it as a CI-4 regression.
4. **The default set can change under you.** Attaching "whatever is currently default" makes the
   monitor's channel depend on Kuma state rather than on the repo. Prefer resolving defaults from
   `notifications.yml` (which declares `default:`) over reading them back from the server, so the
   declared config stays the source of truth.

### Tests to WRITE (narrow — run only these)

Beside the tests CI-4 introduced:

- A created monitor's kwargs contain the default notification ids; with an explicit
  `notification_name`, they contain that id too.
- The `multi` path resolves `notification_name` identically to the single path.
- A `monitoring.extra` entry with `notification_name` reaches `notificationIDList`.
- An unknown `notification_name` warns and does not silently produce an empty assignment.
- An existing monitor whose channels differ from the declared set is detected as changed and
  corrected (item 4).

### Verification — the acceptance criterion

**Force a breach and confirm a notification actually arrives.** Not "the toggle is on" — an alert
that reaches a human. INF-3 proved the responder leg (`/health/disk` → 503 → restored); this WO
exists to prove the leg from there onward, which has never once been demonstrated in this estate.

Then, secondarily: open `main-prod-health-disk` and confirm `discord-alerts` is on and no unintended
relay is attached.

### Why this outranks the other open items

Without it, INF-2, INF-3 and INF-6 are decorative. The self-reach gap (INF-7) narrows alert coverage
to one host; this narrows it to none.

---

## B. Implementation map

### Execution directive (read this FIRST)

> **If you are the implementer reading this work order as your own specification: this section is
> NOT addressed to you.** It tells the Orchestrator how to invoke you. **You ARE that invocation —
> do NOT shell out to `codex exec`.**
>
> Implement through `codex exec` in the background — invoked directly via Bash (never the
> `debugger`/`*_coder` Agent wrappers) with BOTH flags `--skip-git-repo-check` and
> `--dangerously-bypass-approvals-and-sandbox`, prompt passed as a positional argument from a file.
> (Fallback to direct Claude implementation only on Codex quota / rate-limit / non-zero exit.)

### Context package

**Target file:** `scripts/kuma_sync.py` (only this file + its tests — no other repo needs a change,
see "Why no webapp-management change" below).

**Design decision already made (do not re-derive):** resolve which notifications are `isDefault`
from the **declared `notifications.yml` file**, not from Kuma's live `isDefault` field on
`get_notifications()` — Risk 4 in Part A explicitly requires this, so the declared config stays the
source of truth even if someone flips a toggle by hand in the Kuma UI.

**Why no webapp-management change is needed:** the only caller that matters in production is
`webapp-management/.github/workflows/kuma-sync.yml`, which invokes
`python _workflow-templates/scripts/kuma_sync.py multi $PRUNE_FLAG $PROJECT_YAMLS` with **cwd = the
webapp-management checkout root** (the first checkout step, unqualified). `monitoring/notifications.yml`
already exists at exactly that relative path in that checkout. So a `--notifications-config` CLI flag
defaulting to `monitoring/notifications.yml` (same convention as the existing `--project-yaml` default
of `project.yaml`) resolves correctly with **zero workflow-file changes**. Do not touch anything under
`webapp-management/` or `webapp-management-template/`.

**Named changes in `scripts/kuma_sync.py`:**

1. **New helper — resolve declared defaults from the file**, near `sync_notifications` (~line 360):
   ```python
   def _load_default_notification_names(config_path):
       """Names of notifications declared `default: true` in notifications.yml.

       Read from the FILE, not from Kuma's live isDefault — see CI-5 Risk 4: the
       declared config must stay the source of truth even if someone flips a
       toggle by hand in the Kuma UI.
       """
       path = Path(config_path)
       if not path.exists():
           print(f"⚠️  notifications config not found: {path} — no default "
                 "notifications will be attached", file=sys.stderr)
           return set()
       with open(path, "r", encoding="utf-8") as fh:
           data = yaml.safe_load(fh) or {}
       return {n["name"] for n in data.get("notifications", []) if n.get("default")}
   ```

2. **New helper — resolve one spec's notification ids**, replacing the ad hoc block at
   `sync_monitors:560-565` (delete that block, call this instead) and used identically inside
   `sync_monitors_multi`'s per-monitor loop (~line 643-647), which today does **not** resolve
   `notification_name` at all — that asymmetry is CI-5 scope item 2:
   ```python
   def _resolve_notification_ids(spec, notifications_by_name, default_ids, monitor_name=""):
       """Mutates spec in place: sets spec['notification_ids'] to the union of the
       declared defaults and (if present) the resolved notification_name — so every
       monitor gets at least the defaults, and an unresolvable explicit relay never
       silently produces an EMPTY assignment (CI-5 required test)."""
       relay_ids = []
       if "notification_name" in spec:
           notif_name = spec.pop("notification_name")
           notif_id = notifications_by_name.get(notif_name)
           if notif_id is not None:
               relay_ids.append(notif_id)
           else:
               print(f"⚠️  notification '{notif_name}' not found in Kuma "
                     f"(monitor: {monitor_name}) — skipping relay assignment",
                     file=sys.stderr)
       combined = sorted(set(default_ids) | set(relay_ids))
       if combined:
           spec["notification_ids"] = combined
       return spec
   ```
   This one helper closes scope items 1, 2 AND 3 together: item 3 ("`monitoring.extra` entries carry
   a `notification_name`") needs no separate code — `monitors_from_project_yaml`'s `merged = {**base,
   **entry}` (line 500) already lets an `extra` entry set `notification_name` in YAML; the only reason
   it never worked is that the `multi` path never resolved the key at all (item 2). Fixing item 2
   generically (same helper, same call site, no `if auto-derived` branch) fixes item 3 for free. Do
   not add a separate "extra can carry a relay" code path.

3. **Wire the helper into `sync_monitors`** (~line 543-583): compute `default_names =
   _load_default_notification_names(notifications_config_path)` once, then after the existing
   `notifications_by_name = {...}` fetch (line 550-553) compute
   `default_ids = sorted(notifications_by_name[n] for n in default_names if n in notifications_by_name)`
   — warn (`print(..., file=sys.stderr)`) for any declared default name absent from Kuma (means the
   notifications-sync workflow hasn't run yet). Replace the current `if "notification_name" in spec:
   ...` block (560-571) with `_resolve_notification_ids(spec, notifications_by_name, default_ids,
   name)`. Add a `notifications_config_path=None` parameter to `sync_monitors`, defaulting (inside the
   function, mirroring the existing `config_path`/`project_yaml_path` default pattern) to
   `"monitoring/notifications.yml"` when not passed.

4. **Wire the helper into `sync_monitors_multi`** (~line 592-746): it currently never calls
   `get_notifications()` at all. Add, once before the per-app `for path_str in project_yaml_paths:`
   loop (after `session = Session(_login())`): fetch `default_names` (pure file read, no Kuma call
   needed — can happen before login too) and `notifications_by_name = {n["name"]: n["id"] for n in
   session.call("get_notifications", "get_notifications")}`, then `default_ids` the same way as (3).
   Inside the per-monitor loop (~line 643-647, right after `spec = _expand_env(raw_spec)` /
   `name = spec["name"]`), call `_resolve_notification_ids(spec, notifications_by_name, default_ids,
   name)` **before** `kwargs = _build_monitor_kwargs(spec)`. Add a `notifications_config_path=None`
   parameter to `sync_monitors_multi`, same default as (3).

5. **Item 4 (repair existing monitors) needs NO separate code.** Both `add_monitor` and
   `edit_monitor` already go through the same `kwargs = _build_monitor_kwargs(spec)` built from the
   same resolved `spec["notification_ids"]`, and `_monitor_changed` already compares
   `notificationIDList` as an id-set (existing CI-4 logic, `_ID_SET_KEYS`/`_norm_id_set`). Once step
   3/4 make `notification_ids` populated for every spec (not just ones with an explicit
   `notification_name`), an existing monitor whose Kuma-side channels don't match the declared set is
   detected as changed and corrected automatically — same as any other drifted field. Do not add a
   dedicated "repair" pass.

6. **CLI wiring** in `main()` (~line 749-816): add `--notifications-config` to the `sm` (monitors)
   subparser (default `"monitoring/notifications.yml"`, same style as `--project-yaml`'s default) and
   to the `sm_multi` subparser (same default). Pass through to `sync_monitors(...,
   notifications_config_path=args.notifications_config)` and `sync_monitors_multi(...,
   notifications_config_path=args.notifications_config)`.

**Do NOT touch:** `_build_monitor_kwargs`'s existing `if "notification_ids" in spec:` line (356) —
it already does the right thing once `spec["notification_ids"]` is populated correctly upstream.
Do NOT change the prune / retry / budget logic (CI-4) at all — only the notification-resolution
seams named above.

**Invariant to preserve:** `_resolve_notification_ids` must `pop()` `notification_name` off `spec`
(not just read it) — `_build_monitor_kwargs` has no `notification_name` key in its allowlist, so an
un-popped key would silently just sit unused, but popping matches the existing convention at the old
560-565 site and keeps `spec` clean for logging/debugging.

### Required tests to WRITE

Add to `scripts/tests/test_kuma_sync.py` (new section, mirror existing style — plain functions,
`monkeypatch`/`tmp_path` fixtures already available in the file):

1. `_load_default_notification_names`: given a temp YAML file with two notifications, one
   `default: true` and one `default: false` (or omitted), returns a set containing only the
   `default: true` name. Given a missing path, returns `set()` and prints a warning to stderr.
2. `_resolve_notification_ids`: a spec with no `notification_name` and non-empty `default_ids` ends
   up with `spec["notification_ids"] == sorted(default_ids)`.
3. `_resolve_notification_ids`: a spec with `notification_name` resolving to an id present in
   `notifications_by_name` ends up with BOTH the default id(s) and the resolved relay id in
   `spec["notification_ids"]` (union, sorted) — mirrors WO's "created monitor's kwargs contain the
   default notification ids; with an explicit notification_name, they contain that id too."
4. `_resolve_notification_ids`: an unresolvable `notification_name` (not in `notifications_by_name`)
   still leaves `spec["notification_ids"]` containing the default ids (not empty, not missing) and
   prints the existing "not found ... skipping relay assignment" warning — WO's "unknown
   notification_name warns and does not silently produce an empty assignment."
5. A `sync_monitors_multi` integration test (extend the existing `FakeApi`/`WedgedApi`-style fakes
   already in the file, add a minimal fake with `get_notifications`/`get_monitors`/`add_monitor` that
   records kwargs) proving the `multi` path resolves `notification_name` identically to the single
   path — WO's "the multi path resolves notification_name identically to the single path." Use a
   temp `notifications.yml` (via `tmp_path`) with one `default: true` entry and check the kwargs
   passed to `add_monitor` include its id.
6. Reuse/extend an existing `existing_from(...)`-style fixture to prove an existing monitor whose
   `notificationIDList` differs from the newly-resolved declared set is detected as changed by
   `_monitor_changed` (this should already pass given CI-4's existing `_ID_SET_KEYS` logic — write it
   as a regression pin, not new logic) — WO's "existing monitor whose channels differ from the
   declared set is detected as changed and corrected."

Do not write a test that opens a real Kuma connection (conftest.py stubs `uptime_kuma_api` and
asserts on this).

### Target repo working directory (absolute)

`C:\Users\biglmi\Documents\webapps\workflow-templates`

### Preamble (append verbatim)

> The text above is the COMPLETE spec — the committed WO file's content, not a plan to refine; there
> is no separate plan file. Read the nearest `AGENTS.md`, the relevant `.codex/skills/<role>/SKILL.md`,
> and the app `MEMORY.md` ONLY for conventions. Stay in scope; do not touch auth/permissions/deps/schema/CI
> unless the spec says so; do not update `MEMORY.md`. Do NOT `git add`/`commit`/`push` — leave every
> change uncommitted in the working tree for the orchestrator's independent review. WRITE the tests
> the `Required tests` section calls for AND **RUN the tests you just wrote** to confirm they execute
> and pass — that is the ONLY test run you do (NOT the app's affected/full suite, NOT any review).
> The orchestrator re-runs the authoritative set + does the independent review after you finish —
> those are the gate; your own run does not count as the gate.
>
> Narrate continuously: a `PLAN: <step1> | <step2> | …` line up front, then a single-line
> `PROGRESS: [<n>/<total>] <present-tense action>` before every relevant action (and `… done` on
> completion), spaced so no gap exceeds ~2 min, stdout unbuffered, plus exactly one final
> `RESULT: DONE|BLOCKED <reason>`.

### Mini-handover

Repo: `C:\Users\biglmi\Documents\webapps\workflow-templates` (branch `main`). WO:
`work-orders/CI-5.md`. Follow `orchestrate-codex`. Only `scripts/kuma_sync.py` +
`scripts/tests/test_kuma_sync.py` change; run `pytest scripts/tests/` as the affected-areas gate.
