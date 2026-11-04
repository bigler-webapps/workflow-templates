# WFT-CI-47 — Release producer failures name their stage

# A. Envelope

## Goal and expected outcome

The first live run of the release-contract producer (`publish-backend-image` with `release_contract: v1`, survey_app run 41772046675 on a self-hosted runner) failed after about 3.5 seconds with the single fixed code `release_production_failed`, before any image build could have run. The producer suppresses every child diagnostic by design, so nothing says which step failed. Outcome: a failed run states WHICH fixed stage failed and, for a failed child command, a fixed non-secret outcome class, so the cause can be found without weakening the secret-suppression guarantees.

- The stderr line stays `release-build failed: <code>` for every existing code. For `release_production_failed` it gains a fixed suffix ` stage=<stage>` and, when a child command failed, ` detail=<detail>`.
- `<stage>` is one of exactly: `build_control`, `docker_login`, `docker_build`, `docker_push`, `docker_inspect`, `manifest`.
- `<detail>` is one of exactly: `exit_<n>` (the child's integer exit code, 0 to 255), `timeout`, `spawn_failed`, `output_overflow`, `reader_failed`.
- Nothing else is ever printed: no child stdout or stderr, no argument, path, image name, token or exception text.

## Scope

- `.github/scripts/release_build.py` and `.github/scripts/test_release_build_action.py`; a short note in `docs/release-deployment-contract.md` (diagnostics section).

## Non-goals / do not touch

- No change to what the producer builds, pushes or emits (manifest, image tag, outputs), to its credential handling, or to any workflow or composite action file.
- No child output is added to any log, artifact or message, in any form.
- No tag or release of workflow-templates is created by this change.

## Risks

- A diagnostic that leaks: the added text is built only from the two whitelisted vocabularies above; a test must fail if any other text can appear.
- Existing assertions on the exact stderr line must be updated, not loosened.

## Required tests to write (scoped)

1. Each of the six stages, when its command (or step) fails, yields `release_production_failed` with exactly that stage and, for command failures, the right detail (`exit_<n>` for a nonzero exit, `timeout`, `spawn_failed`).
2. The stderr line for a failure never contains a fake token or any child output used in the fixture, whatever the stage.
3. Success path and every existing non-`release_production_failed` code are unchanged.

## Tier

Tier 3 — CI release producer that runs with a package-write token on a privileged runner.

# B. Implementation map — ADDRESSED TO THE IMPLEMENTER

## Package: stage-named release producer failures - current assignment, 2026-11-04

Target working directory: C:/Users/Micha Bigler/Documents/webapps/workflow-templates, branch main. Native inherits the workspace root; use explicit target paths. You are not alone: other sessions have uncommitted work in this checkout (`.github/workflows/staging-health.yml`, `work-orders/WFT-CI-44.md`, `.github/scripts/test_staging_health_probe.py`, `scratchpad/`); preserve it and touch ONLY your own files.

**Own ONLY** `.github/scripts/release_build.py`, `.github/scripts/test_release_build_action.py`, `docs/release-deployment-contract.md` (one short paragraph). Nothing else.

**Seams (verified).** `release_build.py`: `_run(argv, *, env, timeout, capture, input_bytes)` raises `ReleaseBuildError("command_failed")` for a spawn error or timeout (the `except (OSError, subprocess.TimeoutExpired)` branch), for a nonzero exit (`process.returncode`), for a reader failure and `registry_metadata_too_large` for output overflow; `produce()` runs build-control preparation (`_prepare_build_control`), `docker login`, `docker build`, `docker push`, the registry inspect inside `resolve()` and `produce_image_manifest`, all inside ONE `try` that ends with `except Exception: raise ReleaseBuildError("release_production_failed") from None`, which discards which step failed; `main()` prints `release-build failed: <code>`. Existing tests that pin the exact line or code: `test_command_failure_suppresses_raw_error_and_cleans_private_outputs`, `test_bounded_adapter_real_small_processes_and_sanitized_stderr` (expects `command_failed`), `test_main_diagnostics_and_artifacts_never_echo_command_secrets` (asserts the exact stderr line), `test_actual_head_mismatch_and_build_failure_never_push`, `test_credential_cleanup_failure_cannot_return_success`.

**Required behaviour.** (1) `ReleaseBuildError` gets two optional, whitelist-validated attributes `stage` and `detail` (default `None`); constructing one with a value outside the vocabularies in Part A is refused (raise a plain `ValueError`, never echo the value). `_run` sets `detail` on the `command_failed` it raises: `exit_<n>` for a nonzero exit code, `timeout` for `TimeoutExpired`, `spawn_failed` for `OSError`, `reader_failed` for a reader failure, and `output_overflow` for the overflow case; the exit code is the only number ever printed and must be an int in 0..255 (otherwise omit it). (2) In `produce()`, record the current stage in a local variable set immediately before each of: build-control preparation (`build_control`), `docker login` (`docker_login`), `docker build` (`docker_build`), `docker push` (`docker_push`), the registry inspect (`docker_inspect`, set inside `resolve`), the manifest production and file writes (`manifest`); when the generic `except Exception` fires, raise `ReleaseBuildError("release_production_failed", stage=<current stage>, detail=<detail of the underlying ReleaseBuildError when it carried one, else None>)`; keep `from None`. The exception message (`str(exc)`) must stay exactly the code so existing consumers are unchanged. (3) `main()` prints `release-build failed: <code>` and, only when the error is `release_production_failed` with a stage, appends ` stage=<stage>` and, only when a detail exists, ` detail=<detail>`; every other code keeps the exact current line. (4) Update the tests that pin the old exact line for `release_production_failed`; add the Part A tests; add a test that feeds a fake token and a fake child output through every stage and asserts neither appears in stderr; add a test that constructing an error with an out-of-vocabulary stage or detail is refused. Do not change any behaviour besides the diagnostics.

**Invariants.** No child stdout or stderr is ever read into a message. Credentials, success output, manifest, tags and the cleanup behaviour are unchanged. Use explicit UTF-8 file I/O. Run only your own tests: `python -I -m pytest -q .github/scripts/test_release_build_action.py` (if pytest is missing, say so and give the command).

## Preamble — binding on the implementation instance

> The text above is the COMPLETE spec: this WO is not a plan to refine. Read the nearest AGENTS.md and CLAUDE.md, their mandatory named references, the repo WORK_ORDERS.md (open WOs and highest ID), and the relevant role skill for applicable conventions and mandatory instructions. These context reads are required; they grant no register-edit or scope-expansion authority. Work from this context package; open the named files to verify it, without broad exploration. If an essential seam is missing or changed, report RESULT: BLOCKED with the missing context.
>
> Edit only the explicitly assigned implementation files for the dispatched batch: source, tests and required documentation. You are not alone in this checkout: preserve others' edits and stop on overlapping foreign changes. Use explicit UTF-8 file I/O. Do not edit MEMORY.md or WORK_ORDERS.md; register updates and review verdicts belong to the Orchestrator. Do not change effective governance.
>
> Perform only the read-only context reads expressly required above. Beyond those reads, tools are for editing those assigned implementation files and running the tests you wrote, once; nothing else. Git orientation and all repository operations belong to the Orchestrator. No dependency installation, package-manager commands, lockfile changes, repository administration, server access, provisioning, workflow dispatch, secret delivery, cleanup or git add/commit/push. Report blocked repo state instead of repairing it. Never read or modify .env or private SSH configuration, print credentials, or perform destructive actions.
>
> Write Part A's tests for this batch and run only your own new tests once. Do not run the affected/full suite, self-review, launch reviewers or claim a review verdict. Your run is not a gate; the Orchestrator owns independent review and the authoritative scoped run. Leave all edits uncommitted.

## Progress contract

Start with PLAN: <bounded steps>. Before each relevant action emit PROGRESS: [n/total] <present-tense action>, then report completion; use unbuffered output and leave no narration gap over two minutes. Finish with exactly one RESULT: DONE|BLOCKED <reason>, listing changed paths, own test command/result and unresolved acceptance evidence. DONE describes the assigned implementation batch only; it never asserts a release, a live run or programme closure.

# C. Orchestrator only — NOT ADDRESSED TO THE IMPLEMENTER

> Implementer: STOP here. Integration, verification, reviews and git belong to the Orchestrator.

- Origin: operator decision 2026-11-04 in the operations session ("Ja, beheben") after survey_app run 41772046675 failed with `release_production_failed`; the diagnostics are phase 1, the root-cause fix (which needs the stage this change exposes) is phase 2 under this same WO or a follow-up row.
- After landing: bump survey_app `release-build.yml` to the new commit (SVA-DEP-8 follow-up), push `develop`, read the stage from the next run (one `gh` query), then fix the cause.
- Report to the Infra Expertenchat on landing.

## Source Gate #2 - release producer failures name their stage, 2026-11-04

Source: `.github/scripts/release_build.py` now records the current stage and a fixed outcome class and prints `release-build failed: release_production_failed stage=<stage> detail=<detail>` (stage from `build_control`, `docker_login`, `docker_build`, `docker_push`, `docker_inspect`, `manifest`; detail from `exit_<0-255>`, `timeout`, `spawn_failed`, `output_overflow`, `reader_failed`). Both come from closed vocabularies enforced in the `ReleaseBuildError` constructor (a plain `ValueError` that never echoes the value); `str(exc)` and every other code's line are unchanged; no child output is ever read into a message. `_run` classifies a failed child (exit code, timeout, spawn failure, reader failure, overflow); an I/O error on a RUNNING child has no class. The inspect-stage detail survives the manifest wrapper (`produce_image_manifest` converts resolver exceptions into its own error type) through a captured value. Docs: `docs/release-deployment-contract.md` gained a short diagnostics paragraph. Nothing the producer builds, pushes or emits changed.

Implementation: the Orchestrator itself (Codex implementer unavailable: usage limit, first-hand 2026-11-04, `.claude/codex-status.md`; the Envelope was authored by the Orchestrator on the operator's instruction "Ja, beheben"). Scoped gate on the pinned WSL controller: `.github/scripts/test_release_build_action.py` 22 passed, 63 subtests passed, no skips.

review: claude/sonnet (fallback, fresh instances, read-only) · lenses: envelope, regression, duplication, tests, security · 8 raised · 8 accepted · worst accepted: defect · fallback · carried: 1. Round one: one tests defect (the leak test covered only four stages and never went through `_run` or a detail-carrying error) and nits, all fixed: every one of the six stages now goes through the real `main()` with exact stage and detail and a fake token, `reader_failed` and the broken-pipe case run through `_run` with a fake process, the manifest stage and a detail-carrying build-control error are pinned exactly, the stage is set before the docker build arguments are assembled, and a broken pipe on a running child is no longer labelled `spawn_failed`. Carried: a rejected inspect output (oversize, wrong identity) is attributed to `docker_inspect`, because the stage moves to `manifest` only after the output parsed. Closing round: all five lenses No findings.

Not done and still open: landing is source only; the root cause of survey_app run 41772046675 is unknown until a run with this producer shows its stage. Next steps (not this change): bump the producer pin in survey_app `release-build.yml` (SVA-DEP-8 follow-up), push `develop`, read the stage, then fix the cause.
