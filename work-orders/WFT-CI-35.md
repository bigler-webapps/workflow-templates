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

Two slices in two repos, no shared files: **B.1** (workflow-templates) and **B.2** (spesix). Each is its own
dispatch, started with a role line (`ROLE: workflow` / `ROLE: spesix`); implement only your slice. Work from
this package; do not explore broadly. If you must dig deeper, delegate to a read-only Explore sub-agent (Haiku).

## Interface contract (both slices)
- Mount point in every container: `/e2e-mail`; variable `E2E_MAIL_DIR=/e2e-mail`.
- Mail file format: Django `filebased.EmailBackend` writes one `.log` file per message (raw RFC 2822 text
  with headers `To:` and `Subject:`; body may be quoted-printable or base64 encoded). The spesix helper
  must decode with a real MIME-aware decoder, not by regex over raw text.

## B.1 Workflow slice — repo `workflow-templates`, branch `main`, cwd `C:\Users\Micha Bigler\Documents\webapps\workflow-templates`

Files: `.github/workflows/app-ci.yml` (inputs block ~l.147-165; job `e2e` l.485+), `.github/scripts/test_app_ci_e2e_job.py`.

- Add input `e2e-mail-dir` (`type: boolean`, `default: false`) beside the other e2e inputs. Any description
  text must contain no GitHub expression syntax.
