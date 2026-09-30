# WFT-CI-30 — Optional end-to-end job in `app-ci.yml`: Playwright specs and screen capture

Phase 2a of `webapps/work-orders/WS-GOV-1.md`. The consuming app's opt-in (Phase 2b) is a separate
work order in that app's repo, written after this one is tagged. First consumer: `kerzenziehen`.

---

# A. Envelope

## Goal & expected outcome

- **Ziel:** an app that opts in gets its Playwright specs and its screen capture run in CI against the
  image built from the commit under test, with seed data only.
- **Expected outcome:**
  - `app-ci.yml` gains an **optional** job, off by default, that a caller enables with one input.
    Callers that do not enable it see no change.
  - The job starts the app from the **`final` stage of the app's own backend image built from the
    commit under test** — the image already contains the built frontend, so what is tested is exactly
    what that commit ships — together with a database and Redis, using the app's E2E settings module
    and its guarded seed command.
  - Playwright runs **inside the official Playwright image** matching the app's `@playwright/test`
    version, on the host network, so the runner host needs Docker only (already proven by the
    backend job) and no browser dependencies.
  - The specs' result gates the job. Specs tagged `@quarantine` run in a separate, non-blocking step
    whose result is visible in the job summary.
  - The capture runs after the specs and is uploaded as an artifact (screenshots + report), with the
    commit it was built from recorded; its mechanical failures are reported, not gating, in this
    phase.
  - **The runner resets the database, not the test container** (operator decision 2026-09-30, after
    the live proof showed the Playwright image has no Docker): the job's own shell runs the app's seed
    command (`docker exec <app container> python manage.py <seed command>`) before each spec file and
    before each capture cell, and starts Playwright once per spec file / per capture cell. The test
    container gets neither the Docker socket nor any reseed endpoint; the job tells the app's tooling
    to skip its own reseed through an environment variable (`E2E_RESEED=runner`).

## Scope

- `.github/workflows/app-ci.yml` in this repo: the new job and its inputs (enable flag, frontend path,
  E2E settings module, and whatever the app must name for its seed and reseed to work in CI).
- The app side (tagging, skipping its own reseed when `E2E_RESEED=runner`, the opt-in) is Phase 2b
  (`kerzenziehen/KZ-E2E-9`), not this WO.
- Never mount the Docker socket into the Playwright container (root-equivalent on a shared runner).

## Explicit non-goals / do-not-touch

- No change to the existing `backend`, `security` and `frontend` jobs or their defaults.
- No LLM step in CI, no judgement of the capture here.
- No new secret: the job uses seed users only, never an app's `.env` or environment secrets.
- No deploy, no push-triggered run — the job runs where `app-ci.yml` already runs (the callers'
  `pull_request` trigger).

## Tier · precondition / gate

- **Tier 3:** CI, and a shared workflow every app calls.
- **Proof before landing:** exception (b) of `AGENTS.md` → Branching — a `ci-test/WFT-CI-30` ref here
  and a `ci-test/<ID>` ref in `kerzenziehen` pointing at `app-ci.yml@ci-test/WFT-CI-30`, with one real
  run showing the specs green, the quarantined spec reported, and the capture artifact present. A
  second run on a caller that does **not** opt in shows no new job.
- **Ordering:** landing waits for `kerzenziehen/KZ-FIX-3` (landed); the known live-view defect is
  handled by the quarantine (`kerzenziehen/KZ-FIX-4`). Released as a new minor tag; callers pin to it.

## Risks

- GitHub evaluates `${{ }}` anywhere in the file, input descriptions included; an expression in a
  description makes the reusable workflow unresolvable for every caller, with no jobs and no logs.
- The runner slots on `netcup-runner-1` are shared; the job's containers must be uniquely named per
  run and always removed, or parallel jobs collide and the host fills up.
- The Playwright image version must match the app's `@playwright/test` version, or the browsers do
  not start.
