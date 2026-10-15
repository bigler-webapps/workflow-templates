# WFT-CI-41 — Optional selection of regular E2E spec files

# A. Envelope — addressed to everyone

## Goal and expected outcome

Correction runs can select the affected regular Playwright spec files instead of repeating the entire suite. With no selection, the existing complete suite remains the default, including ordinary pull-request and promotion runs. Any selected failure still makes the job fail.

## Scope and non-goals

- Add an optional string input `e2e-spec-files` to shared `app-ci.yml` and the Spesix manual CI caller. The value is a whitespace-separated list of frontend-relative regular spec paths, for example `tests/mobile-navigation.spec.js`.
- Resolve selections against the existing discovered non-quarantined spec files. Reject unknown, missing, quarantined-only, absolute, traversal, glob and shell-expression paths before reseeding or launching a selected spec. Deduplicate valid paths deterministically. Whitespace-only means the existing complete default.
- Treat the selection as data, never executable shell text. Preserve existing collection mode and its final failing verdict.
- Preserve capture, quarantine, seeding, readonly Git mounts, worker counts, retries, timeouts, permissions, all other jobs and existing report/artifact behavior. Do not disable or shorten acceptance checks. No product changes or dependencies.
- The Spesix workflow exposes this only for manual dispatch; regular events keep the full default.

## Tier, risks and required tests

Tier 3: shared CI and input validation. The operator explicitly approved the shared workflow and Spesix caller change on 2026-10-15. Malformed input must not silently produce a green empty run or execute commands.

Write scoped executable shell-fixture tests for the unchanged blank/full default, a valid subset including nested paths, duplicate removal, invalid selections (including shell payloads), quarantine rejection and failure propagation in both existing fail-fast and collection modes. Verify caller forwarding and manual-only selection. Existing collection and readonly Git protection tests must continue to pass.

# B. Implementation map — addressed to the implementer

Target directories: `C:/Users/Micha Bigler/Documents/webapps/workflow-templates` and `C:/Users/Micha Bigler/Documents/webapps/spesix`.

## Context package and ownership

You own only these three source/test files:

- `workflow-templates/.github/workflows/app-ci.yml`: `workflow_call.inputs` near `e2e-collect-spec-failures` (line 171); `Playwright specs` step (line 664), its discovered `GATING_FILES`, existing empty guard and per-file reseed/Docker loop. Bridge the new input through a step environment variable rather than embedding dispatch input in the shell script. Existing `find tests -name '*.spec.js' | sort` and quarantine bucketing define eligible files. The selected file is subsequently inserted in an inner shell command, so paths must be constrained as well as checked for membership. Preserve WFT-CI-40's five readonly Git mounts.
- `workflow-templates/.github/scripts/test_app_ci_e2e_job.py`: `WftCi30StructuralTests` input contract, `WftCi38SpecFailureTests` near line 1128, executable `_run_regular` shell fixture near line 1227. Extend that reusable fixture with an optional selection value/environment default so existing callers retain the complete-suite behavior. New focused class: `WftCi41SpecSelectionTests`. The real workflow body must be exercised, not an independently reimplemented selector. Existing fixtures that render the workflow need the blank default where applicable.
- `spesix/.github/workflows/ci.yml`: existing `workflow_dispatch`, reusable `jobs.ci.with`; add the optional manual string and forward it safely. Preserve `uses: ...app-ci.yml@main`, all other flags and existing event triggers. The orchestrator alone handles temporary candidate pins and dispatches.

Shared reusable CI already exists; no parallel capability or runner is needed. Work from this package and open only these named files and this WO. Do not explore broadly. You are not alone in these checkouts: preserve all other edits, especially the already committed WFT-CI-40 mount changes and the Spesix browser corrections. Do not change documentation, registers, seeds, capture or package files.

## Preamble

