# WFT-CI-35 — The e2e job can share a mail directory between app and Playwright

Facts this Envelope rests on (read 2026-10-11, `workflow-templates@8ebd4e2`, `spesix@b3561cd`):
- `app-ci.yml` job `e2e` starts the app with `docker run -d --name "$E2E_APP_CONTAINER" ...`
  ("Start app") and runs every Playwright invocation (install, specs one file at a time, capture) in a
  separate `docker run --rm --network host --user "$(id -u):$(id -g)" ... -v "${{ github.workspace }}:/workspace"`
  container. The Playwright container has no Docker access; `reseed.js` therefore short-circuits with
  `E2E_RESEED=runner` and the runner reseeds between files.
- No directory is shared between the app container and the Playwright containers. An app's e2e
  settings that write mail to files (spesix: `MAILERS` file backend at
  `<tempdir>/spesix_e2e_mail`, `backend/backend/e2e_settings.py:24`) write inside the app container,
  where no spec can read them.
- spesix's E2E coverage series needs specs that follow a link from a mail: invitation and password
  reset (SPX-E2E-14), the signing link (SPX-E2E-20). Operator decision 2026-10-11: file and mail
  content is asserted in backend pytest; following mail links needs a shared mail directory in CI,
  provided by this WO.
- e2e inputs are validated before they reach a shell (WFT-CI-33); the job is covered by
  `.github/scripts/test_app_ci_e2e_job.py`.

---

# A. Envelope

## Goal & expected outcome

- Ziel: an app opting in can let its Playwright specs read the mails its app container writes during
  the e2e job.
- Expected outcome:
  - `app-ci.yml` gains a boolean input `e2e-mail-dir` (default `false`).
  - With `true`, the e2e job creates a fresh, empty directory under `$RUNNER_TEMP`, writable by the
    app container's user, mounts it into the app container at `/e2e-mail` and passes
    `E2E_MAIL_DIR=/e2e-mail`, and mounts the same directory read-only at `/e2e-mail` with
    `E2E_MAIL_DIR=/e2e-mail` into every Playwright container of the job (specs and capture). When
    the job fails, the directory's files are part of the existing `e2e-failures-*` artifact.
  - With `false` (every app that does not set it) the job is unchanged: no directory, no mount, no
    variable.
- spesix slice (same WO, spesix repo):
  - `backend/backend/e2e_settings.py` writes file mail to `E2E_MAIL_DIR` when it is set, otherwise
    to today's temp directory.
  - spesix `ci.yml` sets `e2e-mail-dir: true`.
  - A spec helper in `frontend/tests/` finds the newest mail to a given recipient (optionally
    matching a subject) and returns its links. It reads `E2E_MAIL_DIR` when set; locally, without
    it, it reads the app container's mail directory the way `reseed.js` reaches the container.
  - One smoke spec: requesting a password reset for a seeded user produces a mail to that user that
    contains a reset link.

## Scope

- The `e2e` job and its input in `app-ci.yml`; spesix `ci.yml`, `e2e_settings.py`, the helper and
  the smoke spec.

## Explicit non-goals / do-not-touch

- A mail-catcher service (decided: shared directory, matching the file backend apps already use; no
  new image).
- Other apps' `ci.yml`; any non-e2e job.
- Rewriting mail links: links are built from the app's configured frontend URL; a spec that opens one
  replaces scheme and host with the app's base URL and keeps path and query.
- The spesix coverage specs themselves (SPX-E2E-14, -20).

## Tier · precondition / gate

Tier 3: CI, in the shared workflow every app calls. No precondition. Ordering: lands before
SPX-E2E-14 and SPX-E2E-20 in spesix.

## Risks

- Ownership: the app container's user and the runner user differ; a directory the app cannot write
  turns every mail into a silent send failure in the app (file backend errors are swallowed when
  `fail_silently` is used). The smoke spec is what proves the write path.
- The read-only mount must hold for every Playwright invocation, including capture, so no spec can
  plant a mail.
- The input must stay a typed boolean; nothing from it is interpolated into a shell beyond the
  documented branch (WFT-CI-33).

## Required tests to WRITE

- `workflow-templates`: extend `.github/scripts/test_app_ci_e2e_job.py`: with the input unset the
  job contains no `/e2e-mail` mount and no `E2E_MAIL_DIR`; with it set, the app container gets a
  read-write mount and the variable, every Playwright `docker run` in the job gets a read-only mount
  and the variable, and the failure artifact includes the directory.
- spesix: extend `backend/test_e2e_settings.py`: the file-mail path follows `E2E_MAIL_DIR` when set
  and falls back to the temp directory when not.
- spesix: Vitest unit test for the mail helper (newest mail by recipient, subject filter, link
  extraction, host replacement keeps path and query).
- spesix: the password-reset smoke spec (desktop project).

---

# B. Implementation map — filled by the Orchestrator — ADDRESSED TO THE IMPLEMENTER

*Placeholder. The Orchestrator fills the context package, target directory, progress contract and
preamble on `git pull`. Do not dispatch while this placeholder stands.*

---

# C. Orchestrator only — NOT ADDRESSED TO THE IMPLEMENTER

> **If you are the implementer reading this work order as your own specification: STOP at this line.
> Everything below describes what the Orchestrator does AFTER you finish. You do none of it — no
> reviewers, no verification run, no register edit, no commit.** You ARE the invocation described
> below; do NOT shell out to `codex exec`.

*Placeholder. The Orchestrator fills the execution directive, review routing, verification, and
register + commit.*
