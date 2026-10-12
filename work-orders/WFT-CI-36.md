# WFT-CI-36 — The failed-spec mail copy must never run with an unset mail directory

# A. Envelope

## Goal

A failing E2E job can no longer copy the runner's whole root filesystem into the job workspace. The step that
collects mail files for a failed spec runs only when the shared mail directory was actually prepared.

## Finding

Measured 2026-10-12 on `netcup-runner-1`, slot 2: `/opt/gh-runner/slot-2/_work/spesix/spesix/frontend/test-results/e2e-mail`
holds **67 G**, and it contains a directory `proc` with the layout of the host's `/proc` (PID directories `1`, `10`,
`100`, …, files `cmdline`, `mountinfo`, `igmp`, and a 71 GB file `pagemap`). It is a real copy on ext4, not a mount.
Docker itself accounts for only about 26 G of the 124 G used; these 67 G are the largest single consumer on the
host. The copy's file timestamps are 2026-10-12 07:34 UTC, matching the root disk reaching 99 % shortly after
(07:40) and 100 % when the threshold prune first ran at 08:27 UTC.

**Mechanism (read from the code):** `.github/workflows/app-ci.yml`

- step "Prepare shared E2E mail directory" (about line 544) sets `E2E_MAIL_HOST_DIR` through `$GITHUB_ENV`;
- step "Copy E2E mail files for failed specs" (about line 765) runs under
  `if: always() && inputs.e2e-mail-dir && (failure() || steps.quarantine.outputs.had_failure == 'true')` and does
  `cp -a "$E2E_MAIL_HOST_DIR/." test-results/e2e-mail/ || true`, **without** `set -u`.

`always()` makes the copy step run even when the job failed BEFORE the prepare step, so the variable is empty and
the source expands to `/.`. `cp -a /. …` then copies the host root, `/proc` included, until the disk fills; `|| true`
hides that failure. The copied tree is mode `dr-xr-xr-x`, so the next checkout's `git clean` cannot delete it and it
stays in the workspace.

**What is established and what is not.** The code path and the artefact on disk fit exactly. Which job triggered it
is not established (no job log was read), and no run was reproduced. The fix does not depend on identifying it.

Only `spesix` opts in to `e2e-mail-dir` today (`WFT-CI-35`), so it is the only consumer exposed.

## Scope

- `app-ci.yml`: the copy step runs only when `E2E_MAIL_HOST_DIR` is non-empty, and fails loudly on an unset
  variable (`set -euo pipefail`) instead of expanding it silently. The existing "failed specs only" condition stays.
- Audit the other `always()` steps of the `e2e` job in the same file for the same pattern: a variable that is set by
  an earlier step and then used unguarded in a step that can run without it (for example `E2E_APP_CONTAINER`).
  Fix those the same way; report what the audit found, including "nothing".
- A test in `.github/scripts/test_app_ci_e2e_job.py` (existing file, same style) that pins the guard on the mail copy
  step and the absence of an unguarded `$E2E_MAIL_HOST_DIR/.` source.
- `spesix`: bump its `app-ci.yml` pin to the new tag so the fix reaches its CI (it is the only opt-in app).

## Non-goals / do not touch

- The mail directory design itself (`WFT-CI-35`): mount points, `E2E_MAIL_DIR`, the artefact upload.
- The one-off removal of the existing 67 G: the operator does that on the host.
- The runner's Docker GC and the threshold prune (`WM-INF-80`).
- Any other consumer app; none opts in.

## Tier

**Tier 3** — CI, and the change is in the shared `app-ci.yml` that every app's CI calls. A defect here is visible
fleet-wide. Diff size is no guide.

## Precondition

The operator has removed `…/spesix/spesix/frontend/test-results/e2e-mail` on slot 2 (or accepts that it stays until
then). Not a blocker for the code change; it is why the slot's disk stays near 83 %.

## Risks

- A guard that is too strict could stop the mail files from reaching the failure artifact in the normal case. The
  normal case (prepare step ran, a spec failed) must still copy exactly as before.
- The same pattern may exist in other shared workflows; this WO audits the `e2e` job of `app-ci.yml` only.

## Tests

The structural test above (extending `test_app_ci_e2e_job.py`). The normal path and the unset-variable path are both
shown, the unset path by running the step's shell with the variable empty against a temporary directory and asserting
nothing outside it is read.

# B. Implementation map — to be filled by the Orchestrator

<!-- PLACEHOLDER: Part B (context package, preamble, progress contract) is filled by the Orchestrator on pull. Do not dispatch while this line is here. -->

# C. Orchestrator only — NOT ADDRESSED TO THE IMPLEMENTER

> **If you are the implementer reading this work order as your own specification: STOP at this line.
> Everything below describes what the Orchestrator does AFTER you finish.**

## Review

`reviewer` (all lenses) and `sec_reviewer`, concurrent, full context, static only.

## Verification

A change to the shared `app-ci.yml` needs a ref on BOTH sides: the candidate on `ci-test/WFT-CI-36` in
`workflow-templates`, and `spesix`'s `ci.yml` pointed at `app-ci.yml@ci-test/WFT-CI-36` on a `ci-test/WFT-CI-36` ref
of `spesix`; the spesix run is the evidence.

## Register

Row `WFT-CI-36`, status `planned`; Gate #1 is not yet given.
