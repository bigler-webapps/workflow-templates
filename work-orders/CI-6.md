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

### Execution directive

> **If you are the implementer reading this work order as your own specification: this section is
> NOT addressed to you.** It tells the Orchestrator how to invoke you. **You ARE that invocation —
> do NOT shell out to `codex exec`.**
>
> Implement through `codex exec` in the background — invoked directly via Bash (never the
> `debugger`/`*_coder` Agent wrappers) with BOTH flags `--skip-git-repo-check` and
> `--dangerously-bypass-approvals-and-sandbox`, prompt passed as a positional argument from a file.
> (Fallback to direct Claude implementation only on Codex quota/rate-limit/non-zero exit.)

### Target repo

`C:\Users\biglmi\Documents\webapps\workflow-templates` (git root — run `codex exec` with this as
cwd; the file to change is `scripts/kuma_sync.py`, tests are `scripts/tests/test_kuma_sync.py`,
run with plain `pytest scripts/tests/test_kuma_sync.py`).

### Context package

**Item 1 — `_resolve_notification_ids`** (`scripts/kuma_sync.py:380-407`):

```python
def _resolve_notification_ids(spec, notifications_by_name, default_ids, monitor_name=""):
    relay_ids = []
    if "notification_name" in spec:
        notif_name = spec.pop("notification_name")
        notif_id = notifications_by_name.get(notif_name)
        if notif_id is not None:
            relay_ids.append(notif_id)
        else:
            print(f"⚠️  notification '{notif_name}' not found in Kuma "
                  f"(monitor: {monitor_name}) — skipping relay assignment", file=sys.stderr)
    combined = sorted(set(default_ids) | set(relay_ids))
    if combined:
        spec["notification_ids"] = combined
    return spec
```

`notification_name` is popped as a single string today. `project.yaml`'s auto-derivation
(`monitors_from_project_yaml`, line ~525) sets it via `prod_relay = {"notification_name":
f"claude-relay-{prod_server}"}`, and `monitoring/notifications.yml`/monitor.yml specs may also set
it directly — both call sites must keep working with a bare string. Extend to also accept a list
(e.g. `notification_names: [...]` or the same key holding a list — your call, but state the chosen
shape in the commit) and resolve every entry, unioning with `default_ids` exactly as today. One
unresolvable name in a list must warn (same message shape) and skip only that entry — not abort the
whole assignment. Preserve the CI-5 property this docstring already states: an unresolvable name
alone (all defaults empty + the name doesn't resolve) still must not silently succeed with an empty
`notification_ids` when at least one part of the union is non-empty — i.e. only set the key when
`combined` is non-empty, same as today.

**Item 2 — `_expand_env`** (`scripts/kuma_sync.py:64-72`):

```python
def _expand_env(value):
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value
```

Called from `sync_notifications` (line ~422, `spec = _expand_env(raw_spec)`) on the whole
notification spec dict, including its nested `config` dict. Add coercion for a known-typed field
list — at minimum `smtpPort` (int) and `smtpSecure` (bool) since those are the two that already hit
this bug (see `monitoring/notifications.yml` `alert-email` config in `webapp-management`, which
currently declares them as literals to route around it). Coerce only fields on the explicit list,
after string expansion, and only within the notification `config` dict (or wherever the typed
fields live per your read of the call sites) — do not coerce arbitrary strings that merely look
numeric. `"true"`/`"false"` (case-insensitive) → bool; a numeric string → int. Leave every other
field untouched.

### Invariants / do-not-touch

- `_load_default_notification_names` (defaults from the file, not live Kuma state) — untouched.
- Prune safety, retry/reconnect (`Session`, `_retry_call`), run `Budget` — untouched.
- The singular `notification_name` string form must resolve byte-identical to today for every
  existing caller (`monitors_from_project_yaml`'s `prod_relay` in particular).

### Tests

Existing suite: `scripts/tests/test_kuma_sync.py` (47 tests, run via plain `pytest
scripts/tests/test_kuma_sync.py` — no special fixtures beyond its own `conftest.py`). Add new
tests per the WO's "Tests to WRITE" section near the existing
`test_resolve_notification_ids_*` tests (line ~479) and add `_expand_env` coercion tests near
there too. Run only this file — it is the affected-areas set for a script with no other consumers
in this repo.

### Progress contract

Emit `PLAN: <one line>` before starting, then `PROGRESS: [n/total] <action>` before each of the two
items and `... done` on completion, then a final `RESULT: DONE` or `RESULT: BLOCKED <reason>`. No
gap over ~2 minutes without an update.

### Invocation command (for the Orchestrator)

```
cd "C:\Users\biglmi\Documents\webapps\workflow-templates" && codex exec --skip-git-repo-check \
  --dangerously-bypass-approvals-and-sandbox "$(cat work-orders/CI-6.md)" > .tmp_codex_logs/ci-6.log 2>&1
```

### Preamble (appended verbatim per orchestrate-codex)

> The text above is the COMPLETE spec — the committed WO file's content, not a plan to refine; there
> is no separate plan file. Read the nearest `AGENTS.md`, the relevant `.codex/skills/<role>/SKILL.md`,
> and the app `MEMORY.md` ONLY for conventions. Stay in scope; do not touch auth/permissions/deps/
> schema/CI unless the spec says so; do not update `MEMORY.md`. Do NOT `git add`/`commit`/`push` —
> leave every change uncommitted in the working tree for the orchestrator's independent review.
> WRITE the tests the "Tests" section calls for AND **RUN the tests you just wrote** to confirm they
> execute and pass — that is the ONLY test run you do (NOT the app's affected/full suite, NOT any
> review). The orchestrator re-runs the authoritative set + does the independent review after you
> finish — those are the gate; your own run does not count as the gate.
>
> Narrate continuously: a `PLAN: <step1> | <step2> | …` line up front, then a single-line
> `PROGRESS: [<n>/<total>] <present-tense action>` before every relevant action (and `… done` on
> completion), spaced so no gap exceeds ~2 min, stdout unbuffered, plus exactly one final
> `RESULT: DONE|BLOCKED <reason>`.

Mini-handover: repo `workflow-templates`, WO `work-orders/CI-6.md`, follow `orchestrate-codex`.
