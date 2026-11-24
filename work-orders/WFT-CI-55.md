# WFT-CI-55 — Report safe CI capture failure categories without artifact storage

## A. Envelope

**Target repository** `workflow-templates`

**Tier 3.** Shared CI diagnostics and data exposure.

### Evidence

Survey Contact `45372488879`, Musiknoten `45373603179` and Innoservice `45381311616` identify a red Capture step without the underlying failure class in the job log. Spesix `45381646412` and Photogallery `45387318845` have red non-blocking capture. Cockpit `45185612586` reports a build-marker failure without distinguishing an absent marker from a mismatch. WFT-CI-52 already records bounded status per cell with artifacts disabled by default.

### Goal and outcome

A red capture gives the operator a bounded cell identifier, viewport, status and a recognised mechanical failure class where the report supplies sufficient evidence. A missing report, outdated report or unmapped finding has an explicit safe fallback. A failed non-blocking capture remains clearly visible. Raw report entries, request details, screenshots and browser traces are absent from the default summary and job log.

### Scope and boundaries

The existing CI capture summary and its regression tests. Reuse current cell and freshness checks. Accept only fixed known categories from capture evidence and reject arbitrary report values. Preserve current gate semantics, runner labels, per-cell reseeding and WFT-CI-52's default-off artifact policy. App capture code and product remediation are outside this order.

### Existing capabilities

WFT-CI-52 validates cell IDs and widths, checks report freshness per invocation and writes compact status to the job summary. Template test suites already exercise green, red and missing capture reports.

### Risks

Raw failure strings can carry local paths, identifiers, credentials or request data. An unbounded or misattributed category can disclose data or send app follow-ups to the wrong cause. Treat old and malformed reports as unavailable.

### Required tests

- A known failure yields a fixed category and the correct cell and width. A non-blocking red cell remains visible while the job policy stays unchanged.
- Unknown strings, excessive inputs, malformed and stale reports yield bounded safe summaries. Sentinel credentials, arbitrary URLs, query values and raw report text cannot appear even when a recognised category is present.
- Default green and red runs still upload zero artifacts. Opt-in behaviour and existing red/green gate assertions stay covered.

## B. Implementation map

- `.github/workflows/app-ci.yml` — `Capture E2E screens and report`, the summary helper, report freshness and the existing upload-policy conditions.
- `.github/scripts/test_app_ci_e2e_reporting.py`, `.github/scripts/test_app_ci_e2e_job.py` and `project.yaml` — reporting contracts and `app-ci-e2e` scope.
- `work-orders/WFT-CI-52.md` and the affected apps' capture manifests and report structures — existing privacy and evidence contracts.

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

`WFT-CI-55-T1` is the existing `app-ci-e2e` scope with the new bounded summary tests. The T protocol requires the active WS-GOV-135 runner. Validate it through `acceptance.py` before implementation dispatch.

### Coordination with WS-GOV-136 and WFT-CI-54

WS-GOV-135 is the shared test-evidence prerequisite. WS-GOV-136 may proceed in parallel after it lands. The CI report exposes only its allowlisted mechanical capture categories. LLM judgement, images, free-text reasons and WS-GOV-133 run history stay in workspace acceptance. Keep default uploads off and retain the existing two-ref workflow proof.

WFT-CI-54 and WFT-CI-55 share `app-ci.yml` and E2E tests. Use one owner for those paths at a time. Apply the second change to the first landed revision and repeat its candidate proof on that combined base.

Prove candidate resolution on both CI refs. Record one synthetic red response that shows a fixed cause class and no sentinel data. Check both gate policies with targeted evidence, reusing local structural tests where sufficient. Verify GitHub reports zero default uploads and retain the app follow-ups for actual product failures.

## Gate record

Gate #1 pending operator handoff.
