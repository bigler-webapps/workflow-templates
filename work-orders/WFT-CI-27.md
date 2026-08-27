# WFT-CI-27 — `tailnet-connect`'s re-probe after a failed join ignores routability too

## Part A — Envelope

### Goal

`ts_probe_2` ("Backoff and re-probe after failed join") requires the same routable-tailnet-IPv4
condition `WFT-CI-26` added to the initial probe, so it cannot wrongly report `connected=true` on
the identical defect and skip Join attempt 2.

### The gap

`WFT-CI-26` fixed `tailnet-connect`'s initial probe (`id: ts_probe`) so it no longer treats
`BackendState=Running` + the right tag as sufficient to reuse a warm daemon — it also requires the
daemon's own reported tailnet IPv4 to actually be bound to a local interface, because a daemon can
report `Running` and correctly tagged while the kernel still has no route back (measured on
`netcup-runner-1`, 2026-08-27; full diagnosis in `work-orders/WFT-CI-26.md`).

`ts_probe_2` (action.yml, "Backoff and re-probe after failed join", currently lines 94-121) runs the
identical pre-fix two-condition check — `state = Running && tagged = true`, no routability — and was
deliberately left untouched by `WFT-CI-26` (that WO's own Non-goal: "No change to the join or retry
branches"; Part B named only `ts_probe`, lines 39-61 at the time).

This step is reachable, not dead code: it runs whenever Join Tailnet (attempt 1) fails
(`steps.ts_join_1.outcome == 'failure'`) — a documented, not-rare occurrence per the action's own
header comment (`tailscale up` on a warm daemon colliding with existing non-default prefs). If
attempt 1 fails without visibly changing `BackendState`/`Tags` (plausible — a rejected `up` need not
tear down the existing session), `ts_probe_2` can wrongly report `connected=true` on the exact
non-routable defect `WFT-CI-26` measured, skip Join attempt 2, and let the job proceed against an
unroutable daemon — reproducing the bug in a path the fix didn't reach.

Found by `WFT-CI-26`'s independent reviewer (`review_fallback`, Claude/sonnet), finding R2, and
recorded as this WO's own row at `WFT-CI-26`'s finalize.

### Scope

Apply the identical third condition `WFT-CI-26` added to `ts_probe` (lines 51-73 today) to
`ts_probe_2`: extract the daemon's own reported tailnet IPv4 from `.Self.TailscaleIPs`, check it is
bound to a local interface via `ip -4 addr show`, and require that alongside `state = Running` and
`tagged = true` before setting `connected=true`. Use the **same anchored match** `WFT-CI-26` landed
after its own reviewer caught an unanchored-substring false positive (`grep -qE "inet
${ip4//./[.]}/"`, not `grep -qF "$ip4"`) — do not reintroduce the bug that fix already closed.

### Non-goals

- No change to `ts_probe` itself (already fixed, `WFT-CI-26`).
- No change to the join steps (`Join Tailnet (attempt 1)` / `Join Tailnet (attempt 2)`) or the retry
  timing (`sleep 90`).
- No change to `group_vars/runners.yml` or any ACL — same non-goal as `WFT-CI-26`, this order only
  touches the probe's decision logic.

### Tier

**3** — same composite action, same fleet-wide blast radius as `WFT-CI-26`.

### Risks

- Same as `WFT-CI-26`: an over-strict condition would reject a genuinely routable daemon during the
  retry path too, forcing Join attempt 2 even when attempt 1's failure was cosmetic (the scenario the
  90s backoff + re-probe exists to avoid — regression `40c70c4`, named in the action's own comment).
  The condition must stay narrow: reject only on "no tailnet IPv4 on any local interface."
- Live evidence for this exact re-probe branch is not separately obtainable beyond what `WFT-CI-26`
  already gathered — its two live dispatches both took the initial-probe join path (attempt 1
  succeeded both times after internal retries), never reaching `ts_probe_2` at all. This order's
  correctness rests on the same class of mutation-tested unit test `WFT-CI-26` used for its own
  probe, not a fresh live reproduction of the re-probe path specifically. Name this gap in the
  register the same way `WFT-CI-26` did; do not overstate the evidence.

### Tests

Structural/behavioural, same approach as `WFT-CI-26`'s `test_tailnet_connect_probe.py` (real script
text extracted from `action.yml` and executed under bash with `tailscale`/`jq`/`ip` stubbed — not a
reimplementation of the logic under test):

- `ts_probe_2` rejects a `Running` + tagged daemon that is not routable (mirrors `WFT-CI-26`'s
  `test_running_tagged_but_not_routable_does_not_reuse`).
- `ts_probe_2` still accepts a `Running` + tagged + routable daemon.
- Mutation check: the pre-fix two-condition version of `ts_probe_2` would wrongly report
  `connected=true` on the non-routable fixture — proves the test suite actually catches the bug.
- The anchored-match regression test from `WFT-CI-26` (`test_prefix_address_does_not_false_positive…`)
  applied to `ts_probe_2` too, so this order doesn't reintroduce that already-fixed defect.

---

## Part B — Implementation map

### Files

- `.github/actions/tailnet-connect/action.yml` — the `ts_probe_2` step, currently lines 94-121 (the
  `run:` block starting after `sleep 90`).
- `.github/scripts/test_tailnet_connect_probe.py` — extend with `ts_probe_2` coverage; reuse the
  extraction/stub helpers already there (`_extract_probe_script`-equivalent for `ts_probe_2`, the
  same `JQ_STUB`/`IP_STUB_TEMPLATE`/`TAILSCALE_STUB`), don't duplicate them.

### Reference

- `work-orders/WFT-CI-26.md` — the fixed `ts_probe` step is the exact pattern to mirror.
- `.github/actions/tailnet-connect/action.yml` lines 51-73 — the landed, reviewed fix text (both the
  routability check and the anchored-match correction) to reproduce in `ts_probe_2` verbatim in
  shape, adjusted only for `ts_probe_2`'s own variable names/context.

### Progress contract

`PLAN: …` · `PROGRESS: [n/total] <action>` · one final `RESULT: DONE|BLOCKED <reason>`.

---

## Part C — Orchestrator only

*Stop line.*

### Review

Tier 3, independent. Same reviewer question as `WFT-CI-26`: can the new condition reject a runner
that is in fact routable? Also: does this correctly mirror `WFT-CI-26`'s anchored-match fix, or does
it reintroduce the unanchored `grep -F` substring bug that WO's own reviewer caught?

### Register

`WFT-CI-27`. Notiz records the review verdict and that live dispatch evidence for this specific
branch is inherited from `WFT-CI-26`'s gap, not independently gathered.

### Commit

`main`, once the independent review is clean and tests pass — same trunk-only flow as `WFT-CI-25`/
`WFT-CI-26` (this repo has no `develop`).
