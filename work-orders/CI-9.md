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
