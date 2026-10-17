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

_To be filled by the Orchestrator (context package, preamble, progress contract) before dispatch._

# C. Orchestrator only — NOT ADDRESSED TO THE IMPLEMENTER

> Implementer: STOP here. Integration, verification, reviews and git belong to the Orchestrator.

Source: night audit 2026-10-17, S269/S435; requirements round in session
`local_4987837b-7646-4b63-b3ab-1fd970f129bd`. Report back to: `local_4987837b-7646-4b63-b3ab-1fd970f129bd`.
