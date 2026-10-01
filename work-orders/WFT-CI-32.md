# WFT-CI-32 — The e2e job keeps the evidence of a failing spec

Follow-up to `WFT-CI-30`. When a Playwright spec fails in the `e2e` job of `app-ci.yml`, nothing
survives the run but the console log: callers' configs record traces only `on-first-retry` with
`retries: 0`, and `test-results/` (error context, screenshots, video) is not uploaded. The first real
red run (`kerzenziehen` run `36862259972`, two attempts, two different failures) therefore could only
be described, not diagnosed (`kerzenziehen/KZ-FIX-9`).

---

# A. Envelope

## Goal & expected outcome

- **Ziel:** every failing spec in the `e2e` job leaves a trace and its error context behind.
- **Expected outcome:**
  - The job runs Playwright with traces retained on failure, overriding the caller's config for CI
    only (for example with the CLI's `--trace retain-on-failure`). Passing specs keep no trace.
  - When any spec file — gating or quarantined — fails, the job uploads that run's Playwright output
    (traces, error contexts, screenshots) as an artifact with a short retention, named per run and
    attempt. A run without failures uploads nothing extra.
  - The job summary names the artifact next to the failed spec.

## Scope

- `.github/workflows/app-ci.yml`, the `e2e` job only.

## Explicit non-goals / do-not-touch

- No change to callers' Playwright configs, to retries, or to the gating rules.
- No change to the capture or its artifact.
- No new secret.

## Tier · precondition / gate

- **Tier 3:** CI, a shared workflow every app calls.
- **Proof before landing**, via `ci-test` refs on both sides (`AGENTS.md` → Branching exception (b)):
  one run where a spec fails on purpose and the artifact contains its trace and error context, and one
  green run that uploads nothing extra. A caller that does not opt into `e2e` is unaffected.

## Risks

- Traces record what the browser typed, including the fixed E2E seed password. It is already committed
  in the callers' repositories, so the artifact exposes nothing new to anyone who can read the run;
  retention stays short anyway.
- An expression inside an input description breaks the workflow for every caller (WFT-CI-23).
- Artifact size grows with traces; keep only failing specs' output.

## Required tests to WRITE

- Structural tests of the new step and flags, parsed with a real YAML loader (not string slicing —
  `WFT-CI-31` showed that misses invalid YAML) and **shown to fail on a broken copy**.
- The two proof runs above are the rest of the evidence.

---

# B. Implementation map — filled by the Orchestrator

(Placeholder — not dispatchable in this state.)

---

# C. Orchestrator only — NOT ADDRESSED TO THE IMPLEMENTER

> **If you are the implementer reading this work order as your own specification: STOP at this
> line.** Everything below describes what the Orchestrator does after you finish.

(Filled by the Orchestrator.)
