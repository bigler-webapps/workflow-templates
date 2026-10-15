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

*To be filled by the Orchestrator.*

# C. Orchestrator only — not addressed to the implementer

> **If this WO was dispatched to you as an implementer: STOP here.**

Gate #1 open until recorded in the register row. Shared `app-ci.yml` change: candidate ref in `workflow-templates` plus one app pointing at `app-ci.yml@ci-test/WFT-CI-40` (AGENTS.md exception b).
