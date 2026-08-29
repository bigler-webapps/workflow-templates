# WFT-CI-28 — `publish-backend-image` drops every app-specific Vite build-time var except the MUI key

## Part A — Envelope

### Goal

Give `publish-backend-image` a way to bake jg-ferien's two survey Vite build-time vars
(`VITE_SURVEY_APP_API_BASE_URL`, `VITE_SURVEY_APP_SITE_SLUG`) into its image, so jg-ferien's
production `/survey` page stops rendering `SurveyEmbed.CONFIG_ERROR` ("Die Umfrage ist noch nicht
konfiguriert.").

### The gap

Before CI-9/CI-10/CI-11 (2026-08-08/09), the backend image was built via `docker compose build` on
the target server, where `docker-compose.yml`'s `build.args` pulled every declared build-arg
(`UV_FLAGS`, `VITE_APP_MUI_LICENSE_KEY`, `VITE_SURVEY_APP_API_BASE_URL`, `VITE_SURVEY_APP_SITE_SLUG`)
from the `.env` `generate-env` had just composed from `project.yaml` for that environment.

CI-9/10/11 moved the build onto the GitHub runner via this composite action
(`.github/actions/publish-backend-image/action.yml`), which builds with a hand-enumerated
`docker build --build-arg VITE_APP_MUI_LICENSE_KEY=... ` — **one hardcoded build-arg**, nothing else.
Any other `ARG` a consuming app's Dockerfile declares (jg-ferien's `backend/Dockerfile:27-30`) has no
input to receive it, so Docker leaves it as an empty string. jg-ferien's
`getRuntimeConfig()`([frontend/src/hooks/useSurveyRuntime.js:58-66](../../jg-ferien/frontend/src/hooks/useSurveyRuntime.js))
treats an empty `VITE_SURVEY_APP_API_BASE_URL`/`VITE_SURVEY_APP_SITE_SLUG` as `isConfigured: false`,
which is exactly the observed defect — live since jg-ferien's first deploy through the new pipeline
(2026-08-09), not since SUR-4 landed (2026-08-04, before the CI migration).

Confirmed via `grep -rn "ARG VITE_" **/Dockerfile`: only jg-ferien's `backend/Dockerfile` declares
extra Vite build-time args beyond the MUI license key; Cinevia and kira declare only
`VITE_APP_MUI_LICENSE_KEY`, which they correctly wire through their own `main.yml`
(jg-ferien's own `main.yml` does NOT — that half is `JG-CI-1`, this repo's own WO, gated on this one
landing + being tagged).

### Scope

Add two new **optional** inputs to `publish-backend-image` (`.github/actions/publish-backend-image/action.yml`),
following the exact existing `vite_app_mui_license_key` pattern (empty-string default, threaded to its
own `--build-arg`):

- `vite_survey_app_api_base_url` → `--build-arg "VITE_SURVEY_APP_API_BASE_URL=$VITE_SURVEY_APP_API_BASE_URL"`
- `vite_survey_app_site_slug` → `--build-arg "VITE_SURVEY_APP_SITE_SLUG=$VITE_SURVEY_APP_SITE_SLUG"`

### Explicit non-goals / do-not-touch

- No generic "arbitrary extra build-args" passthrough mechanism (e.g. a JSON/KV-list input) — this WO
  adds exactly the two named inputs jg-ferien needs today, mirroring the one that already exists.
  **Flagged for reviewer**: if a second app ever needs its own app-specific build-time var, this
  one-input-per-var pattern repeats this exact gap: mention whether a generic mechanism is warranted
  now instead.
- No change to `deploy-app`, to `generate-env`, or to any per-environment value resolution — this
  action stays environment-agnostic; resolving which value to pass for staging vs. production is the
  CALLER's responsibility (`JG-CI-1`).
- No change to any other repo's `main.yml` — only jg-ferien consumes the new inputs today.

### Tier · precondition / gate

- Tier: **3** — shared composite action, callable by all 19 fleet repos.
- No precondition to land this WO itself. It is itself the precondition for `jg-ferien/JG-CI-1`,
  which needs this action retagged (e.g. `v2.10.0`) before it can reference the new inputs — an
  unknown `with:` input on an old pinned tag is a hard GitHub Actions validation error, not a silent
  no-op.

### Risks

- No functional change for the other 18 callers: an unpassed optional input defaults to `""`,
  producing `--build-arg VITE_SURVEY_APP_API_BASE_URL=` — Docker treats an unset `ARG` and an
  explicitly-empty `--build-arg` identically (both yield an empty string unless the Dockerfile
  declares its own default), so this is additive only for callers that don't reference these
  `ARG`s in their own Dockerfile in the first place (verified: none of the other 18 do). **Not
  literally zero-observable** (found by review): Docker prints a one-line
  `"the requested build-arg ... is not consumed"` warning for the two new args in every other
  caller's build log — harmless log noise, not a behaviour change, but worth naming rather than
  claiming a bare "zero."
