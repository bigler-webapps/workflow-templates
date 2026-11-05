# WFT-CI-48 — A PR that changes the Dockerfile builds the full production image in CI

# A. Envelope

## Goal and expected outcome

`app-ci.yml` builds the backend image for the pytest job. Since CI-6, a repository can opt into a pytest-only target (`backend-target: backend_test`), which skips the frontend build stage and collectstatic. cockpit opts in.

On 2026-11-04 the renovate-triage merged cockpit #59 ("update node.js to v26"; it changes only `backend/Dockerfile`) on green CI. Node 26 no longer ships `corepack`, and the skipped `frontend_build` stage runs `corepack enable && corepack prepare pnpm@…`. Every staging deploy after the merge failed (cockpit Deploy App run 41918038268). CI never built that stage, so it was green.

Outcome:

1. **Full build when the Dockerfile changes.** In a pull request whose changed files include the caller's backend Dockerfile (`inputs.dockerfile`) or another `Dockerfile*` in the build context, the backend job builds the full production image, without `--target`, in addition to or instead of the opted-in test target. A failing full build fails the job, so the PR shows red CI.
2. **Evidence the triage can read.** The full build is visible as its own named step or job on the PR head, for example a job or check named `ci / image-build`, so `webapp-management` (WM-RT-49) can require it for Dockerfile changes. The name is fixed in this order and stated in the register row.
3. **Unchanged otherwise.** PRs that do not touch a Dockerfile keep today's opted-in target and build time. Pushes and the deploy path are unchanged. The image is built, not pushed.

## Scope

- `workflow-templates`: `.github/workflows/app-ci.yml` (changed-files detection for the PR, a full-image build step or job, its name), the template's own tests or validation if present.

## Non-goals / do not touch

- No change to the deploy or publish actions, to the e2e job, to pip-audit or to callers' `ci.yml` files.
- No change to the triage (that is WM-RT-49 in webapp-management).

## Precondition and ordering

Lands before WM-RT-49 enforces the check. Callers pin `app-ci.yml@main`, so the change is live estate-wide on merge to `main`. A candidate is proven on a `ci-test/<ID>` ref with one app pointing at it before the merge (AGENTS.md exception b).

## Risks

- **Shared CI, estate-wide.** A defect breaks CI for every app. Mitigated by the ci-test proof on one app with a Dockerfile-changing PR and one without.
- **Runner load.** A full build on a Dockerfile PR costs a few minutes on the shared self-hosted runner, only for those PRs.
- **Secrets in build args.** The full build needs the same build args as the deploy build (for example `VITE_APP_MUI_LICENSE_KEY`). They are passed as today for the test target, never echoed.

## Required tests to write (scoped)

1. On a PR that changes `backend/Dockerfile`, the full image is built (no `--target`) and a failure fails the named check.
2. On a PR that does not change a Dockerfile, the opted-in target build runs as today and the full build is skipped.
3. Both proven on a `ci-test/<ID>` ref with one calling app, recorded in the register row.

## Tier

Tier 3: CI shared by every app.

# B. Implementation map — ADDRESSED TO THE IMPLEMENTER

## Context package

Work from this package; do not explore broadly from scratch; open only the named files to verify.

