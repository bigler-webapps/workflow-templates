# WFT-CI-49 — Playwright containers share the app container's network namespace instead of the host's

# A. Envelope — authored by the Expertenchat

## Goal & expected outcome
- Goal: the E2E Playwright containers of `app-ci.yml` no longer see network-interface changes of the shared runner host.
- Expected outcome: in the `e2e` job, every Playwright container that talks to the app (gating specs, collect-failures, quarantine, capture cells) runs with `--network container:"$E2E_APP_CONTAINER"` and `PLAYWRIGHT_BASE_URL="http://127.0.0.1:8000"`; no Playwright run uses `--network host` any more for app traffic.

## Why
innoservice run 42194250492 (ci-test/INNO-BATCH-1106): `measurement-first-save.spec.js` failed in two attempts with "Error: Network Error"; its trace shows `net::ERR_NETWORK_CHANGED` on three requests to `127.0.0.1:<port>`. Other E2E jobs on the same runner create and remove docker networks (`docker network create` per run), and a host-networked Chromium sees every such interface change as a network change that aborts in-flight requests. Inside the app container's own network namespace no host interface changes arrive. The cause is a hypothesis from the trace plus the `--network host` setup, not a measured fact; the proof run is what tests it.

## Scope + non-goals
- In scope: `.github/workflows/app-ci.yml`, e2e job only: the three Playwright run sites that talk to the app and the capture-cell run site; `.github/scripts/test_app_ci_e2e_job.py` (assertions updated and extended); `README.md` / `CHANGELOG.md` lines that describe the E2E networking, if any exist.
- Non-goals / do-not-touch: the `backend` job (lines with `--network host` there are unrelated), `staging-health.yml`, the "Install Playwright dependencies" step and the capture manifest enumeration step (no app traffic; they stay as they are), the app start (`-p 127.0.0.1::8000`) and `APP_PORT` / health-check step (host-side curl, still needs it), any change to caller workflows or to any app repo, input names, artifacts, cleanup.

## Tier · precondition / gate
- Tier 3: CI (shared `app-ci.yml`, consumed `@main` by every app).
- Gate #1: the operator's "Ja, INNO-FIX-53 angehen" in chat 2026-11-06.
- Precondition: none. A change to `app-ci.yml` is proven on a `ci-test/<ID>` ref on BOTH sides (candidate in `workflow-templates`, one app pointing at `app-ci.yml@ci-test/<ID>`), before it lands on `main`.

## Risks
- Fleet-wide: all joined apps (innoservice, kerzenziehen, spesix, ...) take the change the moment it is on `main`. Mitigation: proof on a ref first, with a green e2e job of one app.
- A spec or the capture script that hardcodes the host port would break: they read `PLAYWRIGHT_BASE_URL` only (checked for innoservice; the proof run checks the rest).
- The Playwright container loses access to anything bound only on the host loopback: nothing in the job needs it (database, redis and app are reached through the app container).
- Django `ALLOWED_HOSTS` is `localhost,127.0.0.1` in the job: unchanged and sufficient for `127.0.0.1:8000`.

## Required tests to WRITE
- Update `.github/scripts/test_app_ci_e2e_job.py` so the e2e job assertions describe the new shape: the Playwright run sites that talk to the app use `--network container:"$E2E_APP_CONTAINER"` and `PLAYWRIGHT_BASE_URL="http://127.0.0.1:8000"`; neither the string `--network host` nor `$APP_PORT` appears in those run sites; the install step and the capture manifest enumeration step are unchanged; plus a mutation-style check in the file's existing style (reverting one run site to `--network host` makes the assertion fail).
- No other tests.

---

# B. Implementation map — filled by the Orchestrator — ADDRESSED TO THE IMPLEMENTER

## Context package

- **Named files to change:**
  - `.github/workflows/app-ci.yml`, e2e job. The four run sites that talk to the app (current line numbers, verify by opening the file):
    - ~869 `docker run --rm --network host` inside `if docker run ...; then` in the gating loop ("Playwright specs"), env `PLAYWRIGHT_BASE_URL="http://127.0.0.1:$APP_PORT"`;
    - ~938 `docker run --rm --network host` in the quarantine loop, same env;
    - ~1025 `docker run --rm --network host` in the capture-cell loop ("Capture E2E screens and report"), same env;
    - the collect-failures branch of the gating loop, if it has its own `docker run` (read lines ~855-900 and check for a second call).
    Replace `--network host` with `--network container:"$E2E_APP_CONTAINER"` and the base URL with `http://127.0.0.1:8000`.
  - Keep unchanged: ~789 "Install Playwright dependencies" and ~997 manifest enumeration (`--network host`, no app traffic); ~322 and ~350 (backend job); `APP_PORT` export at ~736-738 and the health check at ~772 (host-side curl).
  - `.github/scripts/test_app_ci_e2e_job.py`: tests around `step_body(E2E_JOB, "Playwright specs")` and the capture step; existing assertions at ~323 (`--user "$(id -u):$(id -g)"`) must keep passing. Helper `step_body` and the `E2E_JOB` fixture are in that file. The tests at ~1110 and ~1292 set `APP_PORT="8000"` in a fake environment; read them before changing anything they depend on.
