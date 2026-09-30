# WFT-CI-30 — Optional end-to-end job in `app-ci.yml`: Playwright specs and screen capture

Phase 2a of `webapps/work-orders/WS-GOV-1.md`. The consuming app's opt-in (Phase 2b) is a separate
work order in that app's repo, written after this one is tagged. First consumer: `kerzenziehen`.

---

# A. Envelope

## Goal & expected outcome

- **Ziel:** an app that opts in gets its Playwright specs and its screen capture run in CI against the
  image built from the commit under test, with seed data only.
- **Expected outcome:**
  - `app-ci.yml` gains an **optional** job, off by default, that a caller enables with one input.
    Callers that do not enable it see no change.
  - The job starts the app from the **`final` stage of the app's own backend image built from the
    commit under test** — the image already contains the built frontend, so what is tested is exactly
    what that commit ships — together with a database and Redis, using the app's E2E settings module
    and its guarded seed command.
  - Playwright runs **inside the official Playwright image** matching the app's `@playwright/test`
    version, on the host network, so the runner host needs Docker only (already proven by the
    backend job) and no browser dependencies.
  - The specs' result gates the job. Specs tagged `@quarantine` run in a separate, non-blocking step
    whose result is visible in the job summary.
  - The capture runs after the specs and is uploaded as an artifact (screenshots + report), with the
    commit it was built from recorded; its mechanical failures are reported, not gating, in this
    phase.

## Scope

- `.github/workflows/app-ci.yml` in this repo: the new job and its inputs (enable flag, frontend path,
  E2E settings module, and whatever the app must name for its seed and reseed to work in CI).
- The app side (tagging, reseed target configurable for CI, the opt-in) is Phase 2b, not this WO.

## Explicit non-goals / do-not-touch

- No change to the existing `backend`, `security` and `frontend` jobs or their defaults.
- No LLM step in CI, no judgement of the capture here.
- No new secret: the job uses seed users only, never an app's `.env` or environment secrets.
- No deploy, no push-triggered run — the job runs where `app-ci.yml` already runs (the callers'
  `pull_request` trigger).

## Tier · precondition / gate

- **Tier 3:** CI, and a shared workflow every app calls.
- **Proof before landing:** exception (b) of `AGENTS.md` → Branching — a `ci-test/WFT-CI-30` ref here
  and a `ci-test/<ID>` ref in `kerzenziehen` pointing at `app-ci.yml@ci-test/WFT-CI-30`, with one real
  run showing the specs green, the quarantined spec reported, and the capture artifact present. A
  second run on a caller that does **not** opt in shows no new job.
- **Ordering:** landing waits for `kerzenziehen/KZ-FIX-3` (landed); the known live-view defect is
  handled by the quarantine (`kerzenziehen/KZ-FIX-4`). Released as a new minor tag; callers pin to it.

## Risks

- GitHub evaluates `${{ }}` anywhere in the file, input descriptions included; an expression in a
  description makes the reusable workflow unresolvable for every caller, with no jobs and no logs.
- The runner slots on `netcup-runner-1` are shared; the job's containers must be uniquely named per
  run and always removed, or parallel jobs collide and the host fills up.
- The Playwright image version must match the app's `@playwright/test` version, or the browsers do
  not start.
- Run time: an image build plus the suite plus the capture; acceptable up to roughly 15 minutes.

## Required tests to WRITE

- A structural test of the new job and inputs (off by default; quarantine step non-blocking; capture
  uploaded), **shown to fail on a broken copy** of the workflow (mutation check).
- The two real runs named above are the rest of the evidence.

---

# B. Implementation map — filled by the Orchestrator

(Placeholder — not dispatchable in this state.)

---

# C. Orchestrator only — NOT ADDRESSED TO THE IMPLEMENTER

> **If you are the implementer reading this work order as your own specification: STOP at this
> line.** Everything below describes what the Orchestrator does after you finish.

(Filled by the Orchestrator.)