- **File to change:** `.github/workflows/app-ci.yml` (reusable workflow, `on: workflow_call`). Today it has jobs `backend` (line ~237), security, frontend, e2e. The `backend` job's step `Build backend image` (line ~276-305) runs `DOCKER_BUILDKIT=1 docker build "${target_args[@]}" --build-arg "VITE_APP_MUI_LICENSE_KEY=$VITE_APP_MUI_LICENSE_KEY" --secret id=hram_engine_token,env=HRAM_ENGINE_READ_TOKEN --label org.opencontainers.image.source=... -t "$CI_IMAGE" -f "$BACKEND_DOCKERFILE" "$BACKEND_BUILD_CONTEXT"` with `env:` entries `HRAM_ENGINE_READ_TOKEN`, `VITE_APP_MUI_LICENSE_KEY` (from `secrets.*`), `BACKEND_TARGET`, `BACKEND_DOCKERFILE: ${{ inputs.dockerfile }}`, `BACKEND_BUILD_CONTEXT: ${{ inputs.build-context }}`. Its `CI_IMAGE` is `ci-backend:${{ github.run_id }}-${{ github.run_attempt }}`. Do NOT change the `backend` job.
- **New job `image-build`** (job key exactly `image-build`, NO `name:` key: the check shows as `ci / image-build` when the caller's job id is `ci`; every other job in this file likewise has no `name:`). Place it directly after the `backend` job.
  - `if: ${{ inputs.run-backend && (github.event_name == 'pull_request' || github.event_name == 'workflow_dispatch') }}` (decision of the Orchestrator within the Envelope: the job exists only where the backend job runs; `workflow_dispatch` is included only so the candidate can be proven on a `ci-test` ref, where no pull request exists — see the fail-open rule below).
  - `runs-on: ${{ inputs.runs-on }}`; `env: CI_IMAGE: ci-image-build:${{ github.run_id }}-${{ github.run_attempt }}` (unique per run: the runner's Docker daemon is shared).
  - Steps, in this order:
    1. `actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683  # v4.2.2` with `fetch-depth: 2`. On `pull_request` the checkout is the merge commit; `HEAD^1` is the base tip, so `git diff` of `HEAD^1..HEAD` is exactly the PR's changed files.
    2. `Detect Dockerfile changes`, `id: scope`, env `BACKEND_DOCKERFILE: ${{ inputs.dockerfile }}`, `BACKEND_BUILD_CONTEXT: ${{ inputs.build-context }}`, `EVENT_NAME: ${{ github.event_name }}`; bash with `set -euo pipefail`; writes `dockerfile_changed=true|false` to `"$GITHUB_OUTPUT"` and one `::notice::` line saying which rule fired. Rules: (a) if `EVENT_NAME` is not `pull_request`, or `HEAD` has no second parent (`git rev-parse --verify -q HEAD^2` fails), the answer is `true` (fail-open: a full build is the safe side; say so in the notice); (b) otherwise read `git diff --name-only -z HEAD^1 HEAD` (NUL-separated, loop with `while IFS= read -r -d ''`, correct for names with spaces) and answer `true` when any changed path equals the normalised `BACKEND_DOCKERFILE` (strip a leading `./`), or its basename matches `Dockerfile*` AND the path lies inside the build context (`BACKEND_BUILD_CONTEXT` of `.` or empty means everywhere; otherwise strip a leading `./` and trailing `/` and require the path to start with `<context>/`). Otherwise `false`. All inputs reach the script through `env`, never interpolated into the script text.
    3. `Build full production image (no --target)`, `if: ${{ steps.scope.outputs.dockerfile_changed == 'true' }}`, same `env:` secrets as the backend build step, no `BACKEND_TARGET`; the same `docker build` as the backend step WITHOUT any `--target`, with `--build-arg "VITE_APP_MUI_LICENSE_KEY=$VITE_APP_MUI_LICENSE_KEY"`, `--secret id=hram_engine_token,env=HRAM_ENGINE_READ_TOKEN`, the same `--label`, `-t "$CI_IMAGE"`, `-f "$BACKEND_DOCKERFILE"`, `"$BACKEND_BUILD_CONTEXT"`. Never echo a secret. The image is built, not pushed (no `docker push`, no `--push`).
    4. `Remove CI image`, `if: ${{ always() && steps.scope.outputs.dockerfile_changed == 'true' }}`: `docker rmi -f "$CI_IMAGE" >/dev/null 2>&1 || true`.
- **Comments** in the job: say why (incident: a Node bump in `backend/Dockerfile` broke the skipped `frontend_build` stage that the pytest-only target never builds), that the job name `image-build` is a contract with webapp-management WM-RT-49 and must not be renamed, and the fail-open rule.
- **Test file to create:** `.github/scripts/test_app_ci_image_build.py`, `unittest` style like `.github/scripts/test_app_ci_database_endpoint.py` (loads the workflow with `yaml.safe_load`). Required tests:
  1. Structure: `jobs["image-build"]` exists, has no `name` key; its `if` mentions `inputs.run-backend`, `pull_request` and `workflow_dispatch`; no step uses `--target`, `docker push` or `--push`; the build step is conditioned on `steps.scope.outputs.dockerfile_changed == 'true'` and carries `--secret id=hram_engine_token` and the MUI build arg; the cleanup step is `always()`-conditioned.
  2. The `backend` job is unchanged where it matters: its build step still contains `target_args` and `BACKEND_TARGET`, and the job still has the `postgres` service.
  3. Behaviour of the scope step: extract the `run` text of step `Detect Dockerfile changes` and execute it with `bash` in a temporary git repository (skip the test when `bash` or `git` is missing). Build a base commit and a merge commit with the base as first parent and a second parent for the PR head, set `EVENT_NAME`, `BACKEND_DOCKERFILE`, `BACKEND_BUILD_CONTEXT`, `GITHUB_OUTPUT` (a temp file; use forward slashes in paths) and assert `dockerfile_changed` for: Dockerfile changed -> true; only an unrelated file changed -> false (the opted-in target build stays as today); `backend/Dockerfile.prod` changed with context `.` -> true; a `Dockerfile` outside a narrower build context (context `backend`, file `other/Dockerfile`) -> false; a path with a space in its name that is not a Dockerfile -> false; `EVENT_NAME=workflow_dispatch` -> true; HEAD without a second parent -> true.
- **Invariants / do-not-touch:** no change to `backend`, security, frontend or e2e jobs, to pip-audit, to any composite action, to `deploy-app`, to callers' `ci.yml`, or to the `.wt-checkout` pin; no new `workflow_call` input; the `on:` block stays unchanged. Pitfall: an unquoted step `name:` containing `: ` is a YAML parse break; the tests load the file with a real YAML parser, keep it parsing. Another pitfall: do not write brace expressions literally inside comments or descriptions (Actions evaluates `${{ ... }}` there; it once took the whole fleet down).

## Target repo working directory (absolute)

`C:\Users\Micha Bigler\Documents\webapps\workflow-templates`

## Preamble — binding on the implementation instance

> The text above is the COMPLETE spec — the committed WO file's content, not a plan to refine; there is no separate plan file. Read the nearest `AGENTS.md`, the relevant `.codex/skills/<role>/SKILL.md`, and the app `MEMORY.md` ONLY for conventions. Stay in scope; do not touch auth/permissions/deps/schema/CI unless the spec says so; do not update `MEMORY.md`. **Do NOT edit `WORK_ORDERS.md` — the register row and the review verdicts are the orchestrator's alone.** **Your tools are for editing source and test files and for running the tests you wrote — nothing else.** Do NOT install dependencies, touch a lockfile, run a package manager, or tidy up stray files; if something in the repo state blocks you, stop and report it as `RESULT: BLOCKED <reason>` instead of fixing it. The working tree holds unrelated changes of other sessions (`.github/workflows/staging-health.yml`, `work-orders/WFT-CI-44.md`, `.github/scripts/test_staging_health_probe.py`, `scratchpad/`): do not touch them. Do NOT `git add`/`commit`/`push` — leave every change uncommitted in the working tree for the orchestrator's independent review. WRITE the tests the `Required tests` section calls for AND **RUN the tests you just wrote** (`python -m pytest -q -p no:cacheprovider .github/scripts/test_app_ci_image_build.py`) to confirm they execute and pass — that is the ONLY test run you do (NOT the affected/full suite, NOT any review). The orchestrator re-runs the authoritative set + does the independent review after you finish — those are the gate; your own run does not count as the gate.
>
> Narrate continuously: a `PLAN: <step1> | <step2> | …` line up front, then a single-line `PROGRESS: [<n>/<total>] <present-tense action>` before every relevant action (and `… done` on completion), spaced so no gap exceeds ~2 min, stdout unbuffered, plus exactly one final `RESULT: DONE|BLOCKED <reason>`.

# C. Orchestrator only

> **If you are the implementer reading this work order as your own specification: STOP at this line. Everything below describes what the Orchestrator does AFTER you finish. You do none of it — no reviewers, no verification run, no register edit, no commit.** You ARE the implementation instance described below; do NOT launch or dispatch another implementer.

## Execution directive

`codex exec -m gpt-6-luna` with the complete file on stdin, cwd the repo root (2026-11-05: newest `.claude/codex-status.md` line is 2026-11-04 `available`, so this dispatch is the one allowed probe of the day). Launch evidence: filled after the run.

## Review routing

Tier 3: shared CI. `reviewer` lenses (envelope, regression, duplication, tests) plus `sec_reviewer` (secrets in build args and secret mounts), concurrently, one corrective round, then the single closing pass.

## Verification

Scoped tests: `.github/scripts/test_app_ci_image_build.py` and the neighbouring `test_app_ci_*.py`. Proof: the candidate on `ci-test/WFT-CI-48` in workflow-templates plus one calling app (cockpit) whose `ci.yml` points at `app-ci.yml@ci-test/WFT-CI-48`, dispatched (a pull request cannot be opened from a throwaway ref, so the pull-request event itself cannot be exercised; both scope outcomes are pinned by the git-sandbox test, and the dispatch proves the job wiring, the check name `ci / image-build` and the full build). Recorded in the register row.

## Register + commit

Register row WFT-CI-48 with the fixed check name `ci / image-build`; commit to `main` after the proof and the reviews.
