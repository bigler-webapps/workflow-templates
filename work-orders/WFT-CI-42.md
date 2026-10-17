# WFT-CI-42 — No runtime .env left on the runner, 0600 on the servers (closes S269, S435 deploy part)

# A. Envelope

**Goal.** Close S269 and the server-side part of S435 from the 2026-10-17 estate audit
(`webapp-management/SECURITY_FINDINGS.md`). Expected outcome: a deploy leaves no copy of the generated
runtime `.env` (all production secrets) in the persistent self-hosted runner workspace, and the file arrives
on every server readable only by its owner.

**Background (from the findings).**
- S269: `.github/actions/deploy-app/action.yml` runs `generate-env --env "${TARGET_ENV}"` (writes `./.env`),
  appends `IMAGE_TAG` and rsyncs; nothing removes it. It stays in
  `/opt/gh-runner/slot-N/_work/<repo>/<repo>/.env` until the same repo is checked out again on that slot, and
  every later job on the host (same Unix user, S268) can read it.
- S435: `generate_env.write_env_file` uses the default umask; the rsync uses
  `--chmod=...Fo=r`; `/srv/apps` is mode 2775 — the `.env` arrives world-readable (0664) on the servers.

**Scope (operator decision 2026-10-17: fix S269 together with S435; the deploy side suffices, no
django-core-micha change).**
1. `deploy-app`: generate the `.env` outside the checked-out workspace (runner temp directory) with a
   restrictive umask, transfer it with mode 0600, and remove the local copy at the end of the step even when
   the step fails. The main rsync no longer carries a `.env`.
2. Existing server `.env` files get mode 0600 on the next deploy of each app.
3. `deploy-traefik`: the same treatment for its generated `.env` if it writes one the same way (check and
   apply the identical pattern; no other change).
4. Release as a new tag; app callers pick it up through their normal `deploy-app` version bump (not part of
   this order).

**Non-goals / do not touch.** `generate_env` in django-core-micha; `/srv/apps` directory mode; the shared
runner design (S268); S304 heredoc hardening; app `main.yml` pins.

**Risks.** Compose on the server must still read the `.env` (owned by the deploy user, so 0600 suffices); a
failed transfer must not leave the server without its `.env`; a cleanup trap that fires too early breaks
the rsync.

**Required tests (write as part of the implementation).**
- Action-level check (script test or shell harness used in this repo) that the generated file never lands in
  the workspace and is removed after success and after a forced failure.
- Candidate proof via a `ci-test/<ID>` staging deploy of one app pinned to the candidate: on staging the app's
  `.env` has mode 0600 and the app starts; on the runner the job leaves no `.env` in the workspace.

**Target repo.** `workflow-templates`, branch `main`. **Tier 3** (CI/CD, prod secrets).

# B. Implementation map — ADDRESSED TO THE IMPLEMENTER

## Context package

Work from this package; open only the named files. Absolute root: `C:\Users\Micha Bigler\Documents\webapps\workflow-templates` (branch `main`). Another change (WFT-CI-41) touches `.github/workflows/app-ci.yml` and `test_app_ci_e2e_job.py`: do not edit those two files.

### Files

1. **`.github/actions/deploy-app/action.yml`** (composite action):
   - Step `🔐 Generate .env file` (~line 375): sets `SECRETS_CONTEXT`, runs `generate-env --env "${TARGET_ENV}"` which writes `./.env` into the checked-out workspace (generate-env has `--output <path>`, default `.env`), then `printf '\nIMAGE_TAG=%s\n' "$IMAGE_TAG" >> .env`.
   - Step `🚀 Rsync project to server` (~line 408): `rsync_args=(... --delete ... --chmod=Du=rwx,Dg=rwx,Do=rx,Fu=rw,Fg=rw,Fo=r ... --exclude ...)` then `rsync "${rsync_args[@]}" ./ user@host:REMOTE_PATH`; this is what currently carries `.env` (world-readable after the chmod mask).
   - Later steps read the `.env` ON THE SERVER (`--env-file .env` in the remote heredoc); they stay as they are.
2. **`.github/actions/deploy-traefik/action.yml`**: step `🔐 Generate .env` (~line 142) runs `generate-env --env "${TARGET_ENVIRONMENT}" --config project.yaml` into `./.env`; step `📂 Rsync project to server` (~line 198) carries it with the same `--chmod=...Fo=r`; step `🚀 Start containers` checks `[ ! -f .env ]` on the server. Apply the identical pattern.
3. **`.github/scripts/test_deploy_app_action.py`**: add a new test class `WftCi42RuntimeEnvTests` at the end of the file, before the `if __name__` runner if any. Follow the executable-harness idiom already in the file (`INF51DeployImageCleanup`, `_extract_cleanup_block`, `_run_cleanup`, the `DOCKER_STUB` stubs, `BASH = shutil.which("bash")`, skip when no bash): extract the real `run:` bodies of the changed steps from `action.yml` and execute them under bash with stub `generate-env`, `ssh`, `rsync` binaries on `PATH` and a temporary `RUNNER_TEMP`/workspace/`GITHUB_ENV`. Do NOT re-implement the step logic in the test.

