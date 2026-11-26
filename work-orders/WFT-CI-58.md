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

## Completion

Implemented by codex (implementation role, level high); Orchestrator fixed the review finding and the two existing tests that pinned the old constant.

- Change: both actions write `days=90` or `days=7` from the run's own `GITHUB_REF` (`refs/heads/main` exactly) into a step output and pass it as `retention-days` to the manifest upload and, for the static release, the bundle upload. No input reaches the value; inputs, outputs and artifact names unchanged. New scope `release-actions` in `project.yaml` (WSL pytest) covers `test_release_build_action.py` and `test_static_release.py`, which no scope ran before. CHANGELOG 2.19.0.
- Tests: `tests.py --repo workflow-templates --scope release-actions`: 46 passed, 0 failed. The retention tests execute the shell step with five refs (`main` 90; `develop`, `ci-test/x`, `main2`, `refs/tags/main` 7).
- Review: codex/gpt-6-luna · lenses: envelope, regression, duplication, tests, sec_review · 2 rounds · 1 raised · 1 accepted · worst accepted: defect (retention test only matched text; now executes the snippet). Round 2: no findings.
- Deviation from the WM-INF-153 test plan: no `ci-test/WM-INF-153` proof on a caller. reimbursements `release-build.yml` triggers only on pushes to `develop` and `main` and has no dispatch, and the ci-test script refuses a changed `.github/actions`. The caller proof is the reimbursements `develop` build on the new pin (RMB-CI-8): its artifact `expires_at` is recorded there (non-main ref, 7 days). The 90-day branch is proven by the executed-shell tests and confirmed in production after the next reimbursements promotion (carried in WM-INF-153).
- Part B: accurate
- Hook canary: unverified (implementer reported it unavailable).

### Retention proof (2026-11-26)

Promotion reimbursements v0.7.1 (PR 106, merge f0e4febb, tag v0.7.1). The `main` release build run 45895190919 ran on workflow-templates v2.19.0 and its artifact `release-manifest-45895190919-1` expires 2027-02-24T21:01:25Z, 90 days after creation (builds before the pin: 7 days). Status: done.
