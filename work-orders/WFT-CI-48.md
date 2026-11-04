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

_Placeholder — filled by the Orchestrator._

# C. Orchestrator only

_Placeholder — filled by the Orchestrator._
