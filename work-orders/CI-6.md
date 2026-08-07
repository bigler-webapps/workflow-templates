# CI-6 — kuma_sync input handling: multiple notifications per monitor, and typed config values

Target repo: `workflow-templates` (branch `main`) — `scripts/kuma_sync.py`
Tier: **2** (the script that routes every alert in the estate)

Enabler for `webapp-management` **INF-9**, which cannot be implemented without item 1.

---

## A. Envelope (authoritative WHAT/WHY)

Two defects in how `kuma_sync.py` accepts declared input. Both surfaced on 2026-08-07 while wiring
the estate's second alert channel; both block work that is otherwise ready.

### Item 1 — a monitor can carry only ONE named notification

`_resolve_notification_ids` pops a single `notification_name` string and unions it with the declared
defaults:

```python
if "notification_name" in spec:
    notif_name = spec.pop("notification_name")
    ...
combined = sorted(set(default_ids) | set(relay_ids))
```

So a monitor gets *every* default plus *at most one* explicitly named channel.

That is fine while the human channel is a default. It stops being fine the moment a channel must
reach **some** monitors but not all — which is exactly INF-9: the operator wants routine alerts only
through cockpit, with Kuma's email kept as a last resort on the monitors that watch the alerting
chain itself (`cockpit-frontend`, `cockpit-healthz`, `monitoring-health-*`). Those first two already
carry `claude-relay-main-prod` in the single available field, so `alert-email` cannot be added
alongside it.

**Scope:** accept a list as well as a string for `notification_name` (or an equivalent plural field),
resolve each entry, and union all of them with the defaults. The singular form must keep working
unchanged — `prod_relay` produces it for every auto-derived production monitor.

### Item 2 — every value routed through `${...}` arrives as a string

`_expand_env` uses `os.path.expandvars`, which returns strings, and the composite action feeds those
placeholders from GitHub Actions inputs, which are **always** strings. Kuma's client then validates
typed fields and blows up:

```
TypeError: '<' not supported between instances of 'str' and 'int'
```

That killed the 2026-08-07 notification dispatch on `smtpPort`. It was worked around by declaring
`smtpPort: 2465` and `smtpSecure: true` as literals in `notifications.yml` — correct for those two,
but the next numeric or boolean field walks into the same trap, and the failure surfaces only at
dispatch time against a live Kuma.

**Scope:** coerce known numeric and boolean notification-config fields after env expansion, so a
declared `"${SMTP_PORT}"` becomes an int and a declared `"true"` becomes a bool. Unknown fields pass
through untouched.

### Non-goals / do not touch

- Do not change the defaults mechanism from CI-5 (`_load_default_notification_names` resolving from
  `notifications.yml`, not from Kuma's live state). It is the reason the estate's channels are
  deterministic; item 1 extends it, it does not replace it.
- Do not weaken the CI-5 guarantee that an unresolvable explicit name never yields an EMPTY
  assignment on its own.
- Do not touch the prune safety, the retry/reconnect behaviour, or the run budget from CI-4.
- Do not coerce arbitrary strings — a field not on the known-typed list stays a string.

### Risks

1. **Silently dropping a name.** With a list, one unresolvable entry must warn and be skipped while
   the others still apply — and must never reduce the monitor to an empty assignment. CI-5 already
   holds that property for the singular case; a list must not lose it.
2. **Over-eager coercion.** Casting a field Kuma expects as a string (a hostname that happens to be
   numeric, a token of digits) would break it in a way that only shows at dispatch. Drive the
   coercion from an explicit field list, not from "looks like a number".
3. **This routes every alert in the estate.** A regression here does not announce itself — monitors
   simply stop matching the declared config, and the first symptom is a missing alert. That is the
   argument for tests over inspection.

### Tests to WRITE (narrow — run only these)

- `notification_name` as a string resolves exactly as today; as a list, all entries resolve and union
  with the defaults; a mixed list with one unknown name warns, skips that one, keeps the rest.
- A monitor with a list and no defaults still gets a non-empty assignment.
- `smtpPort: "587"` (string) is coerced to int; `smtpSecure: "true"` to bool; an unlisted string field
  is untouched.
- The CI-5 property survives: an unresolvable name alone never produces an empty `notification_ids`.

### Verification

A dispatch that attaches **two** named notifications to one monitor, confirmed in Kuma — the thing
that is impossible today. And a config that routes `smtpPort` through `${...}` again syncing without
the TypeError, which is what proves item 2 rather than the literal workaround.

---

## B. Implementation map

*(To be filled by the Orchestrator on `git pull`. Not authored by the Expertenchat.)*
