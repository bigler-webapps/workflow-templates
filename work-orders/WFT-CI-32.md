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

## Context package

**Named file:** `.github/workflows/app-ci.yml`, the `e2e` job only — the `Playwright specs` step
(`id: playwright_specs`), the `Playwright quarantine specs` step (`id: quarantine`), and one new step
between `quarantine` and `Capture E2E screens and report`.

**Three changes, each independently justified:**

1. **`--trace retain-on-failure` on every `npx playwright test` invocation**, in both the gating and
   quarantine loops. A CLI flag overrides the caller's own `playwright.config.js` `use.trace` setting
   for this run only — no caller config changes, matching the explicit non-goal. `retries: 0` and the
   gating rules are untouched (the WO's own non-goal names these explicitly) — only trace capture
   changes.
2. **Per-invocation output isolation in the QUARANTINE loop only** — not the gating loop. Playwright
   wipes its `test-results/` output directory at the START of each `npx playwright test` invocation.
   The gating loop's `set -euo pipefail` already stops at the FIRST failing file, so there is only ever
   one invocation's worth of output to upload by construction. The quarantine loop keeps iterating
   after a failure (non-blocking), so a second file's run would silently wipe the first file's trace —
   give each invocation its own subdirectory:
   `--output "test-results/$(echo "$f" | tr '/' '-')"` (computed by the OUTER/runner shell, same
   interpolation style already used for `'$f'` in both loops — not evaluated inside the container).
3. **One new step, `if: always() && (failure() || steps.quarantine.outputs.had_failure == 'true')`**,
   uploading `${{ inputs.frontend-path }}/test-results` as
   `e2e-failures-${{ github.run_id }}-${{ github.run_attempt }}`, `retention-days: 7`,
   `if-no-files-found: ignore` (this is what makes "a green run uploads nothing extra" need no extra
   bash logic at all — `retain-on-failure` means a fully-passing run leaves `test-results/` empty, and
   `ignore` on an empty match is silent by design, already the convention this file uses for the
   capture artifact). `failure()` alone would NOT see a quarantine-only failure, because
   `continue-on-error: true` reports that step's own conclusion as `success` regardless of its internal
   exit code — hence the quarantine step needs its own explicit output. Add to the quarantine step,
   right before its existing `exit 0`:
   `echo "had_failure=$([ "$worst" -ne 0 ] && echo true || echo false)" >> "$GITHUB_OUTPUT"`.

**Job-summary requirement** ("names the artifact next to the failed spec"): the artifact's name is
fully computable from `github.run_id`/`github.run_attempt` context values ALREADY, before the upload
step even runs — reference it directly at the point of failure in each loop, not only in the later
upload step:
- Gating loop: replace the bare `docker run ... bash -lc "npx playwright test '$f'"` with an explicit
  `if ! docker run ...; then` so a summary line can be written before the step's own `exit 1` (today
  the loop relies on `set -e` propagating the raw exit code with no chance to annotate first).
- Quarantine loop: after the existing per-file `status=$?` check, when `status -ne 0`, append a
  summary line naming `$f` and the artifact.

**Required tests** — per Part A, write these against the PARSED YAML (`yaml.safe_load`), not string
slicing, in the existing `.github/scripts/test_app_ci_e2e_job.py` (same file as `WFT-CI-30`'s other
`e2e`-job tests — this is a same-file follow-up, not a new test file): load the workflow, navigate
`data['jobs']['e2e']['steps']` to find each named step by its `name` key, assert the substrings above
appear in that step's `run` string. Show at least one case failing on a deliberately broken copy
(mutation test), per this file's own established convention.

## Target repo working directory (absolute)

`C:\Users\biglmi\Documents\webapps\workflow-templates`

## Preamble

Implemented directly by the Orchestrator (Codex recorded `unavailable` for today in
`.claude/codex-status.md`, the newest entry — see the rule's own "skip the attempt entirely" clause).
Author = Orchestrator, so the independent review is mandatory regardless (already true, Tier 3).

---

# C. Orchestrator only — NOT ADDRESSED TO THE IMPLEMENTER

## Execution directive

Implemented directly (see preamble above) — no `codex exec` dispatch this round.

## Review routing

Tier 3: `reviewer` (all four lenses) + `sec_reviewer` (traces record what the browser typed, including
the fixed E2E seed password already committed in callers' own repos — the Envelope's own named risk),
concurrent, one batch.

## Verification

Scoped: `python .github/scripts/test_app_ci_e2e_job.py` (existing suite + new cases). Live proof,
`ci-test/WFT-CI-32` refs on both sides (this repo + `kerzenziehen`, already on `WFT-CI-30`/`KZ-E2E-9`):
one run with a deliberately failing spec showing the artifact with trace + error context, one green
run uploading nothing extra. Coordinate with any active `kerzenziehen` session before touching that
repo's working tree (shared checkout).

## Register + commit

Row → `done` with the review Notiz + both live-proof run IDs. Keep every commit on `ci-test/WFT-CI-32`
until both runs are green — `git log origin/main..HEAD` before every push to `main` (this repo's own
`WFT-CI-30`/`WFT-CI-31` register rows record why this check is non-negotiable).

## Report back

`local_e275ef27-dda0-4a16-996a-ce290719c917`, per the mini-handover.
