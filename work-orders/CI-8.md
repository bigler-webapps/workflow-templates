# CI-8 — kuma_sync must clear undeclared notification IDs, not only add declared ones

Target repo: `workflow-templates` (branch `main`) — `scripts/kuma_sync.py`
Tier: **2** (the script that routes every alert in the estate)

Companion to `webapp-management` **INF-9**, discovered mid-implementation of that WO.

---

## A. Envelope

### Goal

`_resolve_notification_ids` only ever ADDS notifications it can resolve to a monitor's
`notification_ids`; it never sets the key to an empty list. `_build_monitor_kwargs` then omits
`notificationIDList` from the edit call entirely when the key is absent from `spec`, and
`_monitor_changed` only compares keys present in `kwargs` — so a monitor's EXISTING Kuma-side
notification assignment survives untouched whenever the declared config resolves to nothing for
that monitor.

### Why this blocks INF-9

INF-9 turns `alert-email` from `default: true` (attached to all 69 monitors) to explicit-only. Once
that lands, the ~65 monitors that only ever had `alert-email` via the default resolve to
`notification_ids: []` in `_resolve_notification_ids` today's semantics — but because an empty
result never sets the key, `notificationIDList` is never sent to Kuma for those monitors, so the
stale `alert-email` attachment from before the config change is never removed. INF-9's stated goal
("routine alerts through cockpit only, Kuma email only for the alerting chain") would not actually
be achieved by the config change alone — confirmed by tracing `_resolve_notification_ids` →
`_build_monitor_kwargs` → `_monitor_changed` end-to-end against the post-INF-9 config, live, during
INF-9's implementation (2026-08-07).

### Scope

The declared config becomes the EXACT source of truth for a monitor's notifications, including an
explicit "none" — `notification_ids` is set even when it resolves to an empty list, UNLESS the
specific case CI-5 already protects against: explicit `notification_name` entries were declared but
NONE resolved, and there are no defaults either. That combination still reads as "probably broken"
(typo, or `notifications.yml` itself missing/misconfigured) rather than "deliberately empty", and
must still leave the existing Kuma-side assignment untouched — CI-5's original guarantee, narrowed
to the one case it actually protects rather than applied to "nothing declared at all".

### Non-goals / do not touch

- Do not touch CI-4's prune safety, retry/reconnect, or run budget.
- Do not touch CI-6's list-form resolution or typed-config coercion — this WO only changes when the
  resulting `notification_ids` key is SET, not how it's computed.
- Do not change `_load_default_notification_names` or the defaults mechanism itself.

### Risks

1. **This is a deliberate behaviour change to an established CI-5 guarantee.** The existing test
   `test_resolve_notification_ids_no_defaults_no_relay_sets_nothing` encoded the OLD semantics
   ("nothing declared → don't touch Kuma") and must be rewritten, not just left passing by accident —
   the new semantics are the opposite for that exact case (nothing declared → clear explicitly).
2. **Distinguish "nothing intended" from "something intended but broken."** The fix must not collapse
   these — the protected case (explicit name given, unresolvable, no defaults) must still leave
   `notification_ids` unset; only the "genuinely nothing declared" case (no `notification_name` key
   at all) newly sets it to `[]`.
3. **Blast radius:** every monitor synced via the `multi` path (the one CI actually uses) that
   currently carries a notification only via a default that is being turned off. The next scheduled
   `kuma-sync` run after this AND after `notifications.yml`'s default is turned off will strip that
   assignment estate-wide, in one pass — intended, but worth stating plainly rather than discovering
   it live.

### Tests to WRITE (narrow — run only these)

- No `notification_name`, no defaults: `notification_ids` is now `[]` (was: key absent).
- Explicit `notification_name` unresolvable, no defaults: `notification_ids` still NOT set at all
  (CI-5 protection preserved) — with a warning.
- No `notification_name`, defaults present: `notification_ids` = defaults (unchanged from before).
- Explicit name resolves fine: unchanged from CI-6 behaviour.
- Explicit name unresolvable but defaults present: unchanged (defaults still apply, name dropped).

### Verification

Live: after `notifications.yml`'s `alert-email` default is turned off (INF-9), the next
`kuma_sync.py multi` run against a monitor that only had it via the default clears
`notificationIDList` for that monitor in Kuma. Operator-gated (workflow-dispatch), same posture as
CI-4/CI-5/CI-6.

---

## B. Implementation map

Implemented directly (no Codex round — see Notiz). Change is entirely in
`scripts/kuma_sync.py::_resolve_notification_ids` (added `had_explicit_names` tracking and a
`protect_unresolvable` guard replacing the old `if combined:` check) plus
`scripts/tests/test_kuma_sync.py` (rewrote `test_resolve_notification_ids_no_defaults_no_relay_sets_nothing`
→ `..._sets_empty_list`, added `test_resolve_notification_ids_unresolvable_relay_and_no_defaults_protects_existing`).

Tests: `pytest scripts/tests/test_kuma_sync.py` (58 green — 57 + 1 net after the rewrite).
