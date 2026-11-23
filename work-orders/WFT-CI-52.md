# WFT-CI-52 — Run E2E checks without GitHub artifact storage by default

## A. Envelope

Tier 3. Target repository `workflow-templates`, trunk `main`. Gate #1 approved on 2026-11-23 when the operator said “Okay, bitte das so anpassen” to the proposed upload opt-in and job summary.

Netcup runs the app E2E checks. GitHub's artifact quota is full, and Fitness Monitor run 45062566130 failed at upload after the functional and capture checks had passed. A full artifact quota must not affect a routine check or its existing gate.

- Preserve the app's Playwright and quarantine checks, capture execution, reseeding, freshness checks, gating and stack cleanup.
- Default both E2E artifact uploads to off, including on failures. Provide one documented boolean reusable-workflow input to enable the existing capture and failure artifacts for targeted runs. Preserve redaction, existing eligibility and retention when enabled. Indicate in failure summaries whether diagnostics were uploaded or disabled, without promising files before upload.
- Report capture results per declared view and width in the GitHub job summary, including missing evidence without presenting it as passed. Publish only bounded identifiers and check results. Keep screenshots, traces, response bodies and arbitrary diagnostic content out of the summary. The summary itself is diagnostic and does not change the existing test or capture exit policy.
- Use one existing Netcup app for the no-upload candidate CI proof. Scope any caller change to the required temporary CI proof. Keep app production code, billing settings, storage deletion and existing app WO acceptance criteria outside this order. Evidence explicitly requiring a downloadable artifact still requires a local substitute approved under that app WO or a later opt-in upload.
- Extend the workflow tests to cover default and opt-in upload conditions, missing and failed capture evidence, non-disclosure of report diagnostics, and preservation of the existing capture gate.

## B. Implementation map

- `.github/workflows/app-ci.yml` declares reusable E2E inputs and the shared Netcup E2E job.
- `.github/workflows/app-ci.yml` includes Playwright and quarantine summaries, capture report paths and the two artifact upload steps.
- App capture manifests list `screens` with `id`, `widths` and optional `state: n.a.`. Their capture reports contain `cells` with `id`, `width` and `failures`. The workspace E2E harness playbook describes these contracts.
- `.github/scripts/test_app_ci_e2e_job.py` and `.github/scripts/test_app_ci_e2e_stack.py` verify the live workflow definitions. New focused cases can live in `.github/scripts/test_app_ci_e2e_reporting.py`.
- `project.yaml` declares the platform's scoped E2E workflow test entry point.

## Preamble

> You are the implementer. Work from the absolute `workflow-templates` target in the generated dispatch state. Own only the paths listed in the launch. Leave parallel changes alone. The orchestrator owns this WO, runner configuration, review, integration checks, Git and live CI.
>
> Run the exact hook canary command `codex-hook-canary-ws-gov-60` first. The expected hook denial confirms coverage. If the sandbox reports that the command is unavailable or denies the tool before the hook responds, report unverified coverage and continue with the active protections. Work without network, server access, Docker, secrets or environment-file access. Edit source and tests only. You may run one invocation of the focused tests you write, no installs, other tests, Git writes or worker dispatch. If tests are unavailable in your sandbox, state the observed reason and leave the authoritative run to the orchestrator.
>
> Print PLAN, periodic PROGRESS and exactly one closing RESULT: DONE or RESULT: BLOCKED with the concrete blocker.

## C. Orchestrator only

> Implementer stops here. This section is for the Orchestrator only.

The orchestrator runs the declared E2E workflow scope and independent envelope, regression, duplication, tests and infrastructure security review. Prove the exact workflow ref using one existing Netcup app caller on a matching temporary ref. Primary evidence is the real E2E result, capture summary and skipped uploads on the no-upload candidate. Preserve the app workflow on its trunk.

### Local evidence

Part B accurate. Codex Terra 6.1 high implemented the workflow. Terra 6.1 medium addressed both review batches. The scoped test entry point returns 105 passed and 2 skipped (approval run `tests-workflow-templates-20261123-223633/app-ci-e2e.log`). The unchanged 022f2ae baseline had five stale assertions. The updated fixtures preserve the existing Redis health check, teardown and manual SPESIX selection contracts. The capture summary reports a complete row from a valid, still-accumulating report as soon as that row is written and labels a nonzero capture command separately from passed widths.

Independent rounds 1 and 2 used Codex Terra 6.1 for envelope, regression, tests and infrastructure security, and Codex Luna for duplication. Six of seven findings were accepted and addressed. Automatic upload removal was the explicit approved default. Closing-pass fixes were tested without a third review. The test run covers affirmative and unavailable artifact status, stale and missing evidence, successive overwrites and a failing capture command with a fresh report.

The shared `e2e-upload-artifacts` input defaults to false. A caller can set it to true under the reusable-workflow job's `with` only for a targeted run that needs downloadable evidence. The normal CI run keeps the checks and records their results in the job summary. Capture images and failure diagnostics are generated during the test run but are not a durable off-run archive when upload is disabled.

The proposed Fitness Monitor evidence checkout changes only the reusable-workflow reference from `@main` to `@ci-test/WFT-CI-52`. Its dispatch uses `ci.yml` with `e2e=true`, with the artifact opt-in absent.

The reviewed template candidate is `1d9602bea04686a23cecded2fcf8081d7d9c7c35`; the separate Fitness Monitor evidence commit is `9eb7016f258a44c64a397438163257782271e148`. The sanctioned `ci-proof.py push` refused the modified workflow path against `origin/main`. The operator published both temporary refs and dispatched the proof. The orchestrator verified the two remote SHA values and the run's caller SHA.

[Fitness Monitor run 45271818345](https://github.com/bigler-webapps/fitness-monitor/actions/runs/45271818345) passed all six jobs. The `ci / e2e` job completed in 3m36s on `netcup-runner-1-slot1` (`netcup-ci`). Playwright, per-cell reseeding, capture and cleanup completed. Both artifact upload steps were skipped and the run's artifact API returned zero artifacts. The live capture log records normal completion. The exact per-width summary rendering is covered by the local workflow-shell tests. GitHub's check-run API does not return the rendered job summary and the available in-app browser lacked an authenticated GitHub session.
