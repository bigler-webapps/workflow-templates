# WFT-CI-23 — Give `app-ci.yml` the security gates two apps had to build themselves

## Part A — Envelope

### Goal

`app-ci.yml` can run a blocking dependency audit and a static analyser, opt-in per caller, so an app
no longer has to add a second job to get them — and no existing caller's behaviour changes.

### The gap, measured

`app-ci.yml`'s `security` job runs:

| | |
|---|---|
| gitleaks | **blocking** |
| pip-audit | **report-only** (`continue-on-error: true`, findings become a warning) |
| bandit | not present |
| ruff | not present anywhere in the workflow, backend job included |

Two apps compensated independently:

- **Gustav** — `pip-audit -r backend/requirements.txt` (blocking) and
  `bandit -q -r backend/apps -c backend/pyproject.toml`, Python 3.14, tools unpinned.
- **Photogallery** — `ruff check backend` and `bandit -r backend`, Python 3.12,
  `ruff==0.15.20 bandit==1.9.4` pinned.

Photogallery's own comment states the stake: dropping bandit for the sake of a uniform pipeline would
be *"a regression dressed up as consolidation"*. That is right, and it is why the fix belongs here
rather than in the apps.

### The blast radius is the design constraint

**Nineteen repos call `app-ci.yml`** — Gustav, Kira, Photogallery, cinevia, bigler-consult, cockpit,
fitness-monitor, hpc-bridge, hram, innoservice, jg-ferien, kerzenziehen, musiknoten, reimbursements,
spesix, survey_app, survey_contact_app, webshop-guenter, webapp-template.

A new gate that is blocking by default turns every one of them red in the same hour, on findings
nobody has triaged. **Every addition here is opt-in and defaults to today's behaviour.** A caller
that changes nothing must see no change at all — that is the acceptance criterion, not a nicety.

### Scope

New `workflow_call` inputs on the `security` job, each defaulting to current behaviour:

- `run-bandit` (boolean, default `false`), with `bandit-path` (default `backend`) and
  `bandit-config` (default empty, passed as `-c` when set).
- `pip-audit-blocking` (boolean, default `false`) — when true, drop `continue-on-error` and let the
  audit fail the job.
- `run-ruff` (boolean, default `false`) with `ruff-path` (default `backend`).
- `security-python-version` (string, default matching what the job uses today) — callers run 3.12 and
  3.14.

**Pin the tool versions in this workflow**, the way Photogallery does and Gustav does not. An
unpinned linter silently changes what CI enforces; for a gate shared by nineteen repos that is a
fleet-wide behaviour change nobody authored. Renovate keeps the pins moving.

### Non-goals

- **No default flipped.** Not one of the nineteen callers changes behaviour from this WO alone.
- No change to gitleaks, to the `backend`/`frontend` jobs, or to any other input.
- No adoption in the callers — that is `WM-TAKE-8`.
- No opinion on ruff-versus-bandit scope per app; the inputs carry it.

### Tier

**3** — CI, and shared by nineteen repos.

### Risks

- **A default that is not actually the current behaviour.** The whole safety argument rests on this;
  it is worth verifying against a real caller rather than reading the diff.
- `pip-audit-blocking` is the one input that turns an existing warning into a failure. It is opt-in,
  but the app enabling it should expect its first run to be red — that is the finding, not a bug.
- Adding a Python setup step to a job that currently installs pip-audit inline may change tool
  resolution. Check that pip-audit still runs the same way when the new inputs are all off.

### Tests

The gate is a dispatch, because a reusable workflow's behaviour cannot be reproduced locally:

1. **Unchanged-caller proof.** One consumer that sets none of the new inputs runs green and its
   security job's step list is identical to before. This is the important one.
2. Each new input exercised once, on and off.
3. A deliberately failing case for `run-bandit` and `pip-audit-blocking` — a gate that cannot be shown
   to fail is decoration.

Test procedure, within the branch rules: push the change to this repo's `develop`, then point ONE app's
`ci.yml` at `app-ci.yml@develop` on a throwaway `ci-test/<ID>` ref (`AGENTS.md`, exception (b)). Neither
the app's `develop` nor this repo's `main` is touched until the evidence exists.

---

## Part B — Implementation map

### Files

- `.github/workflows/app-ci.yml` — the `workflow_call.inputs` block, and the `security:` job
  (currently line 276 onward; the pip-audit step with its `continue-on-error: true` is around 321-328).

### Reference for what the callers need

- `Gustav/.github/workflows/ci.yml` — its `python-security` job.
- `Photogallery/.github/workflows/ci.yml` — its `python-security` job, and the comment explaining why
  it exists.

Both are the requirement, not a suggestion: after `WM-TAKE-8` those jobs must be deletable with
nothing lost.

### Progress contract

`PLAN: …` · `PROGRESS: [n/total] <action>` · one final `RESULT: DONE|BLOCKED <reason>`.

---

## Part C — Orchestrator only

*Stop line.*

### Review

Tier 3, independent, plus `sec_reviewer`. The security reviewer's question is narrow and specific:
**can any of the nineteen existing callers behave differently after this change?** Defaults, not
features.

### Register

`WFT-CI-23`. First ID in this register carrying the repo token — `AGENTS.md` requires it for new IDs
because `CI-*` is shared with `webapp-management`; the twenty-two existing bare rows stay as they are.
The Notiz records the unchanged-caller proof and which consumer it ran against.

### Commit

`develop` first (the test path needs it there), `main` once the evidence exists.

### Ordering

Before `WM-TAKE-8`, which consumes these inputs.
