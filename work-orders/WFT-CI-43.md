# WFT-CI-43 — Runner hardening: fresh base images, runner files and App key out of the jobs' reach (closes the fixable part of S268)

# A. Envelope

**Goal.** Close the fixable part of S268 from the 2026-10-17 estate audit
(`webapp-management/SECURITY_FINDINGS.md`). Expected outcome: a CI job on the shared runner can no longer
poison the base image of the next production build, rewrite another slot's runner binaries or JIT config, or
read the runner GitHub App's private key.

**Background (from the finding).** `publish-backend-image` runs `docker build` without `--pull`; the app
Dockerfiles use tag bases (`FROM python:3.14-slim`) and the runner's Docker daemon keeps tags across jobs, so a
job can retag a malicious image as the base and the next deploy builds from it and ships it to production. The
runner tree, `bin/jit-config.sh` and `app.pem` (0600) belong to the runner user that all slots run as
(`ansible/roles/github_runner`), so a job can overwrite another slot's runner or JIT script and read the App
key (which can register a foreign runner with the deploy label).

**Scope.**
1. `publish-backend-image`, and any other action that builds deployable images, builds with `--pull`.
2. The github_runner role makes the runner binaries, their configuration and `jit-config.sh` owned by root and
   not writable by the runner user; the runner user keeps write access only where a job must write (work and
   diagnostic directories).
3. The App private key is readable only by root and used by a root-run step that hands each runner its JIT
   config; the runner user cannot read it.
4. Rollout through `ansible-provision` on the runners group (operator-confirmed dispatch), after WM-SEC-8, which
   changes the same role.

**Residual (not fixed here).** All slots run as one Unix user, so a job can read the environment of a
concurrent job's processes; a separate deploy runner host stays a non-goal (operator decision for WM-SEC-8).

**Non-goals / do not touch.** Digest pinning of app Dockerfiles; a separate deploy host; the WM-SEC-8 items.

**Risks.** Runner self-update or JIT registration breaking once files are root-owned; builds failing when the
registry is unreachable (`--pull`).

**Required tests (write as part of the implementation).**
- The build command of `publish-backend-image` carries `--pull`.
- Role tests: rendered owner and mode of the runner binaries, `jit-config.sh` and the key; the key is used only
  by the root step.
- Post-rollout: a job on the runner can neither write `jit-config.sh` nor read the key, and a staging deploy
  still succeeds.

**Target repo.** `workflow-templates`, branch `main` (rollout via `webapp-management`). **Tier 3** (CI,
credentials). Sequence: after WM-SEC-8.

# B. Implementation map — ADDRESSED TO THE IMPLEMENTER

_To be filled by the Orchestrator (context package, preamble, progress contract) before dispatch._

# C. Orchestrator only — NOT ADDRESSED TO THE IMPLEMENTER

> Implementer: STOP here. Integration, verification, reviews and git belong to the Orchestrator.

Source: night audit 2026-10-17, S268; requirements round in session
`local_4987837b-7646-4b63-b3ab-1fd970f129bd`. A new tag needs the operator's approval of the version. Report
back to: `local_4987837b-7646-4b63-b3ab-1fd970f129bd`.
