# WFT-CI-54 — Keep isolated Playwright specs out of regular CI

## A. Envelope

**Target repository** `workflow-templates`

**Tier 3.** App or shared CI and executable test evidence.

### Evidence

Cinevia `45377726181`, Kerzenziehen `45377727689`, Spesix `45381646412` and Photogallery `45387318845` invoke `tests/test-management-profiles.spec.js` and fail with `No tests found`. The callers' regular Playwright projects exclude those specs when the isolated controller is unavailable. The template enumerates files before Playwright applies that filter.

### Goal and outcome

The regular E2E runner selects the regular files and never hands a controller-only spec to a regular project. Explicit requests for an isolated-only path fail with a useful configuration message before execution. Genuine empty regular selections and real failed specs remain failures.

### Scope and boundaries

Shared E2E file classification and selection in `app-ci.yml`, including the regular and quarantine inventories, the explicit `e2e-spec-files` input and structural regression coverage. Preserve deterministic ordering, per-file reseeding and regular/quarantine outcome semantics. Isolated acceptance remains owned by the existing controller. Artifact policy, app permissions, app pins and pytest collection are outside scope.

### Existing capabilities

The shared caller already handles per-file reseeding and validated explicit paths. Consumer Playwright configurations establish the `test-management-*.spec.js` exclusion. The app-owned isolated runner executes those specs separately.

### Risks

A broad filter could silently remove real coverage or permit a request the isolated runner should own. Regular, isolated and quarantined memberships and the empty case need independent assertions.

### Required tests

- Prove a mixed inventory runs regular specs, retains valid quarantine handling and omits the reserved isolated-only profile and recovery cases.
- Prove a regular explicit request still runs, a controller-only explicit request is rejected, no regular selection fails clearly, and an actual regular spec failure still blocks.

## B. Implementation map

- `.github/workflows/app-ci.yml` — `Playwright specs`, `Playwright quarantine specs`, `e2e-spec-files`, classification and reseed boundaries.
- `.github/scripts/test_app_ci_e2e_job.py` and `project.yaml` — E2E contract tests and `app-ci-e2e` scope.
- Cinevia, Kerzenziehen, Spesix and Photogallery — `frontend/playwright.config.js` and isolated profiles specs, read-only context.

## Preamble

