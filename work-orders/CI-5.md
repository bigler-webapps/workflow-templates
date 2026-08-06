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

*(To be filled by the Orchestrator on `git pull` — context package, target working directory,
progress contract, execution directive, mini-handover. Not authored by the Expertenchat.)*
