# WFT-CI-38 — Optional collection of all regular E2E spec failures

# A. Envelope — operator-approved scope

## Goal and outcome

Innoservice currently discovers failures one spec file per CI run because the
shared regular-spec loop exits immediately. An optional collection mode runs all
regular files, records each failure and still fails the job when any file fails.

## Scope and constraints

- Tier3: shared CI. Add boolean `e2e-collect-spec-failures`, default false.
- Enabled mode continues after a Playwright spec failure; before each subsequent
  file the existing reseed must succeed. Discovery/empty-file and reseed failures
  remain fatal: never execute a spec against stale database state.
- Every regular invocation in enabled mode has a distinct output directory that
  cannot collide with another spec or quarantine output. Earlier failure traces
  and error contexts survive later invocations and the existing artifact upload.
- Default false retains existing fail-fast behavior. Other apps do not opt in.
- Preserve file discovery/classification, per-file desktop/mobile runs, tests,
  assertions, timeouts, seed controls, quarantine behavior, capture and uploads.
- Do not weaken or quarantine tests, alter product/auth/schema/secrets/dependencies,
  triggers, runners or deployment. WFT-CI-37 capture wiring remains unchanged.
- Risk: a failed enabled run lasts longer, but reports the whole regular-spec set.
  No screenshots or image judging are needed for this CI-only change.

## Required tests to write

Scoped workflow contract and safe stub-only shell cases: false retains first-fail
exit; true runs later files after a failure but remains nonzero; all-pass is zero;
reseed failure is fatal without running stale cases; empty regular set is fatal;
distinct potentially colliding filenames preserve both output markers, including
when quarantine follows. Pin opt-in/default semantics and artifact retention.
Require paired real app/shared candidate evidence: green and deliberately red,
with retained failed-spec artifacts and later spec execution on the red run.

# B. Implementation map — ADDRESSED TO THE IMPLEMENTER

Absolute target: C:/Users/Micha Bigler/Documents/webapps/workflow-templates.
Native workspace-root bootstrap; shell/file work uses this absolute repo target.
Own only `.github/workflows/app-ci.yml` and
`.github/scripts/test_app_ci_e2e_job.py`.

- app-ci workflow_call input section and validate-inputs (existing run-e2e guards).
  Add the named input and reject enabled collection without run-e2e. Default false
  preserves every existing caller. This is separate from e2e-capture-gate.
- `Playwright specs` step, currently647-687: set-euo, regular files discovered
  under tests and classified by the existing literal @quarantine convention;
  reseed before each file; a failed docker/Playwright invocation writes error and
  summary then exits. Extend only this opt-in behavior; keep reseed fatal.
- `Playwright quarantine specs` currently692-756 is READ ONLY. It already gives
  each invocation an index-prefixed output subdirectory because Playwright clears
  its output directory at the start. Reuse that existing concept, ensuring regular
  and quarantine paths cannot collide. Do not change quarantine classification.
- Existing always-upload failure artifact reads frontend-path/test-results;
  keep artifact path/name, failure condition, capture and cleanup ordering.
- Test file has live-source helpers step_body, shell_body, load_e2e_steps/find_step
  and WftCi37CaptureGateTests with safe stub Docker and CAPTURE_BASH portability.
  Add WftCi38SpecFailureTests, reusing these conventions. Preserve all existing
  assertions and the two unrelated Linux-only mail skips. Do not silently skip
  new shell cases on Windows: use the existing safe Git Bash handling/stub PATH.

No broader exploration. Read the two named files and work from these seams.

> This WO is the COMPLETE spec. You are not alone; preserve other changes and
> use explicit UTF-8. Tools only to read assigned context, edit these two source/
> test files, and run your new class once with python -m pytest
> .github/scripts/test_app_ci_e2e_job.py -k WftCi38SpecFailureTests -q.
> No real Docker, app or browser execution: tests use only fixture stubs.
> No package managers/installs, git/add/commit/push, lockfiles, register/memory/
> governance, agents, self-review, environment/settings/secrets/.env reads or
> unrelated edits. Leave uncommitted. PLAN first, PROGRESS before actions and
> every two minutes; finish exactly RESULT: DONE or BLOCKED with concrete cause.
> Parent independent review and authoritative tests are the gate.

# C. Orchestrator only — NOT ADDRESSED TO THE IMPLEMENTER

> IMPLEMENTER STOP HERE. Nothing below is your work.

Gate1 explicitly approved2026-10-13 in this chat, reply to call95858712: collect
all regular Innoservice specs, retain reseeding/isolated artifacts/failure exit,
default off for other apps, Luna initial implementation, reviews and green/red
proofs. Duplicate reply is the same approval, not a second scope expansion.

Fresh native plain Luna/high/fork_turns=none; complete WO and absolute two-file
ownership. Record accepted ID/requested model; do not infer effective metadata
or child containment. Parent fixes findings, never redispatches initial author.
Four independent configured source-review lenses concurrent, read-only CLI,
static-only context, no tests/app/images. Parent affected-file test run plus safe
shell scenarios; no real Docker locally and no full suite. Review assembled CI37/
38 behavior where these share a candidate; prior CI37 contracts remain binding.

Pair own shared ci-test/WFT-CI-38 and app candidate using INNO-CI-2. Parent owns
only temporary candidate references/faults and restores them before landing. A
controlled regular-spec failure must leave later execution and retained traces,
with job red; corrected source must be green. CI37 also still requires its own
capture-green/capture-red/artifact proof before the combined shared main lands;
a diagnostic candidate input is not permanent INNO-E2E-6 implementation. Do not
mark CI37 or E2E6 done from this opt-in. Respect queue budget and branch rules.
Final shared main and app develop references require the actual reviewed/tested
source; no production promotion. Record tests/reviews/run/artifact, then automatic
own-path commit/landing-SHA register update/push on green.

## Source checkpoint 2026-10-14

Initial native author /root/spec_failure_collection, requested codex/gpt-6-luna/high, fresh context;
effective native metadata unexposed. Workspace-root bootstrap, target repo per B.
Parent integrated fixture corrections and restored unchanged quarantine source.
Author test attempt was blocked by temporary-path permissions, not model unavailability;
parent authoritative affected-file run: 87 passed, 2 pre-existing Linux-only mail skips,
13 subtests passed, zero failures/errors. Four concurrent independent readonly
Luna lenses plus envelope/tests clarifications: 5 raised, 0 accepted, no open findings.
See [source evidence](assets/WFT-CI-38-candidate-source-review.json) for dispositions.
Paired real green/red CI and retained artifacts remain required; no done claim.
Temporary shared ref and capture proof probe are evidence-only and restored before
prerequisite landing; permanent E2E6 still requires its own fresh author.

## Final gates and automatic publication

Final shared gate2026-10-14: explicit initial Luna authors retained; final four concurrent independent codex/gpt-6-luna/medium envelope/regression/duplication/tests all clean,0open. Unchanged affected file87passed2pre-existing Linux skips13subtests7.88s. Positive38601444526 allfive jobs/capture74green; regular-negative38610424378 only sentinel fails,23reseeds/22later files89cases pass/early artifacts retained; capture-negative38617202940 regulargreen,first cell2widths fail,72later records/artifact retained. [Final source/real-proof evidence](assets/WFT-CI-37-38-final-proof.json). App temporary faults removed locally; shared main publication automatic on these gates. Permanent app capture opt-in still follows FIX11/12/13 landing.
