> **Self-address guard:** if you are the implementer reading this work order as your own
> specification, this section is not addressed to you — it tells the Orchestrator how to invoke
> you; you ARE that invocation. Do NOT shell out to `codex exec` yourself.

# CI-9 — Build the image once on the runner, pull it on the server

Status: planned · Tier: 2, prod-infra · Target repo: workflow-templates (main) · Datum: 2026-08-08

## A. Envelope (Expertenchat — authoritative WHAT/WHY)

### Goal

Stop every application server from building Docker images. CI already builds the image; make it push
the result, and make the deploy pull it instead of building a second time.

### Why — the image is built twice, and the second build is what fills the prod disks

`docker push` and `ghcr.io` appear **nowhere** in `app-ci.yml` or `deploy-app/action.yml`. The
current flow is:

1. The runner builds the image (`app-ci.yml:~138`), tags it locally as `$CI_IMAGE`, runs the tests
   against it — and **discards it**.
2. The deploy builds the same image **again on the target server**
   (`deploy-app/action.yml:551`, `docker compose … up -d --build --force-recreate`).

The `image_name: ghcr.io/…` entries in every app's `project.yaml`, and the `image:` references in
every compose file, are therefore effectively decoration: `compose up --build` builds locally and
applies that name, but nothing is ever pulled from the registry.

This is not a considered design with a tradeoff to weigh — it is the default of
`compose up --build`, and the push/pull half was never written. The consequence is measured: on
2026-08-08 main-prod's **build cache alone was 40 GB**, one day after three unrelated disk defects
had been fixed. Every prod box additionally carries BuildKit, the intermediate layers, and the CPU
and RAM cost of building during each deploy.

### The starting point is better than it looks

Every app's compose file already declares both:

```yaml
backend:
  image: ${IMAGE_NAME:-ghcr.io/bigler-webapps/<app>-backend}:${IMAGE_TAG:-latest}
  build:
    context: .
```

`image:` plus `build:` on the same service is exactly the dual-mode shape: with `--build` compose
builds and tags; without it, `compose pull` fetches that tag. **No app repository needs to change.**
Most apps have one `build:` stanza; hram has two. `generate-env` (dcm ≥ 2.17.5) already emits
`IMAGE_NAME` into the server's `.env`.

### Scope

**CI side (`app-ci.yml`)**

