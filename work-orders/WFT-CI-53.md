# WFT-CI-53 — Empty pytest-args must not override the app's pytest collection

## A. Envelope

### Goal and outcome

The shared backend pytest step in `app-ci.yml` collects exactly what the app's own pytest configuration declares when the app passes no `pytest-args`. The collection errors seen in six apps on 2026-11-24 disappear without any app-side change.

Evidence: run [bigler-consult 45373369250](https://github.com/bigler-webapps/bigler-consult/actions/runs/45373369250) fails with `3 errors during collection`. The three modules (`backend/test_management_urls.py`, `consulting/test_management.py`, `consulting/test_test_management.py`) are not in the app's `pytest.ini` `testpaths`. They import `django_core_micha.test_management` and need the app's separate test-management settings. The same collection failure is reported for survey_contact_app, musiknoten, webshop-guenter, reimbursements and Gustav.

Cause, derived from the code and to be confirmed by the first test: `ci_test_workers.py pytest-args` writes one NUL byte for an empty argument list (`"\0".join([]) + b"\0"`). `mapfile -d ''` in the "Run pytest" step reads that as one empty argument. pytest then receives `""` as an explicit path, which replaces `testpaths` and collects the whole working directory. The helper came with WM-INF-101 (9ce4476, v2.17.0, 2026-11-02). The existing helper test strips the trailing NUL and never checks an empty input.

### Scope

- With an empty or whitespace-only `pytest-args`, pytest receives no positional argument, and the app's `testpaths` and `addopts` apply unchanged.
- Non-empty `pytest-args` keep today's behaviour, including the xdist clamping and the rule that the raw input never reaches the script text.
- The app-ci checkout of the helper uses the corrected commit.

**Excluded.** The other failures from the same runs: survey_app's manifest-link test and missing isolated-compose mount, the capture exit 1 in survey_contact_app and musiknoten, kira's ESLint errors and webshop-guenter's Vitest timeout. Also excluded: the E2E steps, the worker caps, app repositories and their pytest configuration, and running the test-management modules in CI.

### Tier

**Tier 3.** Shared CI for every app.

### Existing capabilities

`.github/scripts/ci_test_workers.py` and its test `.github/scripts/test_ci_test_workers.py` own the argument handling. The workflow test in the same file already asserts that pytest runs with the clamped arguments only.

### Risks

- Every app's backend CI passes through this step. A change that drops a real argument, or that lets raw input into the script text, affects all of them at once.
- Some apps may already depend on collecting the whole directory without knowing it. After the fix they run only their declared `testpaths`, which is the intended behaviour, but their test counts can drop.

### Required tests to write

- An empty and a whitespace-only `pytest-args` produce no argument at all, end to end through the same `mapfile -d ''` reading the workflow uses, not through a helper that strips the output.
- Single and multiple arguments, quoted arguments and the existing xdist forms keep their current results.
- The workflow test asserts that the helper checkout pin points at a commit containing the fix, and keeps its existing assertions.

## B. Implementation map

- `.github/scripts/ci_test_workers.py` — `pytest-args` mode output (lines 62-66) and `clamp_pytest_args`.
- `.github/scripts/test_ci_test_workers.py` — helper tests; `pytest_args()` strips the trailing NUL (line 20).
- `.github/workflows/app-ci.yml` — helper checkout pin (line 274), "Run pytest" step with `mapfile -d ''` and `"${pytest_args[@]}"` (lines 311-343), `pytest-args` input default `''` (lines 39-41).
- Consumers — every app `ci.yml` calling `app-ci.yml@main`; for example bigler-consult passes no `pytest-args` and declares `testpaths` in `backend/pytest.ini`.
- Environment — shared self-hosted netcup runner; apps resolve `app-ci.yml@main`.

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
> `git add`/`commit`/`push` — leave every change uncommitted in the working tree for the orchestrator's
> independent review. WRITE the tests the `Required tests` section calls for AND **RUN the tests you
> just wrote** to confirm they execute and pass — that is the ONLY test run you do. The orchestrator
> re-runs the authoritative set and does the independent review.
>
> **Files.** Locate and read only the relevant range in files over roughly 400 lines. Use cohesive
> behaviour-named modules and tests. Preserve changes belonging to other sessions.
>
> Narrate a `PLAN:` line, then `PROGRESS: [<n>/<total>]` before each relevant action. End with one
> `RESULT: DONE|BLOCKED <reason>` and name any local test environment limitation.

## C. Orchestrator only

> **If you are the implementer reading this work order as your own specification: STOP at this line.**
> Everything below describes the Orchestrator's work after implementation. You do none of it. You are
> the implementation instance and do not launch or dispatch another implementer.

### Acceptance

1. The fix lands in two commits, because the pin must name the helper commit: first the helper and its tests, then the pin bump with the workflow test. Prove both on `ci-test/WFT-CI-53` with one consumer pointing at `app-ci.yml@ci-test/WFT-CI-53` (AGENTS.md exception b). Use bigler-consult; its ci-test run must show no collection error and its declared `testpaths` count.
2. After landing on `main`, start one fresh bigler-consult `ci.yml` run on `develop`. A rerun of 45373369250 may reuse the old resolved template and proves nothing. Record the result and the collected count.
3. Record in Completion which of the six apps still fail in the backend job and with what. Those are separate causes outside this WO.

## Gate record

Gate #1 approved by the operator 2026-11-24 (click in session local_1e5f212a-77db-4423-a507-983adf826bc4).