- **Contract slice:** the app container is started in "Start app" as `docker run -d --name "$E2E_APP_CONTAINER" --network "$E2E_NETWORK" -p 127.0.0.1::8000 ...`; the app (Daphne) listens on 8000 inside the container. `docker run --network container:<name>` makes the new container use that container's network namespace, so `127.0.0.1:8000` inside it is the app.
- **Invariants:** `--user "$(id -u):$(id -g)"`, `-e HOME=/tmp`, `-e E2E_RESEED=runner`, the volume mounts, `${E2E_MAIL_PW_ARGS:-}` and the capture env (`GIT_CONFIG_*`) stay exactly as they are; no new inputs, no new steps; the cleanup step removes the app container only after the Playwright runs (check the step order, do not reorder).
- **Pitfalls:** the app container must be running when a Playwright container joins its namespace (it is, for the whole job); `--network container:` cannot be combined with `-p` or `--network` of another kind on the same run (the Playwright runs use none of them). Do not touch quoting inside the `bash -lc "..."` strings.
- Work from this package; do not explore broadly from scratch; open only the named files to verify.

## Target repo working directory (absolute)

`C:\Users\Micha Bigler\Documents\webapps\workflow-templates`

## Preamble

> The text above is the COMPLETE spec — the committed WO file's content, not a plan to refine; there
> is no separate plan file. Read the nearest `AGENTS.md` and the `.codex/skills/<role>/SKILL.md`
> ONLY for conventions. Stay in scope; do not touch auth/permissions/deps/schema/CI beyond what the spec
> names; do not update `MEMORY.md`. **Do NOT edit `WORK_ORDERS.md` — the register
> row and the review verdicts are the orchestrator's alone.** **Your tools are for editing source
> and test files and for running the tests you wrote — nothing else.** Do NOT install dependencies,
> touch a lockfile, run a package manager, or tidy up stray files; if something in the repo state
> blocks you, stop and report it as `RESULT: BLOCKED <reason>` instead of fixing it. Do NOT
> `git add`/`commit`/`push` — leave every change uncommitted in the working tree for the
> orchestrator's independent review. The working tree has foreign uncommitted changes in other files
> (ansible/, staging-health.yml, work-orders/WFT-CI-44.md): do not touch or revert them. WRITE the tests the `Required tests` section calls for AND **RUN the tests you
> just wrote** to confirm they execute and pass — that is the ONLY test run you do. The orchestrator
> re-runs the authoritative set + does the independent review after you finish — those are the gate;
> your own run does not count as the gate.
>
> Narrate continuously: a `PLAN: <step1> | <step2> | …` line up front, then a single-line
> `PROGRESS: [<n>/<total>] <present-tense action>` before every relevant action (and `… done` on
> completion), spaced so no gap exceeds ~2 min, stdout unbuffered, plus exactly one final
> `RESULT: DONE|BLOCKED <reason>`.

---

# C. Orchestrator only — NOT ADDRESSED TO THE IMPLEMENTER

> **If you are the implementer reading this work order as your own specification: STOP at this line.
> Everything below describes what the Orchestrator does AFTER you finish. You do none of it — no
> reviewers, no verification run, no register edit, no commit.** You ARE the implementation
> instance described below; do NOT launch or dispatch another implementer.

## Execution directive

Configured `implementation` entry from `.claude/models.local.json`, started via `codex exec` per `orchestrate-codex`.

## Review routing

Tier 3: all review lenses concurrently, full context, plus the Orchestrator's own pass over the diff against Part A. No `ui_reviewer`, no `sec_reviewer` unless the diff touches credentials (it must not).

## Verification

Authoritative run: the scoped Python test file `.github/scripts/test_app_ci_e2e_job.py` (and the other `test_app_ci_*` files as the affected set). CI proof on both sides: candidate `ci-test/WFT-CI-49` in `workflow-templates`, plus `ci-test/WFT-CI-49` in innoservice with its `ci.yml` pointing at `app-ci.yml@ci-test/WFT-CI-49`, dispatched with `-f e2e=true`. Evidence to record: the run ID and whether the e2e job shows no `ERR_NETWORK_CHANGED`. One green run does not prove a flake is gone; say so in the Notiz.

## Register + commit

Row `WFT-CI-49` in `WORK_ORDERS.md`; INNO-FIX-53 in innoservice gets its status and the commit SHA. Commit to `main` of `workflow-templates`.
