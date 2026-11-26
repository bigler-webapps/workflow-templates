# WFT-CI-58 — 90-day release manifest retention for builds from main

Workflow-templates part of `webapp-management/work-orders/WM-INF-153.md`; the Envelope, risks, tests and Gate record live there.

## A. Envelope

**Target repository** `workflow-templates`

**Tier 3.** Shared CI on the production release path (infra lane).

`publish-backend-image` (release contract v1) and `publish-static-release` upload the release manifest (and the static bundle) with `retention-days: 90` when the run ref is `refs/heads/main`, and 7 otherwise. The value is decided inside the action from the run's own ref; no input can change it. Names, inputs and outputs are unchanged. Released as the next minor tag.

### Required tests

- Both actions: 90 days exactly for `refs/heads/main`, 7 for every other ref, no input reaches the value; inputs and outputs unchanged.

## B. Implementation map

- `.github/actions/publish-backend-image/action.yml` (manifest upload `:105-114`).
- `.github/actions/publish-static-release/action.yml` (bundle upload `:49-56`, manifest upload `:69-77`).
- Tests next to `.github/scripts/test_release_build_action.py` and `test_static_release.py`.
- Caller proof: reimbursements `release-build.yml` on `ci-test/WM-INF-153` (RMB-CI-8).

## Preamble

> You are the dispatched implementer. Run `codex-hook-canary-ws-gov-60` as the first tool action.
> Continue on the expected denial. Otherwise report unverified coverage once and continue under the existing gates.
> Read this Envelope, webapp-management/work-orders/WM-INF-153.md and its Gate record, the nearest governance and the relevant role skill.
> Edit only the two named actions and their tests and run only the new tests you wrote. Preserve parallel work.
> Do not access hosts or credentials, dispatch jobs or agents, run reviews, edit the register or write to git.
> Report progress and end with `RESULT: DONE` or `RESULT: BLOCKED` with the test evidence.

## C. Orchestrator only

> Implementer: stop here. The Orchestrator owns review, evidence and landing.

Proof through `ci-test/WM-INF-153` here and the caller ref in reimbursements; tag after landing.

## Gate record

Gate #1 is recorded in `webapp-management/work-orders/WM-INF-153.md`.
