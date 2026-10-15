# WFT-CI-40 — Playwright containers cannot write the repository's `.git` (S261)

Prepared: 2026-10-15. Target repository: `workflow-templates` (`main`). Finding: `webapp-management/SECURITY_FINDINGS.md` S261 (P2).

# A. Envelope — addressed to everyone

## Goal and expected outcome

The E2E, quarantine and capture containers of the shared `app-ci.yml` can no longer write the checkout's `.git` directory. A malicious package running inside one of these containers cannot plant a Git hook that the next `actions/checkout` runs on the self-hosted runner host. The capture provenance check (`git rev-parse HEAD`, KZ-E2E-8) keeps working.

## Why

Since `WFT-CI-30` (commit `74000d7`) the four Playwright `docker run` calls mount the whole workspace read-write (`-v "${{ github.workspace }}:/workspace"`), `.git` included. `actions/checkout` reuses the workspace on the self-hosted runner and leaves `.git/hooks` in place. A hook written from inside the container would run on the host as the runner account, which has passwordless sudo (S227). Found by the routine scan of 2026-10-01; still present on 2026-10-15 (`app-ci.yml` lines 657, 701, 769 and the capture step).

## Tier

Tier 3: shared CI, runner host security.

## Scope

1. In every Playwright/capture `docker run` of `app-ci.yml`, `.git` is not writable from the container: either it is mounted read-only over the read-write workspace, or only the frontend path and the output directories are mounted writable and `.git` read-only. The choice is the implementer's; the outcome is fixed.
2. The provenance check that needs `git rev-parse HEAD` still gets the real HEAD.
3. A release of `workflow-templates` per its normal procedure; the apps pick it up through their existing pins (no consumer change in this order).
4. `SECURITY_FINDINGS.md` S261 is set to done with the release, in a second step in `webapp-management`.

## Non-goals

- S227 (the runner account's sudo) and S263 (failure artifact retention): separate.
- Any other job of `app-ci.yml`.

## Risks

- A spec or script that writes into `.git` or outside its directories breaks; that is the intended effect and shows in the first CI run.

## Required tests to write

- The repository's structural workflow tests assert that each Playwright/capture `docker run` keeps `.git` read-only (no read-write mount that contains `.git`).
- A CI run on an app pointing at the candidate (`ci-test` ref on both sides) shows the E2E and capture jobs green with the provenance check passing.

# B. Implementation map — addressed to the implementer

## Context package

- **File to change:** `.github/workflows/app-ci.yml`, job `e2e`. Every Playwright container that mounts `-v "${{ github.workspace }}:/workspace"` (steps: Install Playwright dependencies ~l.654, Playwright specs ~l.696, quarantine loop ~l.764, Capture E2E (cell enumeration ~l.838 and capture run ~l.865)). Chosen approach: directly after each such workspace mount add a nested read-only mount `-v "${{ github.workspace }}/.git:/workspace/.git:ro" \`. Docker applies the nested ro mount over the rw bind. Do not touch the `docker run` calls at ~l.296/323 or the DB/Redis/app containers (they do not mount the workspace) - verify with grep which runs mount the workspace.
- **Provenance:** the capture run already sets `GIT_CONFIG_*` safe.directory=/workspace; `git rev-parse HEAD` only reads, so no change there.
- **Test file:** `.github/scripts/test_app_ci_e2e_job.py` (unittest, inspects `E2E_JOB` text; helpers `step_body`, `shell_body`). Add one test class/method asserting: every `docker run` in `E2E_JOB` that contains a `github.workspace` mount of `/workspace` also contains the `.git:/workspace/.git:ro` mount, and that no `docker run` mounts a read-write path that contains `.git` without it. Count the runs (expect 5) so a new unprotected run fails. Assert against the text of each docker-run command (split on `docker run`, take until the image / `bash -lc` line).
- **Do not touch:** other jobs, inputs, other tests, CHANGELOG/README/WORK_ORDERS.md.
- Run only: `python .github/scripts/test_app_ci_e2e_job.py` (your own new test; full file is fine, it is the file's own suite).
- Directive: work from this package; open only the named files.

## Target repo working directory (absolute)

`C:/Users/Micha Bigler/Documents/webapps/workflow-templates`

## Preamble

> The text above is the COMPLETE spec - the committed WO file's content, not a plan to refine. Read the nearest `AGENTS.md` ONLY for conventions. Stay in scope; do not touch auth/permissions/deps/schema/CI beyond what the spec says; do not update `MEMORY.md`. **Do NOT edit `WORK_ORDERS.md` - the register row and the review verdicts are the orchestrator's alone.** **Your tools are for editing source and test files and for running the tests you wrote - nothing else.** Do NOT install dependencies, touch a lockfile, run a package manager, or tidy up stray files; if something in the repo state blocks you, stop and report `RESULT: BLOCKED <reason>`. Do NOT `git add`/`commit`/`push` - leave every change uncommitted for the orchestrator's independent review. WRITE the tests the `Required tests` section calls for AND RUN the tests you just wrote - that is the ONLY test run you do (no review, no wider suite). The orchestrator's run is the gate. Use explicit UTF-8 for file I/O.
>
> Narrate continuously: a `PLAN: <step1> | <step2> | ...` line up front, then a single-line `PROGRESS: [<n>/<total>] <present-tense action>` before every relevant action (and `... done` on completion), plus exactly one final `RESULT: DONE|BLOCKED <reason>`.

# C. Orchestrator only — not addressed to the implementer

> **If you are the implementer reading this work order as your own specification: STOP READING at this line - Part C is not for you, and Parts A and B ARE your task: do them now.** Everything below describes what the Orchestrator does AFTER you finish. You do none of it - no reviewers, no verification run, no register edit, no commit.

Gate #1 open until recorded in the register row. Shared `app-ci.yml` change: candidate ref in `workflow-templates` plus one app pointing at `app-ci.yml@ci-test/WFT-CI-40` (AGENTS.md exception b).
