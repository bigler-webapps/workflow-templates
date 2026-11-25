# WFT-CI-56 — A caller can switch the frontend job off

## A. Envelope

**Target repository** `workflow-templates`

**Tier 3.** Shared CI. Part of the package led by WS-GOV-155 (workspace root, selective CI dispatch); PG-CI-16 is the pilot caller.

### Evidence

In `app-ci.yml` the `frontend` job (~593) has no job-level condition: it runs on every call, and only its lint and test steps are switchable (`run-frontend-lint`, `run-frontend-tests`). A dispatch meant to prove a backend or E2E change therefore always pays for the frontend job too. No job has `needs:`, so the jobs are independent.

### Goal and outcome

- New input `run-frontend` (boolean, default `true`) gates the `frontend` job at job level. Existing callers that do not pass it behave exactly as today.
- `validate-inputs` fails with a clear message when a call switches every job off (`run-backend`, `run-frontend`, `run-e2e` and `run-security` all false), instead of a green run that tested nothing.
- The input descriptions of `e2e-spec-files` and `pytest-args` state the accepted value forms that the workspace dispatch hook will mirror (WS-GOV-155): whitespace-separated `tests/<path>.spec.js` paths; pytest node ids or one `-k` expression.

### Scope and boundaries

`app-ci.yml` inputs, the `frontend` job condition, `validate-inputs`, and the structural tests of the workflow. Out of scope: other jobs' logic, spec selection rules (WFT-CI-54), pytest-args parsing (WFT-CI-53), artifact policy, every caller (PG-CI-16 is the pilot; other apps take the inputs in their next CI WO).

### Existing capabilities

`validate-inputs` already fails loudly on contradictory inputs (`run-migration-check` without `run-backend`, `run-e2e` without its settings); the new check follows that pattern. Selection inputs already exist: `e2e-spec-files` (~185) and `pytest-args` (~39).

### Risks

A default other than `true`, or a condition that misreads an unset input, would silently drop the frontend job in all callers. Callers pinned to `@main` pick up the change at once.

### Required tests

- Structural test: the `frontend` job carries the `run-frontend` condition; the default is `true`.
- `validate-inputs`: all jobs off fails with the message; any one job on passes.

## B. Implementation map

- Shared workflow: .github/workflows/app-ci.yml (inputs ~39-185, `validate-inputs` ~209-238, `frontend` job ~593).
- Structural tests of the workflow: the existing test files under .github/scripts/ and tests/ that assert on `app-ci.yml` (locate with Grep for `app-ci.yml`).
- Reference only: the workspace dispatch hook in WS-GOV-155 mirrors the value forms named above.

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

> **If you are the implementer reading this work order as your own specification: STOP at this line.
> Everything below describes what the Orchestrator does AFTER you finish. You do none of it — no
> reviewers, no verification run, no register edit, no commit.** You ARE the implementation
> instance described below; do NOT launch or dispatch another implementer.

Package WS-GOV-155: run by the WS-GOV-155 Orchestrator session. Prove the candidate on `ci-test/WFT-CI-56` with PG-CI-16's caller pointing at `app-ci.yml@ci-test/WFT-CI-56` (two-sided proof, AGENTS.md exception (b)): one dispatch with all jobs, one with `run-frontend` off; record run IDs. Review: general lenses plus sec_review. Land on `main` before PG-CI-16. Delete own `ci-test/WFT-CI-56` refs after landing.

## Gate record

Gate #1 approved by the operator 2026-11-26 (Expertenchat chat, AskUserQuestion answer "Freigeben, Übergabetext ausgeben" for WS-GOV-153, WS-GOV-154 and the package WS-GOV-155 with WFT-CI-56 and PG-CI-16).
