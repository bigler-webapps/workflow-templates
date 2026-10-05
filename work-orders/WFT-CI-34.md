# WFT-CI-34 — pip-audit blocks by default

`app-ci.yml` runs pip-audit in the `security` job, but report-only by default (`pip-audit-blocking`,
default `false`, introduced by WFT-CI-23): a finding becomes a `::warning` and the job stays green. Only
Gustav sets `pip-audit-blocking: true`. In 2026-10 that is why Django 6.1's CVE-2026-15830 and
oauthlib's PYSEC-2026-4114 surfaced in Gustav only, while the same versions sat unnoticed in up to 13
other apps (`webapp-management/WM-INF-77`). A report nobody reads is not a control.

---

# A. Envelope

## Goal & expected outcome

- **Ziel:** a known vulnerability in an app's backend dependencies fails that app's CI.
- **Expected outcome:**
  - `pip-audit-blocking` defaults to `true` in `app-ci.yml`; an app can still set `false`, and that
    opt-out is visible in its `ci.yml` (a deliberate, reviewable exception, not a silent default).
  - **The flip happens only on a clean estate:** before it lands, the latest pip-audit report of every
    app calling `app-ci.yml` is read; the flip lands when none reports a finding — or, for an app that
    still does, after the operator has decided per app (fix first, or a temporary `false` with a register
    row naming the advisory). No app turns red by surprise.
  - The input's description states the new default and how to opt out.
  - Proof on `ci-test` refs on both sides (this repo's candidate, one app pointing at it): a run with a
    known-vulnerable pin fails the security job, a clean run passes.

## Scope

- `.github/workflows/app-ci.yml` (the default and the description), its structural tests.
- `CHANGELOG.md` entry.

## Explicit non-goals / do-not-touch

- No ignore list mechanism in this order.
- No change to bandit/ruff gating.
- No app repo changes, except the temporary `pip-audit-blocking: false` the operator may decide for an
  app with an open finding.

## Tier · precondition / gate

- **Tier 3** — CI, the shared workflow every app calls from `@main`, so the flip reaches every app at once.
- **Precondition:** `webapp-management/WM-INF-77` has landed (Django 6.1.1 and oauthlib 4 everywhere),
  so the measurement before the flip can come out clean.

## Risks

- **A new advisory published between the measurement and the flip** turns an app red on its next PR.
  That is the intended behaviour from then on; the risk is only the timing — re-read the reports right
  before landing.
- **A blocking audit stops a promotion PR** on an advisory with no fix yet. Then the opt-out with a
  register row is the path, decided by the operator, never an ignore flag added in passing.
- **pip-audit availability on the runner** (WM-TAKE-8: it silently found no Python once). The
  structural tests must keep proving the audit actually runs.

## Required tests to WRITE

- Structural test: the input's default is `true` (shown to fail on a copy with `false`).
- The CI proof above (vulnerable pin fails, clean passes).

---

# B. Implementation map — filled by the Orchestrator

*(placeholder — to be filled by the Orchestrator on `git pull`)*

---

# C. Orchestrator only — NOT ADDRESSED TO THE IMPLEMENTER

*(to be filled by the Orchestrator)*