- Scope-creep risk noted above (generic mechanism) — explicitly left to reviewer/operator judgement,
  not decided here.

### Required tests to WRITE

Structural, matching this repo's existing style (`.github/scripts/test_app_ci_security_gates.py` /
`test_tailnet_connect_probe.py` — assert against the real `action.yml` text, not a reimplementation):

- New `.github/scripts/test_publish_backend_image_build_args.py`:
  - `vite_survey_app_api_base_url` and `vite_survey_app_site_slug` are both declared as optional
    inputs with an empty-string default.
  - The build step's `docker build` invocation passes both as `--build-arg KEY=$KEY`.
  - Mutation check: removing either `--build-arg` line breaks the test (proves the assertion is
    load-bearing, not tautological — per the `d8fc8e6` postmortem lesson already on file in this
    repo's own register).

---

## Part B — Implementation map

### Files

- `.github/actions/publish-backend-image/action.yml`:
  - `inputs:` block (currently ends `existing_image` at line 34) — add the two new inputs after
    `vite_app_mui_license_key` (lines 27-30).
  - `env:` block of the `Publish backend image to GHCR` step (lines 47-55) — add
    `VITE_SURVEY_APP_API_BASE_URL: ${{ inputs.vite_survey_app_api_base_url }}` and
    `VITE_SURVEY_APP_SITE_SLUG: ${{ inputs.vite_survey_app_site_slug }}`.
  - The `docker build` invocation (lines 71-78) — add the two `--build-arg` lines next to the
    existing `VITE_APP_MUI_LICENSE_KEY` one.
- `.github/scripts/test_publish_backend_image_build_args.py` — new, per Tests above.

### Reference

- `.github/actions/publish-backend-image/action.yml:27-30,54,72-73` — the exact `vite_app_mui_license_key`
  pattern to mirror for both new vars.
- `jg-ferien/backend/Dockerfile:27-30` — the two `ARG`/`ENV` pairs these build-args feed.

### Progress contract

`PLAN: …` · `PROGRESS: [n/total] <action>` · one final `RESULT: DONE|BLOCKED <reason>`.

### Preamble

> The text above is the COMPLETE spec — the committed WO file's content, not a plan to refine; there
> is no separate plan file. Read the nearest `AGENTS.md`, the relevant `.codex/skills/<role>/SKILL.md`, and
> this repo's `WORK_ORDERS.md` header ONLY for conventions. Stay in scope; do not touch auth/permissions/deps/schema/CI
> beyond what this spec names; do not update `WORK_ORDERS.md`. **Do NOT edit `WORK_ORDERS.md` — the
> register row and the review verdicts are the orchestrator's alone.** **Your tools are for editing
> source and test files and for running the tests you wrote — nothing else.** Do NOT install
> dependencies, touch a lockfile, run a package manager, or tidy up stray files; if something in the
> repo state blocks you, stop and report it as `RESULT: BLOCKED <reason>` instead of fixing it. Do NOT
> `git add`/`commit`/`push` — leave every change uncommitted in the working tree for the orchestrator's
> independent review. WRITE the tests the `Required tests` section calls for AND **RUN the tests you
> just wrote** (`python .github/scripts/test_publish_backend_image_build_args.py`) to confirm they
> execute and pass — that is the ONLY test run you do (NOT this repo's other test files, NOT any
> review). The orchestrator re-runs the authoritative set + does the independent review after you
> finish — those are the gate; your own run does not count as the gate.
>
> Narrate continuously: a `PLAN: <step1> | <step2> | …` line up front, then a single-line
> `PROGRESS: [<n>/<total>] <present-tense action>` before every relevant action (and `… done` on
> completion), spaced so no gap exceeds ~2 min, stdout unbuffered, plus exactly one final
> `RESULT: DONE|BLOCKED <reason>`.

---

## Part C — Orchestrator only

> **If you are the implementer reading this work order as your own specification: STOP at this line.
> Everything below describes what the Orchestrator does AFTER you finish. You do none of it — no
> reviewers, no verification run, no register edit, no commit.**

### Review

Tier 3, independent (`reviewer`, no `sec_reviewer` — build-arg passthrough, not auth/security).
Explicitly ask: (1) does the diagnosis hold — is CI-9/10/11's runner-side `docker build` really the
only build path now, with no other place these values could still be getting through; (2) is there a
**simpler** correct fix than adding named inputs to the shared action (e.g. default values in
jg-ferien's own `Dockerfile` `ARG` declarations, a generic extra-build-args passthrough, restoring a
`docker compose build` invocation instead of raw `docker build`, GitHub Environment-scoped variables);
(3) is the "additive, zero behavioural change for the other 18 callers" claim actually true.

### Register

`WFT-CI-28`. Notiz records the review verdict and whether a simpler alternative was recommended
instead of/in addition to landing this.

### Commit

`main`, once the independent review is clean and tests pass — this repo has no `develop`.
