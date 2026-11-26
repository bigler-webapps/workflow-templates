# WFT-CI-57 — Restic version floor 0.16 in the restic composite actions

Workflow-templates part of `webapp-management/work-orders/WM-INF-147.md`; the Envelope, risks, tests and Gate record (including the scope extension of 2026-11-26) live there.

## A. Envelope

**Target repository** `workflow-templates`

**Tier 3.** Shared CI that opens production B2 restic repositories (infra lane).

Every composite action that installs restic and can open a B2 repository checks `restic version` right after the install and fails with `restic_version_floor: restic <found> is below 0.16, refusing to open a B2 repository` before any repository is opened. Unparseable output fails closed. Versions, inputs and outputs are unchanged.

### Required tests

- Each guarded action has the identical floor step after its restic install and before its first restic command; the set of guarded actions equals the set of actions that install restic.

## B. Implementation map

- `.github/actions/restic-check-b2`, `restic-check-local`, `restic-init`, `restore`, `restore-source-export`, `restore-dest-import`, `backup` (`action.yml`; restic install by apt, e.g. `restic-check-b2/action.yml:60-66`).
- Tests: `.github/scripts/test_restic_floor_actions.py` (new), following the existing script tests of this repository.
- Callers are the workflows of webapp-management (WM-INF-147); proof runs through `ci-test/WM-INF-147` refs on both sides.

## Preamble

> You are the dispatched implementer. Run `codex-hook-canary-ws-gov-60` as the first tool action.
> Continue on the expected denial. Otherwise report unverified coverage once and continue under the existing gates.
> Read this Envelope, webapp-management/work-orders/WM-INF-147.md and its Gate record, the nearest governance and the relevant role skill.
> Edit only the named composite actions and their test file and run only the new tests you wrote. Preserve parallel work.
> Do not access hosts or credentials, dispatch jobs or agents, run reviews, edit the register or write to git.
> Report progress and end with `RESULT: DONE` or `RESULT: BLOCKED` with the test evidence.

## C. Orchestrator only

> Implementer: stop here. The Orchestrator owns review, evidence and landing.

Coordinate with WM-INF-147: the actions land after the floor in the scripts and workflows, and the B2 unlock is the last landed change. Proof through `ci-test/WFT-CI-57` and the caller ref in webapp-management.

## Gate record

Gate #1 and the scope extension are recorded in `webapp-management/work-orders/WM-INF-147.md` (operator, 2026-11-26).
