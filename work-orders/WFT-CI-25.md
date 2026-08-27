# WFT-CI-25 — Remove the GHCR publish from `app-ci.yml`, finishing CI-11's correction

## Part A — Envelope

### Goal

`app-ci.yml` tests. It does not publish. The deploy path keeps getting its image from `main.yml`,
unchanged.

### Why the step exists, and why that reason is gone

`CI-9` put the publish into `app-ci.yml` on the assumption that CI runs before the deploy. **`CI-11`
disproved that assumption** and quotes the triggers: `ci.yml` fires on `pull_request` with no push
trigger, `main.yml` on `push` only — *"the two react to disjoint events"* — and work is committed
straight to `develop`. So the publish was added to `main.yml`, where the deploy actually is.

**The `app-ci.yml` half was never removed.** Its own comment still describes the world before that
correction: it publishes so the host does not have to, *"replaces the former server-side build"*.

Measured 2026-08-27, that world no longer exists:

- **`deploy-app` does not build.** It has no `existing_image` and no `backend_target` input, and
  contains no `docker build`. It writes `IMAGE_TAG` into `.env` and refuses an empty one — *"refusing
  a non-deterministic deploy"*. Compose pulls the tag.
- **`main.yml` publishes its own image** via `publish-backend-image`, keyed by the same
  `resolve_sha` it then hands to `deploy-app`. That pairing is the deploy path, start to finish.
- **`app-ci.yml` exposes no `workflow_call` outputs**, and **no caller reads `needs.ci.outputs`**.
  Nothing downstream consumes what it pushes.

### What keeping it costs

**1 — It gives back the CI-6 saving.** The step passes
`existing_image: ${{ inputs.backend-target == '' && env.CI_IMAGE || '' }}`. With no `backend-target`,
the CI image *is* the production image and gets reused — cheap. **With `backend-target` set,
`existing_image` is empty and a second full production build runs**, frontend and collectstatic
included. `CI-6` exists precisely to skip that. `WM-TAKE-8` has just moved all four takeover apps onto
`backend-target: backend_test`, so all four now pay the build they were opted out of.

**2 — It pushes images for refs nobody deploys.** Pull requests, and now `ci-test/*` refs: four
images today from `WM-TAKE-8`'s dispatches alone, including one for Gustav's `2dff600`, a throwaway
commit. Confirmed real — `ghcr.io/bigler-webapps/kira-backend:8cfc7615…` came from a `ci-test`
dispatch.

### Carried unknown

**How many orphaned tags have accumulated since 2026-08-08 is not established.** Listing GHCR needs
`read:packages`, `gh auth refresh` is blocked, and no retention policy for these packages was found.
Do not state a number. Establish it if credentials allow; otherwise record the gap and leave the
cleanup to a follow-up — removing the source is worth doing either way.

### Scope

- Delete the `Publish backend image to GHCR` step and its `id: publish_backend_image`.
- **Rework, do not delete, `Remove CI image`.** It removes `$CI_IMAGE` *and*
  `steps.publish_backend_image.outputs.image`; only the second half goes. The first still matters —
  it is what keeps the runner's disk from growing, which is live right now (`WM-TAKE-9`).
- Remove the `.wt-checkout` checkout of `workflow-templates` **only if nothing else in the job uses
  it**. Check; it is a shared step.

### Non-goals

- **No change to `main.yml` in any app.** The deploy path is correct and stays untouched.
- No change to `publish-backend-image` itself — `main.yml` still uses it.
- No GHCR tag cleanup here. That is a separate order and needs credentials this one does not have.

### Tier

**3** — `app-ci.yml`, nineteen callers.

### Risks

- **A caller that relies on the image existing after CI.** None found: no workflow outputs, no
  `needs.ci.outputs` reader across the estate. **Re-verify rather than trusting this line** — it is
  the single assumption the change rests on.
- **The `Remove CI image` rework is where this breaks quietly.** Deleting the step wholesale would
  leave `$CI_IMAGE` on the runner every run, turning a cleanup into a leak on a self-hosted runner
  that may already be out of disk.
- Removing a step that has published for weeks changes what exists in GHCR going forward. Anyone who
  learned to expect a PR's image will not find one. That is the intent; it should be said out loud
  rather than discovered.

### Tests

**This file has taken three defects in a week, two of which shipped without a live run.** Structural
tests read the YAML; they do not ask GitHub to parse or execute it. So:

- A live dispatch on both sides, per `AGENTS.md` exception (b): a `ci-test/<ID>` ref here carrying the
  candidate, one app pointing at `app-ci.yml@ci-test/<ID>` from its own `ci-test` ref.
- **Two callers, not one:** one **with** `backend-target` set (proving the second build is gone) and
  one **without** (proving the `existing_image` path did not depend on the removed step).
- Confirm no new tag appears in GHCR for the dispatched SHA.
- The runner's image list after the run: `$CI_IMAGE` gone, nothing left behind.

---

## Part B — Implementation map

### Files

- `.github/workflows/app-ci.yml` — the `backend` job's tail, currently around lines 298-328
  (`Publish backend image to GHCR`, `Remove CI image`, and the `.wt-checkout` step above them).
- `.github/scripts/test_app_ci_*.py` — existing tests referencing the publish step will need updating;
  find them before editing the workflow, not after.

### Reference

- `CI-9` and `CI-11` in this register — the two orders whose gap this closes.
- `.github/actions/deploy-app/action.yml` — no build, `IMAGE_TAG` required, for the claim above.

### Progress contract

`PLAN: …` · `PROGRESS: [n/total] <action>` · one final `RESULT: DONE|BLOCKED <reason>`.

---

## Part C — Orchestrator only

*Stop line.*

### Review

Tier 3, independent. One question for the reviewer above all others: **does anything, in any of the
nineteen callers, depend on an image existing in GHCR after a CI run?** Everything else in this diff
is deletion.

### Register

`WFT-CI-25`. The Notiz records both dispatch results (with and without `backend-target`), and the
orphaned-tag count if credentials allowed it — or explicitly that they did not.

### Commit

`main`, after the dispatch evidence exists. Not before.