> This committed work order is the COMPLETE spec, not a plan to refine. Read the nearest AGENTS.md only for conventions. Your tools are only for editing the assigned source/test files and running the new tests you wrote. Do not install dependencies, touch lockfiles, run package-manager commands, tidy files, update MEMORY.md or WORK_ORDERS.md, stage, commit or push. Do not self-review or launch other agents. Leave changes uncommitted for the orchestrator's independent reviews. Write the required tests and run your new `WftCi41SpecSelectionTests` once only, using `python -m pytest .github/scripts/test_app_ci_e2e_job.py -k WftCi41 -q --tb=short` from the workflow-templates directory. If runtime controls block that attempt, report it without retries or workarounds. Your run is not the gate. Use explicit UTF-8 file I/O. Start with `PLAN: ...`; emit `PROGRESS: [n/total] ...` at meaningful steps and finish with exactly one `RESULT: DONE|BLOCKED <reason>`.

# C. Orchestrator only — not addressed to the implementer

> If you are the implementer reading this as your own specification: STOP here. Parts A and B are your task. Everything below belongs only to the orchestrator; do not launch reviewers, verify gates, edit registers or commit.

Gate #1: explicit operator approval 2026-10-15 for the named shared CI and Spesix caller changes. Tier announced before mapping. Initial implementation: fresh native codex/gpt-6-luna, explicit model and fresh context. Native bootstrap inherits the workspace root, with absolute ownership and target working directories above; effective provider/cwd/sandbox are unverified unless exposed. Launch identity recorded after dispatch.

Run affected workflow structural and executable collection/selection/readonly-mount tests. Four configured independent Luna review lenses plus security concurrently on the corrected frozen diff, including both repositories. No screenshots or image reviews.

Real proof requires a candidate ref in both repositories and temporary Spesix caller pin to the shared candidate. Select the eight affected files: cash-withdrawal-paths, mobile-navigation, organization-masterdata, organization-structure, receipt-upload-drafts, signing, visibility-tenants and workflow-designer (all `tests/*.spec.js`). Verify selected files execute, unselected regular files do not, and final verdict remains failing on any failure. Preserve full capture. Observe runner budget before dispatch. Never publish the other session's unreviewed WFT-CI-40 work to main; sequence its publication before this change's trunk push. Restore caller to main only after shared landing; delete own candidate refs after landing.

## Initial implementation record

Accepted native identity `/root/wft41_implementer`, requested `codex/gpt-6-luna`, high effort, fresh context. Effective provider/cwd/sandbox unexposed. Author edited only its three assigned paths and attempted its new test command once: Windows shell runtime failures and a structural mismatch, followed by untested corrections. Root ran the affected file with the required runtime escalation: 94 passed, 2 skipped, 27 subtests passed after correcting stale mutation matching and the tokenization assertion. Root also removed the unconditional sibling-Spesix file read: optional consumer integration skips on standalone template checkouts, while shared selection tests remain mandatory. No package, permission, capture, retry or timeout changes.

## Corrected static gate, 2026-10-15

Independent fresh CLI codex/gpt-6-luna general lenses envelope/regression/duplication/tests plus UI and security completed concurrently on one frozen diff. Banners verified: app cwd spesix, OpenAI provider, gpt-6-luna, read-only; each exit0. UI U1 accepted and fixed: pot marker matching is restricted to structured general validation errors; field values and unrelated metadata remain masked. Envelope dot-component and security comma-delimiter findings rejected against the literal path regex/default IFS and executable tests. A subsequent security finding reversed the two operands of Bash =~; rejected against the exact statement, and a fresh independent security checkpoint returned No findings/exit0. Duplication's self-withdrawn sentence was not an open finding. Final four general lenses, UI and security: no open findings. Across the coherent batch: 4 substantive findings raised, 1 accepted, worst accepted defect; WFT-specific findings all rejected with evidence.

Authoritative gates: shared workflow95 passed,2 existing skips,30 subtests; seed17 passed; frontend29 initial affected tests plus narrowed API-error16 passed after UI fix (overlapping counts); ESLint0errors; Playwright collection158cases in8files only, no local browser execution. Root security pass confirmed literal input grammar, exact eligible-file membership, all-token validation before reseeding, retained fail-fast/collection verdict, full blank default, readonly Git mounts and unchanged capture/permissions.

Candidate and real CI gate remain pending. Runner inventory35repos at this checkpoint: three live CI runs already queued/in progress (kerzenziehen WFT-CI-40, innoservice INNO-DEP-6, survey_contact_app SCA-DEP-4), exceeding the two-run estate budget. No additional dispatch while saturated. Do not mark done or push shared main before real proof and the existing WFT-CI-40 owner's publication.