### Required behaviour (deploy-app, then the same for deploy-traefik)

- **Generate:** `umask 077`; create a private directory with `mktemp -d "${RUNNER_TEMP}/deploy-env.XXXXXX"` (outside the workspace, mode 0700), write the file as `<dir>/.env` via `generate-env ... --output`, append `IMAGE_TAG` to that file, and export the directory path to later steps through `$GITHUB_ENV` (a path, not a secret). The workspace never contains a `.env` written by this action.
- **Main rsync:** add `--exclude=/.env` (anchored) so the workspace-wide rsync (with `--delete`) neither sends nor deletes a server `.env`.
- **Transfer (new step after the rsync, before anything that needs `.env` on the server):** send the file over ssh in one atomic operation that ends with mode 0600 owned by the deploy user — pattern: `ssh <host> 'umask 077; cat > "$P/.env.new" && chmod 600 "$P/.env.new" && mv -f "$P/.env.new" "$P/.env"' < "$ENV_FILE"` with the remote path passed as an ssh argument the same way existing steps template `REMOTE_PATH` (it is validated upstream; reuse that style). A failed transfer must leave the previous server `.env` untouched and fail the step. A second `chmod 600` of an existing `.env` is covered by the same replace.
- **Cleanup (new final step with `if: ${{ always() }}`):** remove the private directory recorded in `$GITHUB_ENV`; tolerate an empty/unset variable (the generate step may have failed before it was set); refuse to delete a path that is not inside `${RUNNER_TEMP}`; it must never fail the job. It runs after success and after any earlier failure.
- Keep every other behaviour of both actions byte-for-byte (secrets filtering, tailnet checks, file-secret provisioning, deploy and health steps).

### Tests (the one test run you do, once)

In `WftCi42RuntimeEnvTests`: (a) after the generate step the workspace directory contains no `.env` and the private file exists with mode 0600 under `RUNNER_TEMP`; (b) the rsync step's arguments contain `--exclude=/.env`; (c) the transfer step sends the file's content to the stubbed ssh with a command that applies `umask 077`, `chmod 600` and an atomic `mv`, and a failing ssh stub makes the step fail without touching a pre-existing remote file (stub records its stdin and command); (d) the cleanup step removes the directory after success and after a forced transfer failure, tolerates an unset path, and refuses a path outside `RUNNER_TEMP`; (e) the same four facts for `deploy-traefik`. Also a structural assertion that cleanup is the last step and has `if: ${{ always() }}`. Run: `python -m pytest .github/scripts/test_deploy_app_action.py -k WftCi42 -q --tb=short` from the repository root. The test file is plain `unittest`-style and runs under pytest.

### Invariants and pitfalls

- Composite-action steps: each `run:` is bash with `set -euo pipefail`; `$GITHUB_ENV` lines are `NAME=value`.
- `${{ }}` expressions must not be added inside `run:` bodies (existing code routes inputs through `env:`); keep that.
- `generate-env` is the installed django-core-micha CLI; do not change it and do not pass anything but the existing arguments plus `--output`.
- Do not change inputs/outputs, version strings, docs, `WORK_ORDERS.md`, or any other action.

## Target working directory (absolute)

`C:\Users\Micha Bigler\Documents\webapps\workflow-templates`

## Preamble

The text above is the COMPLETE spec — the committed WO file's content, not a plan to refine; there is no separate plan file. Read the nearest `AGENTS.md` ONLY for conventions. Stay in scope; do not update `MEMORY.md`. **Do NOT edit `WORK_ORDERS.md` — the register row and the review verdicts are the orchestrator's alone.** **Your tools are for editing source and test files and for running the tests you wrote — nothing else.** Do NOT install dependencies, touch a lockfile, run a package manager, or tidy up stray files; if something blocks you, stop and report `RESULT: BLOCKED <reason>`. Do NOT `git add`/`commit`/`push` — leave every change uncommitted for the orchestrator's independent review. WRITE the tests AND RUN the `WftCi42` tests once — the only test run you do; the orchestrator's run is the gate. Other agents share this working tree: edit only the three files named above and preserve everyone else's changes. Use explicit UTF-8 file I/O.

Narrate continuously: a `PLAN: <step1> | <step2> | …` line up front, then a single-line `PROGRESS: [<n>/<total>] <present-tense action>` before every relevant action, and exactly one final `RESULT: DONE|BLOCKED <reason>`.

# C. Orchestrator only — NOT ADDRESSED TO THE IMPLEMENTER

> Implementer: STOP here. Integration, verification, reviews and git belong to the Orchestrator.

Source: night audit 2026-10-17, S269/S435; requirements round in session
`local_4987837b-7646-4b63-b3ab-1fd970f129bd`. Report back to: `local_4987837b-7646-4b63-b3ab-1fd970f129bd`.
