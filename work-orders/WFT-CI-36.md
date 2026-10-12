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
- No `spesix` pin bump: measured 2026-10-12, `spesix/.github/workflows/ci.yml` line 14 consumes
  `app-ci.yml@main`, a floating reference, so the fix reaches its CI (and every other app's) when it lands on
  `workflow-templates` `main`. This corrects an earlier version of this line, which assumed a tag pin.

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

- Because every caller consumes `@main`, a defect in the new `if:` expression is fleet-wide the moment it lands;
  hence the evidence on `ci-test` refs before `main`.
- A guard that is too strict could stop the mail files from reaching the failure artifact in the normal case. The
  normal case (prepare step ran, a spec failed) must still copy exactly as before.
- The same pattern may exist in other shared workflows; this WO audits the `e2e` job of `app-ci.yml` only.

## Tests

The structural test above (extending `test_app_ci_e2e_job.py`). The normal path and the unset-variable path are both
shown, the unset path by running the step's shell with the variable empty against a temporary directory and asserting
nothing outside it is read.

# B. Implementation map — ADDRESSED TO THE IMPLEMENTER

## Target working directory

`C:\Users\Micha Bigler\Documents\webapps\workflow-templates` (Git Bash: `/c/Users/Micha Bigler/Documents/webapps/workflow-templates`). Never the workspace root.

## Context package

Work from this package; do not explore broadly. Open only the named files to verify.

- **Production file: `.github/workflows/app-ci.yml`, job `e2e` (starts about line 488).**
  - Step "Prepare shared E2E mail directory" (about 544-554), `if: ${{ inputs.e2e-mail-dir }}`: creates
    `$RUNNER_TEMP/e2e-mail` and exports `E2E_MAIL_HOST_DIR` (and the two `-v` argument strings) through `$GITHUB_ENV`.
    That is the only place the variable is set; it is NOT in the job-level `env:` block (that block, about 491-496,
    defines only `CI_IMAGE` and the `E2E_*` container/network names).
  - Step "Copy E2E mail files for failed specs" (about 765-770). Today:
    `if: ${{ always() && inputs.e2e-mail-dir && (failure() || steps.quarantine.outputs.had_failure == 'true') }}`,
    `working-directory: ${{ inputs.frontend-path }}`, and a `run:` of exactly
    `mkdir -p test-results/e2e-mail` then `cp -a "$E2E_MAIL_HOST_DIR/." test-results/e2e-mail/ || true`, with no `set -u`.
    That is the defect: it runs under `always()` even when the prepare step never ran, the variable is empty, and the
    source becomes `/.`.
  - **Keep the line `cp -a "$E2E_MAIL_HOST_DIR/." test-results/e2e-mail/ || true` byte for byte:**
    `test_app_ci_e2e_job.py` line 854 asserts it. The best-effort `|| true` stays.
- **What to change (the seam, not the solution):** the step must not run when `E2E_MAIL_HOST_DIR` is empty, and its shell
  must fail loudly rather than expand an unset variable. Two layers: the step's `if:` gains a test that the variable is
  non-empty (the `env` context carries values written to `$GITHUB_ENV` by earlier steps, but confirm that against the
  workflow's other uses before relying on it; this is verified live on the `ci-test` run), and the shell starts with
  `set -euo pipefail` plus an explicit refusal before `mkdir` when the variable is unset or empty
  (for example `: "${E2E_MAIL_HOST_DIR:?…}"`). The existing "failed specs only" condition stays.
- **Audit (report in your final message, including "nothing found"):** read the other `always()` steps of the `e2e` job and
  check each for a variable that is set by an earlier step and used unguarded. Measured by the Orchestrator and to be
  CONFIRMED by you: `Cleanup E2E containers…` (about 854-860) already guards with `${E2E_MAIL_HOST_DIR:-}`/`-n`;
  `Capture E2E screens and report` (about 788) uses `${E2E_MAIL_PW_ARGS:-}`; `$APP_PORT` (set at about line 577) is only
  interpolated into a URL there; `E2E_APP_CONTAINER` is a job-level `env`, always set. Change a step only if you find the
  same unguarded pattern with an effect that can read or write outside the job's own directory; otherwise leave it and say so.
- **Do not touch:** the mail-directory design (mount points, `E2E_MAIL_DIR`, the artifact upload step), any other job
  (`backend`, `frontend`, `security`), the inputs block, anything under `spesix`.

## Tests to write (and run — your only test run)

Extend `.github/scripts/test_app_ci_e2e_job.py` (unittest style; helpers `step_body(job, name)`, `E2E_JOB`; neighbouring
tests around lines 825-870 show the "structural check plus `test_assertion_fails_if_…` mutation twin" pattern — follow it):

1. Structural: the copy step's `if:` requires a non-empty `E2E_MAIL_HOST_DIR` in addition to its existing conditions, and
   its `run:` starts with `set -euo pipefail` and refuses when the variable is unset or empty before `mkdir`.
2. Mutation twin: dropping the `E2E_MAIL_HOST_DIR` test from the `if:`, or dropping the refusal, makes the structural check fail
   (build the mutated text in the test and assert the check's predicate rejects it, as the neighbours do).
3. Behavioural (`unittest.skipIf` on `sys.platform == "win32"` or no `bash`): extract the step's `run:` text, write it to a
   temp script, run it with `bash` in a temp working directory with a PATH-first stub `cp` that logs its arguments:
   - variable unset: exit status non-zero, the stub was never called with a source ending in `/.`, nothing was created
     outside the temp directory;
   - variable set to a temp directory holding one file: exit 0 and that file is present under `test-results/e2e-mail/`.
4. The existing line-854 assertion and all existing tests in the file still hold.

Run your tests once with `python .github/scripts/test_app_ci_e2e_job.py` from the repository root (Windows: the behavioural
cases skip; that is expected, the Orchestrator runs them on Linux).

## Preamble

The text above is the COMPLETE spec — the committed WO file's content, not a plan to refine; there is no
separate plan file. Read the nearest `AGENTS.md` only for conventions. Stay in scope; do not touch
auth/permissions/deps/schema/CI unless the spec says so (this WO IS the named CI change: only the step and tests named
above); do not update `MEMORY.md`. **Do NOT edit `WORK_ORDERS.md` — the register row and the review verdicts are the
orchestrator's alone.** **Your tools are for editing source and test files and for running the tests you wrote — nothing
else.** Do NOT install dependencies, touch a lockfile, run a package manager, or tidy up stray files; if something in the
repo state blocks you, stop and report it as `RESULT: BLOCKED <reason>` instead of fixing it. Do NOT
`git add`/`commit`/`push` — leave every change uncommitted in the working tree for the orchestrator's independent review.
WRITE the tests the "Tests to write" section calls for AND **RUN the tests you just wrote** to confirm they execute and
pass — that is the ONLY test run you do (NOT the affected/full suite, NOT any review). The orchestrator re-runs the
authoritative set and does the independent review after you finish; your own run does not count as the gate.

Narrate continuously: a `PLAN: <step1> | <step2> | …` line up front, then a single-line
`PROGRESS: [<n>/<total>] <present-tense action>` before every relevant action (and `… done` on completion),
spaced so no gap exceeds ~2 min, stdout unbuffered, plus exactly one final `RESULT: DONE|BLOCKED <reason>`.

# C. Orchestrator only — NOT ADDRESSED TO THE IMPLEMENTER

> **If you are the implementer reading this work order as your own specification: STOP at this line.
> Everything below describes what the Orchestrator does AFTER you finish.**

## Review

`reviewer` (all lenses) and `sec_reviewer`, concurrent, full context, static only.

## Execution directive

`codex exec` in the background, cwd = `workflow-templates` repo root, `-m gpt-6-luna`, WO on stdin. Read the banner back.

## Verification

A change to the shared `app-ci.yml` needs a ref on BOTH sides: the candidate on `ci-test/WFT-CI-36` in
`workflow-templates`, and `spesix`'s `ci.yml` pointed at `app-ci.yml@ci-test/WFT-CI-36` on a `ci-test/WFT-CI-36` ref
of `spesix`; the spesix run is the evidence. `spesix`'s local `develop` carries another session's unpublished WIP commits,
so that ref is built with git plumbing on top of `origin/develop`, containing only the one-line `ci.yml` change.
The failed-spec copy path cannot be forced live without a red spec: the behavioural test (Linux) is its evidence, and
the live run shows the normal path unchanged; both are declared in the Notiz. Local scoped run:
`python .github/scripts/test_app_ci_e2e_job.py` plus `test_app_ci_composite_checkout.py` and `test_app_ci_security_gates.py`.

## Register

Row `WFT-CI-36`, status `planned`; Gate #1 is not yet given.