- Run time: an image build plus the suite plus the capture; acceptable up to roughly 15 minutes.

## Required tests to WRITE

- A structural test of the new job and inputs (off by default; quarantine step non-blocking; capture
  uploaded), **shown to fail on a broken copy** of the workflow (mutation check).
- The two real runs named above are the rest of the evidence.

---

# B. Implementation map — filled by the Orchestrator — ADDRESSED TO THE IMPLEMENTER

## Context package

**Named files to change:**
- `.github/workflows/app-ci.yml` — add six `workflow_call.inputs` (below), one guard step in the
  existing `validate-inputs` job, and one new job `e2e`.
- `.github/scripts/test_app_ci_e2e_job.py` — new structural test file, same idiom as the neighbouring
  `.github/scripts/test_app_ci_security_gates.py` (read it first — it is the house style: slice the
  job body out of the live YAML text by header, `re.search` on that slice, one deliberate "mutate the
  live text, assert the guard fires" test per assertion, one falsifying-fixture test per guard). Do
  **not** invent a different structural-test style.

**New inputs (place in a new `# End-to-end` section after the existing `# Frontend` inputs, before
`secrets:`):**

| name | type | default | purpose |
|---|---|---|---|
| `run-e2e` | boolean | `false` | off by default; a caller opts in explicitly |
| `e2e-settings-module` | string | `''` | e.g. `backend.e2e_settings`; passed as `DJANGO_SETTINGS_MODULE` |
| `e2e-seed-command` | string | `''` | e.g. `seed_e2e`; run as `manage.py <this>` inside the app container |
| `e2e-playwright-image` | string | `''` | e.g. `mcr.microsoft.com/playwright:v1.54.2-noble`; MUST match the caller's own `@playwright/test` version or the browsers refuse to start — no safe default exists, hence empty |
| `e2e-capture-command` | string | `'node tests/capture/capture.js'` | run from `inputs.frontend-path` inside the Playwright image after the specs |
| `e2e-capture-output-path` | string | `'tests/capture/output'` | relative to `inputs.frontend-path`; uploaded as an artifact |

**`validate-inputs` job — add one step**, same shape as the existing `run-migration-check` guard
right above it in the same job:
```yaml
      - name: run-e2e requires its own inputs
        if: ${{ inputs.run-e2e && (inputs.e2e-settings-module == '' || inputs.e2e-seed-command == '' || inputs.e2e-playwright-image == '') }}
        run: |
          echo "::error::run-e2e: true requires e2e-settings-module, e2e-seed-command and e2e-playwright-image to all be set."
          exit 1
```

**Do NOT write a literal `${{` inside any new `description:` field.** GitHub evaluates expressions
in input-description text too; an empty/malformed one makes the whole reusable workflow unresolvable
for every one of the ~13 callers (this exact failure already happened once, `WFT-CI-23`/`WM-TAKE-8`,
and `test_no_expression_syntax_in_workflow_call_input_descriptions` in
`test_app_ci_security_gates.py` already guards the WHOLE `workflow_call.inputs` block — your new
inputs are automatically covered by that pre-existing test, do not duplicate it, just don't trip it.

**New job `e2e`** — self-contained like `backend`/`security`/`frontend` (no shared artifacts between
jobs in this file), `if: ${{ inputs.run-e2e }}`, `runs-on: ${{ inputs.runs-on }}`.

Design constraints verified against the first consumer (`kerzenziehen`, not visible from this
checkout — treat everything below as given fact, not something to (re)discover):

1. **Build the FULL image, no `--target`.** Unlike the `backend` job's optional `backend_test`
   target, the e2e job needs the `final` stage — it already contains the built frontend
   (`COPY --from=frontend_build ... ./static/` + `./templates/index.html` in the app's
   `backend/Dockerfile`), so what Playwright exercises is exactly what that commit ships. Reuse
   `inputs.dockerfile` / `inputs.build-context` / the same `VITE_APP_MUI_LICENSE_KEY` /
   `HRAM_ENGINE_READ_TOKEN` build-arg plumbing the `backend` job already uses — do not add new
   secrets (explicit non-goal).
2. **Isolated Docker network per run, not GH Actions `services:` for the app container.** `services:`
   only works for images already resolvable from a registry (postgres, redis) — the app image is
   built locally in THIS job and can't be a `services:` entry. Create
   `docker network create ci-e2e-net-${{ github.run_id }}-${{ github.run_attempt }}`, and run
   `db` / `redis` / the app container on it by container name (`ci-e2e-db-<runid>-<attempt>` etc.,
   same uniqueness reasoning the `backend` job's `CI_IMAGE` tag already documents — three shared
   runner slots, a fixed name collides across concurrent jobs on `netcup-runner-1`).
3. **Redis's host is configurable, its PORT is NOT** — `django_core_micha`'s `settings_base.py`
   hardcodes `env("REDIS_HOST", default="redis"), 6379` as a literal tuple (`CHANNEL_LAYERS` and the
   cache backend both). This is exactly why the private per-run Docker network matters: address redis
   by its **container name** on the network's internal port 6379 (`REDIS_HOST=ci-e2e-redis-<runid>`),
   never by a host-mapped dynamic port the way the `backend` job's Postgres `services:` entry does —
   there is no env var to carry a non-default Redis port to the app.
4. **DB env vars, same names the `backend` job's `pytest` step already uses**: `DB_HOST`, `DB_PORT`
   (`5432`, the container's own internal port on the private network — again no host-port dance
   needed here since nothing publishes it), `DB_NAME`/`DB_USER`/`DB_PASSWORD` all `test`. Reuse
   `inputs.db-image` (already `postgres:18` by default) for the `db` container — kerzenziehen has no
   GeoDjango/postgis dependency (removed by its own `INF-29`), so no per-caller override is needed
   for this consumer; a future GeoDjango caller would set `db-image` itself, same as the `backend`
   job's existing pattern.
5. **Publish the app's port 8000 to an ephemeral host port** (`docker run -p 8000 ...`, no fixed host
   port — same "self-hosted runner, 3 shared slots" collision reasoning as the `backend` job's
   Postgres comment), then read it back: `docker port <container> 8000/tcp` and parse the port number
   out of its `0.0.0.0:NNNNN` output. This is the ONE port Playwright needs to reach from the host
   network (`PLAYWRIGHT_BASE_URL=http://127.0.0.1:<that port>`).
6. **Health-check before migrate/seed**: poll `curl -fsS http://127.0.0.1:<port>/api/healthz`
   (the same endpoint the app's own `docker-compose.yml` Traefik healthcheck already targets) with a
   bounded retry loop (model the `postgres` service's own `--health-retries`/`--health-interval`
   shape) before running migrations — do not sleep a fixed duration.
7. **Migrate then seed inside the running app container**: `docker exec <app-container>
   python manage.py migrate --noinput`, then
   `docker exec <app-container> python manage.py ${{ inputs.e2e-seed-command }}`, both with
   `DJANGO_SETTINGS_MODULE=${{ inputs.e2e-settings-module }}` already set as the container's own env
   at `docker run` time (not re-passed per `exec`) — same as the `backend` job's env-var style,
   plus `EMAIL_PORT`/`EMAIL_USE_TLS` since the E2E settings module swaps the mail backend (see
   `e2e_settings.py`'s own `MAILERS` override — file-based backend, no real SMTP needed).
8. **Playwright runs INSIDE the pinned official image, `--network host`, mounting the FULL checked-out
   workspace (`${{ github.workspace }}:/workspace`, `-w /workspace/${{ inputs.frontend-path }}`) — not
   just `frontend-path`**, so the capture script's own `git rev-parse HEAD` provenance check
   (`kerzenziehen/KZ-E2E-8`) finds `.git`. Every container invocation below also needs:
   `--user "$(id -u):$(id -g)"` + `-e HOME=/tmp` (a bind-mounted host checkout written to as the image's
   default root user leaves root-owned files a self-hosted runner's own non-root cleanup can't remove
   — confirmed live, broke a *different* job's checkout on two runner slots) and, for any step that
   invokes git inside the container, `-e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory
   -e GIT_CONFIG_VALUE_0=/workspace` (git ≥2.35 refuses to operate on a repo whose files are owned by a
   different UID than the running process — confirmed live too). Install dependencies with
   `npx --yes pnpm@${{ inputs.pnpm-version }} install --frozen-lockfile` — **not**
   `corepack enable`, which writes a system-wide symlink and needs root, directly conflicting with
   `--user` above; `npx` needs no shim install at all, and no other step ever invokes bare `pnpm`,
   only `npx playwright`/`node`.
9. **The runner resets the database, not the test container (operator decision, amended after the live
   proof showed the Playwright image has no Docker at all — `/bin/sh: 1: docker: not found`).** Every
   Playwright container invocation below additionally carries `-e E2E_RESEED=runner`, which tells the
   app's own `reseed.js` (and, through it, the capture script, which already calls `reseedE2E()`) to
   skip its own reseed — the job's shell does it instead, via the SAME `docker exec
   "$E2E_APP_CONTAINER" python manage.py "${{ inputs.e2e-seed-command }}"` already used once for the
   initial seed, run again before each spec file and each capture cell. **Never** give the Playwright
   container the Docker socket or a reseed HTTP endpoint — both were considered and explicitly
   rejected (socket = root-equivalent host access from a third-party image on a shared runner; an
   endpoint expands scope into the app's backend for no gain once the runner can reseed directly).
   This changes the specs/quarantine/capture steps from one bulk invocation into a **loop over
   individually-discovered units**, reseeding before each:
   - **Enumerate matching spec FILES generically — never hardcode a caller's file names, and never
     depend on Playwright's own `--list`/JSON-reporter output shape (version-fragile, unobserved in
     `--list` mode specifically — the one format actually confirmed live was a FAILURE listing, not a
     dry-run list).** Instead, read the plain files already sitting in the checkout, on the RUNNER
     itself (no container round-trip needed for this step at all): `ls tests/*.spec.js` from
     `inputs.frontend-path` (the one path convention, matching `playwright.config.js`'s own
     `testDir: './tests'`), then bucket each file by grepping its OWN source for the literal substring
     `@quarantine` — present → quarantine bucket, absent → gating bucket. This is the same "read the
     file directly" approach already used for the capture manifest below, and it makes the split exact
     and independent of any CLI tool's output format. Fail loudly (`::error::` + `exit 1`) if the
     gating bucket comes back empty — an empty match is itself a defect worth surfacing, not a silent
     no-op.
   - **Gating step ("Playwright specs")**: `for f in $GATING_FILES; do` reseed via `docker exec` (same
     command as the initial seed), `then docker run … bash -lc "npx playwright test 'tests/$f'"`,
     `set -euo pipefail` so any file's failure fails the whole step — `done`.
   - **Non-blocking step ("Playwright quarantine specs")**: same shape, iterating `$QUARANTINE_FILES`
     instead, `set +e` inside the loop, track the worst exit code across all matched files, and report
     pass/fail/"no tests matched" to `$GITHUB_STEP_SUMMARY` exactly as before — never `exit` non-zero
     from the step itself. An empty bucket here is expected (today, before `KZ-E2E-9`'s own spec gets
     the `@quarantine` tag) and must report cleanly, not error — that is the one place this loop's
     "fail on empty" rule from the gating step does NOT apply.
   - **Capture ("Capture E2E screens and report")**: enumerate cell IDs generically via
     `node -e "console.log(require('./tests/capture/manifest.json').screens.map(s=>s.id).join('\n'))"`
     run from `-w /workspace/${{ inputs.frontend-path }}` inside the container (the manifest's own
     path convention, `tests/capture/manifest.json`, colocated with the default
     `e2e-capture-command`'s `tests/capture/capture.js` — a documented assumption of the shared
     capture-harness convention Phase 1 established, same class of assumption as the already-hardcoded
     default `e2e-capture-output-path`). For each cell ID: reseed via `docker exec`, then run
     `${{ inputs.e2e-capture-command }} --routes=<cell-id>` (the manifest's own existing
     `--routes=<id,...>` filter, already part of the capture script per `kerzenziehen/KZ-E2E-1`) —
     `set +e`, non-blocking exactly as before, worst exit code tracked for the summary/warning.
10. **Cleanup, unconditionally (`if: always()`)**: `docker rm -f` the three containers, `docker network
    rm` the run's network, `docker image rm -f` the built `$CI_IMAGE` — mirrors the `backend` job's
    existing `Remove CI image` step's own `if: always()` + `|| true` style. A leaked container/network
    on the shared self-hosted runner is the Envelope's own named risk.

**Known pitfalls already paid for elsewhere in this file — do not reintroduce:**
- No bare `pip`/`docker compose` assumptions; this design uses `docker run`/`docker exec`/`docker
  network` directly, all already proven available on `netcup-runner-1` by the `backend` job.
- Every container/network/image name MUST include `${{ github.run_id }}-${{ github.run_attempt }}`
  (or equivalent) — a fixed name is the exact class of bug the `backend` job's own `CI_IMAGE` comment
  documents.
- No `${{` inside a new input `description:` (point 4 above).

## Target repo working directory (absolute)

`C:\Users\biglmi\Documents\webapps\workflow-templates`

## Preamble — REQUIRED, do not strip

> The text above is the COMPLETE spec — the committed WO file's content, not a plan to refine; there
> is no separate plan file. Read the nearest `AGENTS.md`, the relevant `.codex/skills/<role>/SKILL.md`,
> and the app `MEMORY.md` ONLY for conventions. Stay in scope; do not touch auth/permissions/deps/
> schema/CI beyond this file and its test unless the spec says so; do not update `MEMORY.md`. **Do NOT
> edit `WORK_ORDERS.md` — the register row and the review verdicts are the orchestrator's alone.**
> **Your tools are for editing source and test files and for running the tests you wrote —
> nothing else.** Do NOT install dependencies, touch a lockfile, run a package manager, or tidy up
> stray files; if something in the repo state blocks you, stop and report it as
> `RESULT: BLOCKED <reason>` instead of fixing it. Do NOT `git add`/`commit`/`push` — leave every
> change uncommitted in the working tree for the orchestrator's independent review. WRITE the tests
> the `Required tests` section calls for AND **RUN the tests you just wrote** to confirm they execute
> and pass — that is the ONLY test run you do (NOT the app's affected/full suite, NOT any review, NOT
> a live `docker`/`gh workflow run` dispatch — that proof is the orchestrator's, via a `ci-test/<ID>`
> ref). The orchestrator re-runs the authoritative set + does the independent review + the live proof
> run after you finish — those are the gate; your own run does not count as the gate.
>
> Narrate continuously: a `PLAN: <step1> | <step2> | …` line up front, then a single-line
> `PROGRESS: [<n>/<total>] <present-tense action>` before every relevant action (and `… done` on
> completion), spaced so no gap exceeds ~2 min, stdout unbuffered, plus exactly one final
> `RESULT: DONE|BLOCKED <reason>`.

---

# C. Orchestrator only — NOT ADDRESSED TO THE IMPLEMENTER

> **If you are the implementer reading this work order as your own specification: STOP at this line.
> Everything below describes what the Orchestrator does AFTER you finish. You do none of it — no
> reviewers, no verification run, no register edit, no commit.** You ARE the invocation described
> below; do NOT shell out to `codex exec`.

## Execution directive

Implement through `codex exec` in the background (`.claude/models.local.json` → `implementation`:
`codex`/`gpt-5.6-luna` as of this writing) — invoked directly via Bash, both
`--skip-git-repo-check` and `--dangerously-bypass-approvals-and-sandbox`, `-m gpt-5.6-luna`, WO passed
on stdin (this file is large). Fallback to direct Claude implementation only on Codex quota/rate-limit/
non-zero exit — the fallback flips authorship, independent review becomes mandatory (it already is,
Tier 3).

## Review routing

Tier 3, CI/CD entry criterion: `reviewer` — all four lenses (`envelope`, `regression`, `duplication`,
`tests`) — **and** `sec_reviewer` (this job creates/uses known-credential seed users and wires a
guarded seed command into CI; matches the precedent set by `kerzenziehen/KZ-E2E-1`'s own sec_review
lens on the same seed-guard class of change). No `ui_reviewer` — this diff touches no app frontend
code, only CI/YAML. All concurrent, one batch, diff inline + this WO's Part A + the one relevant
skill section (`orchestrate-codex`'s "Proving a workflow change" + the risk/pitfall list above) — not
the full governance stack.

## Verification

1. Scoped tests: `python .github/scripts/test_app_ci_e2e_job.py` (new) plus a re-run of
   `python .github/scripts/test_app_ci_security_gates.py` (Codex's new inputs sit in the same
   `workflow_call.inputs` block that file already asserts against) and
   `python .github/scripts/test_app_ci_composite_checkout.py` (guards the `.wt-checkout` pattern the
   `frontend` job depends on — confirm untouched). NOT the full `.github/scripts` suite beyond that —
   no other file in this diff touches migration-check/pnpm-setup/etc.
2. **Live proof — the WO's own gate, not optional**: push this repo's commit to
   `refs/heads/ci-test/WFT-CI-30` (a ref, not a local branch), then coordinate with the operator/other
   sessions before pointing a `kerzenziehen` `ci-test/<ID>` ref's `ci.yml` at
   `app-ci.yml@ci-test/WFT-CI-30` with `run-e2e: true` and that repo's real
   `e2e-settings-module: backend.e2e_settings` / `e2e-seed-command: seed_e2e` /
   `e2e-playwright-image` (read the pinned `@playwright/test` version from
   `kerzenziehen/frontend/package.json` at dispatch time, don't trust a memorised value — it can have
   moved). `gh workflow run ci.yml --ref ci-test/<ID>` on the kerzenziehen side, read the run: specs
   green, the quarantine step visible and non-blocking (0 tests matched today, since KZ-FIX-4 has not
   yet applied the tag), capture artifact present. A second run on a caller that leaves `run-e2e`
   unset (or the existing `develop` branch's own CI) shows no new job at all.
3. Both `ci-test/*` refs are the operator's to delete afterward — do not delete them yourself.

## Register + commit

- `WORK_ORDERS.md` (this repo) — advance the existing `WFT-CI-30` row: `planned` → `done` once
  the review is clean/findings fixed and the live proof (both refs) is green. Record:
  `review: codex/gpt-5.6-luna · lenses: envelope,regression,duplication,tests,sec_review · <n> raised
  · <k> accepted · worst accepted: …`, the live-proof run URLs/IDs, and — since the app-side opt-in
  (Phase 2b) is a separate WO in `kerzenziehen` not yet written — note explicitly that this WO's own
  "done" covers the shared workflow only, off by default for every existing caller.
- Commit message: single concise English subject line, e.g. `WFT-CI-30: optional e2e job in
  app-ci.yml (Playwright specs + capture)`.
- Report back to session `local_e275ef27-dda0-4a16-996a-ce290719c917` per the mini-handover: WO ID,
  landing SHA, register line, the live-proof run(s), and — importantly — that the `kerzenziehen`
  `ci-test/<ID>` coordination step (Verification #2) needs that repo's own session to either already
  have a compatible `ci.yml`/branch state or be looped in before the cross-repo dispatch, since this
  session does not own that repo's working tree.