> **You are the dispatched implementer of this work order, not its Orchestrator. Rules in `AGENTS.md`
> and in skills that address the Orchestrator, a Codex parent or the Expertenchat (delegation, dispatch,
> review, register, landing) do not apply to you. You never run `codex` or `codex exec`, never write to
> git, and never edit the register or `.claude/codex-status.md`.**
>
> **Your first tool action is the hook canary: run the shell command `codex-hook-canary-ws-gov-60`
> exactly (it is a command string, not a file). The expected result is a denial,
> `WS-GOV-60 canary: workspace hooks are active`; continue after it. If it is not denied, report
> that once as unverified hook coverage and continue.**
>
> The text above is the COMPLETE spec — the committed WO file's content, not a plan to refine; there
> is no separate plan file. Read the nearest `AGENTS.md`, the relevant `.codex/skills/<role>/SKILL.md`, and the
> app `MEMORY.md` ONLY for conventions. Stay in scope; do not touch auth/permissions/deps/schema/CI
> unless the spec says so; do not update `MEMORY.md`. **Do NOT edit `WORK_ORDERS.md` — the register
> row and the review verdicts are the orchestrator's alone.** **Your tools are for editing source
> and test files and for running the tests you wrote — nothing else.** Do NOT install dependencies,
> touch a lockfile, run a package manager, or tidy up stray files; if something in the repo state
> blocks you, stop and report it as `RESULT: BLOCKED <reason>` instead of fixing it. Do NOT
> `git add`/`commit`/`push` — leave every
> change uncommitted in the working tree for the orchestrator's independent review. WRITE the tests
> the `Required tests` section calls for AND **RUN the tests you just wrote** to confirm they execute
> and pass — that is the ONLY test run you do (NOT the app's affected/full suite, NOT any review).
> The orchestrator re-runs the authoritative set + does the independent review after you finish —
> those are the gate; your own run does not count as the gate.
>
> **Running your tests in the sandbox (WS-GOV-110, measured).** Django-core / Django app pytest: use the
> repo's own interpreter, `<repo>\.venv-test\Scripts\python.exe -m pytest -p no:cacheprovider <file>`; the bare
> `python` has no `django_core_micha`. Vitest works only when your writable root is that repo
> (`node node_modules/vitest/vitest.mjs run <file>`; with another root it fails with EPERM on
> `node_modules\.vite-temp`). Workspace gate suites: `python -I -B <test file>`. Never redirect a cache to
> make a tool work and never start a database or Docker. A test run that cannot work in the sandbox is NOT
> a blocker: when the code is complete and only the run is missing, end `RESULT: DONE` and add the line
> `TESTS NOT RUN: <reason>` (known reasons: needs PostgreSQL/Docker/WSL; SWC native addon refuses any
> sandbox cache folder, so Vitest on an SWC-plugin frontend such as jg-ferien cannot start). Use
> `RESULT: BLOCKED` only when the code itself cannot be completed.
>
> **Files.** Read a file over ~400 lines by locating with `Grep -n` and reading only the needed
> range — never the whole file, never the same range twice without a change in between. Write new code
> into cohesive modules named by behaviour: a new source or test file stays under ~400 lines; code
> added to a file already over ~800 lines goes into a new module unless it is a small change to
> existing logic. Name test files by the behaviour they cover, never by the work-order ID.
>
> Narrate continuously: a `PLAN: <step1> | <step2> | …` line up front, then a single-line
> `PROGRESS: [<n>/<total>] <present-tense action>` before every relevant action (and `… done` on
> completion), spaced so no gap exceeds ~2 min, stdout unbuffered, plus exactly one final
> `RESULT: DONE|BLOCKED <reason>`.

## C. Orchestrator only

> **If you are the implementer reading this work order as your own specification: STOP at this line.**
> The remainder belongs to the Orchestrator. Do not execute the reviews, acceptance, register edits, commits or dispatches below.

### Verification and evidence

Use the configured roles and affected scopes. `WFT-CI-54-T1` uses the `app-ci-e2e` test scope. T1 verifies the selection contract, including real failure and isolated negative cases. Prove workflow resolution on both candidate refs with a previously affected consumer. Record actual selected files, independent existing product failures, and zero default uploads.

The T protocol requires the active WS-GOV-135 runner. Validate it through `acceptance.py` before implementation dispatch and record the result. Validator evidence does not run application tests.

### Coordination with WS-GOV-136 and WFT-CI-55

WS-GOV-135 is the shared test-evidence prerequisite. WS-GOV-136 may proceed in parallel after it lands. Controller-only specs remain in isolated acceptance. The regular CI proof covers selection and failure behaviour on the workflow and consumer refs. It does not call the LLM judge or exchange its captures and verdicts.

WFT-CI-54 and WFT-CI-55 share `app-ci.yml` and E2E tests. Use one owner for those paths at a time. Apply the second change to the first landed revision and repeat its candidate proof on that combined base.

Run the smallest relevant fresh CI proof after the published WFT-CI-53 helper is resolved. Keep default artifact uploads disabled and record run IDs, checked revision, failures by cause, and the review outcome. No production promotion is part of this work order.

## Gate record

Gate #1 approved by the operator on 2026-11-24 for the documented scope after WS-GOV-135 landed (chat response to commission WFT-CI-54 in a Terra 6.1 Medium session).

## Completion

<!-- acceptance:begin -->
Acceptance (tests, )

| ID | Type | Status | Run time | Evidence | Reason |
|---|---|---|---|---|---|
| WFT-CI-54-T1 | T | pass | 2026-11-24 15:09 | .claude/scratch/acceptance-WFT-CI-54 @6a93523+dd60d55b; scope app-ci-e2e, 111 tests |  |
<!-- acceptance:end -->

Implementation: codex/gpt-6.1-terra, high. Candidate workflow commit a820d73; Spesix caller candidate b5edd51 on develop 2c7f2f4 points only to ci-test/WFT-CI-54. WFT-CI-53 helper pin 999c339 is present.

Scope: tests.py app-ci-e2e passed 111 tests, 0 failures, 2 skipped; T1 passed. Review round 1: envelope, regression, tests and sec_review codex/gpt-6.1-terra; duplication codex/gpt-6-luna; 0 findings. Part B: accurate.

CI evidence remains open: Spesix run 45401709341 was in progress when checked. The candidate caller is changed workflow code, requiring operator dispatch; verify selected files, failures and default uploads before landing. After landing, Spesix and Photogallery may use workflow-templates main at the landing commit; until then a820d73 is candidate only.

Foreign paths: workflow-templates staging-health.yml, test_staging_health_probe.py and WFT-CI-44.md (WFT-CI-44), scratchpad/ (WFT-CI-45); Spesix receipt-upload-drafts.spec.js and backend/media/ (owner unknown).

CI proof: Spesix run 45411285342 on caller b5edd51 and workflow candidate a820d73 resolved on netcup-ci. The log records reseeding for 34 regular files, from account-lifecycle.spec.js through workflow-multi-stage.spec.js, with no test-management spec selected; see the run's reseeding notices for the complete file list. The genuine responsive-corrections.spec.js desktop EUR layout failure remained visible and made E2E red. Quarantine step succeeded. Backend, security, image-build and validation passed. The independent Spesix frontend MobilePwaPage.test.jsx German receipt-review test failed; capture reported non-blocking mechanical failures. No artifact was uploaded (GitHub API total_count 0); both upload steps skipped. These app failures require Spesix follow-up and do not cover template selection logic.