- New step in `e2e`, `if: ${{ inputs.e2e-mail-dir }}`, placed before "Start app": create
  `"$RUNNER_TEMP/e2e-mail"` (fresh, empty), `chmod 0777` it (the app container's user differs from the
  runner's), and append to `$GITHUB_ENV`: `E2E_MAIL_HOST_DIR=<dir>`, `E2E_MAIL_APP_ARGS`
  (`-v <dir>:/e2e-mail -e E2E_MAIL_DIR=/e2e-mail`) and `E2E_MAIL_PW_ARGS` (`-v <dir>:/e2e-mail:ro -e
  E2E_MAIL_DIR=/e2e-mail`). When the input is false the step does not run and none of these variables
  exist. Nothing from workflow inputs is interpolated into this shell (boolean only; WFT-CI-33).
- Pass `$E2E_MAIL_APP_ARGS` unquoted (intended word splitting; runner temp paths contain no spaces) to the
  "Start app" `docker run`, and `$E2E_MAIL_PW_ARGS` to EVERY Playwright-image `docker run` of the job:
  install dependencies, the gating specs loop, the quarantine loop, the capture cell enumeration, the
  capture loop. An unset variable expands to nothing, so the false case renders the same commands as today.
- Failure artifact: the directory's files must land in `${{ inputs.frontend-path }}/test-results/e2e-mail/`
  before "Upload E2E failure artifacts" (a new step with the same `if:` as "Save app container log for
  failed specs", additionally guarded on the input; `cp -a "$E2E_MAIL_HOST_DIR/." <dest> || true`).
- The Cleanup step also removes the directory when the input is true.
- Tests (extend `test_app_ci_e2e_job.py`, same text-inspection style): input type and default; the setup
  step is gated by the input and is the only place that builds the `/e2e-mail` mount; outside it the job
  references only the variables; "Start app" references `E2E_MAIL_APP_ARGS`; every `docker run` using the
  Playwright image references `E2E_MAIL_PW_ARGS` (count against the number of Playwright-image
  `docker run` occurrences, so a new invocation without the mount fails) and no `:/e2e-mail` mount other
  than the setup step's `:ro` and the app's exists; the failure-artifact copy step exists. Run only this
  file: `python .github/scripts/test_app_ci_e2e_job.py`.

## B.2 spesix slice — repo `spesix`, branch `develop`, cwd `C:\Users\Micha Bigler\Documents\webapps\spesix`

Another session works in this tree: touch ONLY the files listed here.

- `backend/backend/e2e_settings.py:24`: `file_path` = the `E2E_MAIL_DIR` env value when set and non-empty,
  else today's `<tempdir>/spesix_e2e_mail`.
- `backend/backend/test_e2e_settings.py` (it lives in `backend/backend/`): add cases for env set and unset.
  The module reads env at import, so reload it (`importlib.reload` with `monkeypatch.setenv/delenv`) and
  restore it afterwards so other tests are unaffected. The existing assertion at l.19 stays valid for the
  unset case. Run it with cwd `backend` using that file's path.
- `.github/workflows/ci.yml`: add `e2e-mail-dir: true` under `with:` next to the other e2e keys, trailing
  comment `# WFT-CI-35`. Nothing else in that file; do not change `@main`.
- `frontend/tests/mail-helper.js` (new): export `findMail({ to, subject, dir })` returning the newest message
  file (mtime, ties by name) whose `To:` contains `to` (case-insensitive) and, when given, whose subject
  contains `subject`; `extractLinks(message)` returning de-duplicated http(s) URLs from the decoded text body
  (trailing punctuation trimmed); `toBaseUrl(link, baseUrl)` replacing scheme and host with `baseUrl` and
  keeping path and query; `waitForMail(...)` polling (~250 ms, up to ~10 s, then throwing with the recipient
  in the message). Directory resolution: `dir` argument, else `process.env.E2E_MAIL_DIR`, else the app
  container's mail directory reached the way `reseed.js` reaches it (`docker exec spesix_local-backend-1`;
  local only: `docker cp` `/tmp/spesix_e2e_mail` into an OS temp dir, then read it). Use `node:fs`,
  `node:path`, `node:os`; decode quoted-printable/base64 and unfold headers with a small own decoder, or an
  existing dependency if `frontend/package.json` already has one. Do NOT add a dependency.
- `frontend/tests/mail-helper.test.js` (new, Vitest): message files in a temp dir; cases: newest wins,
  recipient filter, subject filter, none found, quoted-printable body with a soft-wrapped link, link
  de-dup, `toBaseUrl` keeps path and query. Check how `reseed.test.js` is picked up by the Vitest config and
  follow it. Run only this file, cwd `frontend`.
- `frontend/tests/password-reset-mail.spec.js` (new; mirror `login-access.spec.js`, `reseedE2E` in
  `beforeAll`): desktop project only (skip on mobile via `testInfo.project.name`); open
  `/reset-request-password`, fill `e2e-submitter@example.com`, submit, `waitForMail({ to })`, assert a link
  whose path matches `/reset/<uid>/<token>`. Read the rendered field type and labels from
  `PasswordResetRequestPage` in `frontend/src` first; do not guess selectors. Do not run this spec (it needs
  the CI harness); the Orchestrator proves it in CI.

## Preamble

The text above is the COMPLETE spec, the committed WO file's content, not a plan to refine. Read the nearest
`AGENTS.md` and the app `MEMORY.md` ONLY for conventions. Stay in scope; do not touch auth/permissions/deps/
schema/CI beyond what the spec says; do not update `MEMORY.md`. **Do NOT edit `WORK_ORDERS.md`; the register
row and review verdicts are the orchestrator's alone.** Your tools are for editing source and test files and
for running the tests you wrote, nothing else. Do NOT install dependencies, touch a lockfile, run a package
manager, or tidy up stray files; if repo state blocks you, stop and report `RESULT: BLOCKED <reason>`. Do NOT
`git add`/`commit`/`push`; leave every change uncommitted for the orchestrator's independent review. WRITE the
tests the `Required tests` section calls for AND RUN the tests you just wrote once; that is your only test run
(not the affected/full suite, not any review). The orchestrator's run is the gate.

Narrate continuously: a `PLAN: <step1> | <step2> | ...` line up front, then a single-line
`PROGRESS: [<n>/<total>] <present-tense action>` before every relevant action (and `... done` on completion),
spaced so no gap exceeds ~2 min, plus exactly one final `RESULT: DONE|BLOCKED <reason>`.

---

# C. Orchestrator only — NOT ADDRESSED TO THE IMPLEMENTER

> **If you are the implementer reading this work order as your own specification: STOP at this line.
> Everything below describes what the Orchestrator does AFTER you finish. You do none of it — no
> reviewers, no verification run, no register edit, no commit.** You ARE the invocation described
> below; do NOT shell out to `codex exec`.

## Execution directive

Two `codex exec` runs, one per repo, concurrently, `-m gpt-6-luna`, WO on stdin, banner read back:
`ROLE: workflow - implement only B.1` (cwd workflow-templates) and `ROLE: spesix - implement only B.2`
(cwd spesix). Each stdin is the role line followed by this file. Fallback: direct Claude implementation per
AGENTS.md (flips authorship).

## Review routing

Tier 3 (CI). Lenses envelope/regression/duplication/tests on both diffs inline in one batch, plus
`sec_reviewer` (shared workflow). Own security pass: no input reaches a shell; the read-only mount holds for
every Playwright run.

## Verification

Local: `python .github/scripts/test_app_ci_e2e_job.py`; spesix pytest for e2e_settings; Vitest for the helper.
CI proof: candidate on `ci-test/WFT-CI-35` in workflow-templates; spesix `ci.yml` on `ci-test/WFT-CI-35` of
spesix pointing at `app-ci.yml@ci-test/WFT-CI-35`; the app-side run must be green including the reset-mail
spec. Landing order: workflow-templates `main` first, spesix `develop` second.

## Register + commit

Row WFT-CI-35 in the workflow-templates `WORK_ORDERS.md`; commit only own files; landing commit, then
register commit with SHA, one push per repo. Delete both `ci-test/WFT-CI-35` refs after landing.