- Log in to ghcr.io (the workflow's own `GITHUB_TOKEN` with `packages: write`), tag the built image
  by **commit SHA**, push.
- `:latest` alone is not acceptable as the deploy tag — it would discard the determinism a
  server-side build gives today. A moving tag may exist alongside a SHA tag, never instead of it.
- **CI-6 interaction:** repos that opt into `backend-target: backend_test` build a leaner test-only
  image. Those need the full image built as well for the push. On the runner's warm cache that is
  cheap, and it replaces a full build on the server.

**Deploy side (`deploy-app/action.yml`)**

- Log in to ghcr.io on the target, `docker compose pull`, then `up -d` **without** `--build`.
- Thread the resolved `IMAGE_TAG` into the server's `.env` alongside the existing `IMAGE_NAME`.
- **Keep `--build` as an explicit, documented fallback** — do not delete the path. A registry
  outage must not make deploys impossible, and the first rollout needs somewhere to fall back to.

**Secrets**

- The servers need one new credential: a scoped GHCR **read** token, distributed via the sanctioned
  `sync-secrets` path. That is the only genuinely new exposure.
- It is offset: the build currently needs `HRAM_ENGINE_READ_TOKEN` (a BuildKit secret for the
  private engine package) and `VITE_APP_MUI_LICENSE_KEY` **on the server**. When the server stops
  building, both leave every prod box. Removing two build secrets in exchange for one read token is
  a net improvement — state it in the WO's outcome, and verify the removal rather than assuming it.

### Non-goals / do-not-touch

- **NOT** the app repositories. If this WO starts editing compose files, the dual-mode assumption
  above is wrong and that is a scope change back to the operator, not something to work around.
- **NOT** the daily build-cache prune (`webapp-management` **INF-11**). That is the interim measure
  and must land independently; this WO must not become its blocker.
- **NOT** multi-arch builds, image signing, or SBOM generation. Adjacent and tempting, out of scope.
- **NOT** changing what the Dockerfiles build.
- Not auth/permission logic, schema, or app behaviour.

### Acceptance

- A deploy to staging pulls the image and starts it, with **no** build step executing on the target
  host — verified in the deploy log, not inferred.
- The deployed image's digest matches the one CI pushed for that commit.
- `HRAM_ENGINE_READ_TOKEN` and `VITE_APP_MUI_LICENSE_KEY` are no longer required on the target for a
  deploy to succeed.
- The `--build` fallback still works when explicitly selected.

### Required tests to WRITE (narrow)

- The CI workflow pushes a SHA-tagged image and the deploy resolves that exact tag — assert the tag
  is not `latest`.
- The deploy path runs `pull` and does **not** pass `--build` in the default mode, and does pass it
  in the fallback mode.
- Structural: the composite action fails loudly if `IMAGE_TAG` is unresolved, rather than silently
  falling back to `:latest`. A silently-latest deploy is the worst outcome here — it looks like it
  worked and ships an unknown image.

Scoped to the workflow/action structural tests in `.github/scripts/`. Not an estate render.

### Risks

- **ghcr.io becomes a deploy dependency.** Registry unavailable means no deploy, where today only
  GitHub source access is required. Mild, and the reason the `--build` fallback stays.
- **Tag discipline.** Without a SHA tag the estate loses reproducibility that it currently gets for
  free. This is the single most likely thing to be done carelessly.
- **First pull-deploy fails late.** A misconfigured registry login on the target surfaces only at
  deploy time, on the target. Staging first, and read the log rather than trusting the exit code.
- No new dependency on the runner is introduced — the deploy already runs from CI.
- Pull traffic replaces build traffic. Usually favourable (only changed layers move, versus a build
  re-downloading packages), but worth measuring on the first staging run rather than asserting.

### Orientation hints

- `.github/workflows/app-ci.yml:~110-145` — the build step, its `$CI_IMAGE` tag, and the CI-6
  comment explaining the runner's warm-cache setup.
- `.github/actions/deploy-app/action.yml:551` — the `compose up -d --build --force-recreate` line to
  replace; `:223` — the note on `generate-env` emitting `IMAGE_NAME` (dcm ≥ 2.17.5); `:78` — the
  `HRAM_ENGINE_READ_TOKEN` BuildKit secret that becomes unnecessary on the target; `:520` — the
  comment on serialising parallel builds to avoid OOM, which this WO makes moot.
- Any app's `docker-compose.yml` — the `image:` + `build:` dual-mode shape this relies on.
- `webapp-management/work-orders/INF-11.md` — the interim prune, and why it must not wait for this.

### Execution directive

Codex-first via `codex exec` (both flags, prompt from this committed file). Codex was out of credits
through 2026-08-06; if no newer evidence exists, make exactly one probe and fall back to Claude on
failure, with a mandatory independent `reviewer`. Do NOT spawn a nested `codex exec`. Leave the diff
uncommitted for the Orchestrator's independent review; run the scoped tests only; commit on green
(`main`). **Do not deploy** — the first pull-based deploy is operator-gated and goes to staging
first.

## B. Implementation map (Orchestrator)

Single repo (`workflow-templates`, cwd = repo root). Do NOT `git add`/`commit`/`push` — leave the
diff for the Orchestrator's review.

### The key design constraint: zero app-repo changes

Every app repo's own `.github/workflows/*.yml` calls `app-ci.yml` (reusable workflow) and the
`deploy-app` composite action with the inputs that already exist today. The Envelope's "NOT the app
repositories" non-goal means **no per-app caller workflow may need a new input added** — only
`workflow-templates` changes. Two facts make this possible without touching any app repo:

1. **The commit SHA is free inside both workflows.** `app-ci.yml`'s `backend` job and the
   `deploy-app` composite action both run in a job that already has `github.sha` in scope (the
   checked-out commit) — neither needs a caller to pass a tag through. Use `github.sha` as the image
   tag on both the push side and the pull side; they will match because both run from the same
   workflow-dispatch/push event's commit.
2. **A new org-level secret needs no per-app wiring.** Every app repo's deploy step already passes
   `secrets_context_json: ${{ toJson(secrets) }}` into `deploy-app` (action.yml:41-43, consumed at
   `:363-373` where it's filtered down for `generate-env`). If the new GHCR read token is provisioned
   as an **organization secret** (not a per-repo one), it lands inside that same `toJson(secrets)`
   blob automatically in every app repo with no caller-side edit. Extract it from
   `SECRETS_CONTEXT_RAW`/`secrets_context_json` inside the action the same way the existing filter
   step reads that JSON — do not add a new required action input that every app workflow would have
   to start passing. (Provisioning the actual org secret value in Proton/GitHub Org Secrets is an
   operator step outside this WO's diff — see "New secrets" in the orchestrate-codex skill; the code
   only needs to read whatever key name you choose, e.g. `GHCR_READ_TOKEN`, from that JSON.)

### Files to change

**`.github/workflows/app-ci.yml`** (`backend` job, `:~91-145`)
- After the existing "Build backend image" step (currently tags only `$CI_IMAGE`, discarded at
  `:174-176`), also tag the image `ghcr.io/${{ github.repository_owner }}/<app>-backend:${{
  github.sha }}` — derive `<app>-backend` the same way `deploy-app`/`project.yaml` already name
  images (check `image_name:` convention in any app's `project.yaml` / compose file referenced in the
  Envelope, e.g. `ghcr.io/bigler-webapps/<app>-backend`), or read the name from `project.yaml` if a
  checkout of it is available in this job (it is — full repo is checked out at `:99`).
- Add a `docker login ghcr.io` step using the workflow's own ambient `${{ secrets.GITHUB_TOKEN }}`
  (needs `permissions: packages: write` added at the job or workflow level — currently only
  `contents: read` at `:83`) and `docker push` the SHA-tagged image. Keep the existing `$CI_IMAGE`
  build/test/rmi flow unchanged — this is an additional tag+push, not a replacement.
- CI-6 interaction (`backend-target: backend_test`): when a caller opts into the leaner test image,
  build the **full** (non-`--target`) image as a second build for the push, per the Envelope — reuse
  the warm layer cache, don't skip pushing.
- Do not push when `run-backend` is false or the build fails — push must be gated on tests passing,
  not run in parallel with them (a red build must never be the one pulled at deploy time).

**`.github/actions/deploy-app/action.yml`**
- New composite input, e.g. `use_build_fallback` (`required: false`, `default: 'false'`) — the
  documented escape hatch the Envelope requires.
- Before the remote block at `:508-556`: add a `docker login ghcr.io` (remote, over the same SSH
  session pattern already used for `hram_engine_token` at `:78`/`:539` — local-interpolated
  credential, remote-side `docker login`) using the GHCR read token extracted per point 2 above.
- Replace `docker compose ... up -d --build --force-recreate --remove-orphans` (`:551`) with a
  branch: default path does `docker compose ... pull` then `up -d --force-recreate --remove-orphans`
  (no `--build`); `use_build_fallback == 'true'` keeps today's `--build` line verbatim, unchanged.
- **Fail loudly, never silently default to `:latest`:** if the GHCR login fails, or the resolved tag
  is empty (it shouldn't be — `github.sha` is always populated in a real run — but assert it rather
  than trusting it), the step must exit non-zero, not fall through to whatever `IMAGE_TAG` default
  the compose file's `${IMAGE_TAG:-latest}` would silently pick.
- `IMAGE_TAG` reaches the server via the same mechanism `IMAGE_NAME` already does: appended to the
  local `.env` (written by the "🔐 Generate .env file" step, `:359-376`) **before** the rsync step
  (`:378-414`) ships it to the server — `generate-env` itself does not need to know about
  `IMAGE_TAG`; append the line to the `.env` file after `generate-env` runs, same file, same rsync.
- `HRAM_ENGINE_READ_TOKEN` / `VITE_APP_MUI_LICENSE_KEY` (currently required as BuildKit secrets on
  the remote build, `:539`/`hram_engine_token` input) stay wired for the `use_build_fallback` path
  but are no longer needed for the default pull path — do not remove the inputs (fallback needs
  them), verify per the Acceptance criterion that a default-path deploy doesn't require them to be
  set.

### Tests to write (in `.github/scripts/` per WO scoping — new file, e.g. `test_deploy_app_action.py`)

- Parse `deploy-app/action.yml` as YAML/text: the default-mode compose command does not contain
  `--build`; the `use_build_fallback` branch does.
- `app-ci.yml`: the push step tags with `${{ github.sha }}` (or equivalent), not a bare `:latest` —
  assert `latest` does not appear as the *only* pushed tag (a moving `latest` tag alongside the SHA
  tag is fine per the Envelope; SHA-only-as-the-deploy-tag is the thing under test).
- Structural: the composite action has no path where an unresolved/empty tag falls through to a
  successful exit — e.g. assert `set -euo pipefail` (or equivalent explicit checks) governs the new
  login/pull block, matching the existing remote-block pattern at `:449`/`:513`.

State which assertion covers which file, per the WO's own scoping note.

### Deviation from the Envelope's secrets plan (discovered during review)

The Envelope assumed the CI-side push could use the ambient `GITHUB_TOKEN` with a `packages: write`
permission added to `app-ci.yml`. An independent review caught that this doesn't work: a reusable
workflow's `GITHUB_TOKEN` permissions are capped by the **caller's** `permissions:` block, and every
app repo's `ci.yml` declares only `contents: read` — `packages: write` added solely inside
`app-ci.yml` would still 403 on push for every app tracking `@main`, which is nearly all of them.
Verified against hram's and jg-ferien's `ci.yml` (both `contents: read` only, no job-level override).

Fix: a single new org-level PAT secret (`GHCR_TOKEN`, `write:packages` scope — write implies read)
used for **both** the CI push and the deploy pull, instead of the Envelope's implied two mechanisms
(ambient token for push, separate read token for pull). This is fewer credentials than originally
implied, not more, and still requires zero app-repo changes — confirmed every app's `ci.yml` already
uses `secrets: inherit` (hram, jg-ferien, kerzenziehen, innoservice, survey_app, survey_contact_app,
reimbursements, spesix, cockpit, hpc-bridge), so the new org secret reaches the reusable workflow
automatically. Provisioning `GHCR_TOKEN` in Proton/GitHub Org Secrets remains the same operator step
the Envelope already called for; only the token's name and scope changed (one token, write:packages,
not "one read token").

### Invariants / do-not-touch

- No app repository's compose file or per-app workflow caller changes.
- `--build` fallback path must still work byte-for-byte as today when explicitly selected.
- No secret value is ever echoed/logged (follow the existing heredoc local/remote split pattern at
  `:495-512` — plain-text assignment locally, `\$VAR` remote-escaped).
- Do not touch `terraform-*` files, CI/CD dispatch permissions, or any auth/permission logic outside
  the GHCR read-only credential itself.
