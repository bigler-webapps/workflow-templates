# WFT-CI-43 — Transitional runner file ownership and App-key hardening

# A. Envelope

**Goal.** Reduce direct access through the shared runner Unix account to runner executables,
registration configuration and the App key while off-host registration is introduced by WM-SEC-18.
This is transitional defence in depth for S268, not a security boundary against jobs with Docker/root access.

**Background (from the finding).** The runner tree, `bin/jit-config.sh` and `app.pem` (0600) belong to the runner user that all slots run as
(`ansible/roles/github_runner`), so a job can overwrite another slot's runner or JIT script and read the App
key (which can register a foreign runner with the deploy label).

**Scope.**
1. The github_runner role makes the runner binaries, their configuration and `jit-config.sh` owned by root and
   not writable by the runner user; the runner user keeps write access only where a job must write (work and
   diagnostic directories).
2. During the transition, the App private key is readable only by root and used by a fixed root-run step
   that hands each runner its scoped JIT config. Direct unprivileged file reads are denied; do not claim
   that a job able to reach host root cannot obtain the key. The helper exposes no arbitrary command or
   unrestricted token and leaks neither the key nor installation tokens in logs or job-readable files.
3. Retain root-owned runner executable/configuration files after WM-SEC-18 removes on-host key use.
   Provisioning must not recreate a local App key/helper after the off-host issuer becomes authoritative.
   Fresh base-image pulls and their build tests belong exclusively to WFT-CI-45.
4. Rollout through `ansible-provision` on the runners group (operator-confirmed dispatch), after WM-SEC-8,
   coordinated with WM-SEC-18 so no obsolete custody path is reintroduced.

**Residual (not fixed here).** All slots run as one Unix user; Docker access remains root-equivalent and
can bypass file ownership. Concurrent-job access and compromised build outputs remain S268 residuals.
The separate operations host is now the agreed programme (WM-INF-89/WM-SEC-17/WM-SEC-18), outside this WO.

**Non-goals / do not touch.** Build-action changes or their tests (WFT-CI-45); digest pinning of app
Dockerfiles; implementation of the separate host/issuer; unrelated WM-SEC-8 changes.

**Risks.** Runner self-update or JIT registration breaking once files are root-owned; mistaking this
transitional hardening for isolation from privileged jobs; a later provision restoring retired key delivery.

**Required tests (write as part of the implementation).**
- Role tests: rendered owner and mode of the runner binaries, `jit-config.sh` and the key; the key is used only
  by the fixed root step during transition; the issuer profile cannot reinstall the local key/helper.
- Post-rollout: direct reads/writes as the unprivileged runner account fail and CI registration still works.
  Use synthetic keys/permission probes, not real secret reads; this proves Unix permissions only, not Docker isolation.

**Target repo.** `workflow-templates`, branch `main` (rollout via `webapp-management`). **Tier 3** (CI,
credentials). Sequence: after WM-SEC-8.

# B. Implementation map — ADDRESSED TO THE IMPLEMENTER

## Context package and ownership

Baseline workflow-templates main 6fab525. Preserve the recorded 2026-10-17 approval and 2026-10-18 scope reduction; no renewed build task or new deployment dispatch is implied.

| Target file / function | Transitional assignment |
|---|---|
| ansible/roles/github_runner/tasks/main.yml: Ensure base directory, runner download/unpack, App private key, jit-config helper, per-slot initialization and service template tasks | Root-own executable/configuration files, constrain writable work/diagnostic paths and use only the fixed transitional privileged key step. Existing task ownership follows github_runner_user and is insufficient even against an ordinary sibling job. |
| ansible/roles/github_runner/defaults/main.yml | Explicit transitional profile and writable-path contract coordinated with WM-INF-89/WM-SEC-18. Off-host authority must not reinstall local key/helper. Preserve legacy CI capacity pending migration. |
| ansible/roles/github_runner/templates/jit-config.sh.j2: local PEM signing and token exchange; gh-runner@.service.j2: ExecStartPre/ExecStart/temp cleanup | Keep any transitional root invocation fixed and bounded; pass only JIT data to the unprivileged runner, not the App key/install token. Do not expose arbitrary privileged arguments or job-readable token files. Preserve PrivateTmp/cleanup and validate self-update behaviour under root ownership. |
| New ansible/roles/github_runner/tests/test_gh_runner_ownership.py; existing test_gh_runner_restart.py and test_gh_runner_slot_labels.py | Render ownership/mode and allowed writable paths; synthetic helper invocation/JIT fixtures, failed access probes, CI registration and off-host no-reinstall cases. Coordinate issuer-specific cases with WM-SEC-18 rather than duplicate its service implementation. |
| CHANGELOG.md | Unreleased transitional ownership note; release/version/tag is Orchestrator/operator work after the applicable approval. |

Key snippets: current jit-config.sh reads github_runner_base_dir/app.pem and signs an installation JWT; the runner unit executes it as github_runner_user. Current role adds Docker group and NOPASSWD:ALL. Root-only file access here proves Unix permissions, never isolation from root-equivalent CI jobs.

## Absolute target working directory

C:/Users/Micha Bigler/Documents/webapps/workflow-templates — main. Only the named role source/tests and changelog are owned. WM orchestrator handles collection pin, runbook and authorized provisioning.

## Invariants and dependencies

Sequence after WM-SEC-8's required proof; its first role wiring is landed but not live-accepted. Serialize shared-role edits with WM-INF-89, WM-SEC-18 and WM-RT-30. Do not edit publish-backend-image, deploy-app, their build tests, staging-health or foreign WFT-CI-44 work. Base-image freshness belongs solely to WFT-CI-45. Re-read updated role context before dispatch so transitional custody cannot supersede an already activated off-host issuer.

## Preamble — binding on the implementation instance

> The text above is the COMPLETE spec: this WO is not a plan to refine. Read the nearest AGENTS.md and CLAUDE.md, their mandatory named references, app MEMORY.md if present, the repo WORK_ORDERS.md (open WOs and highest ID), and the relevant role skill for applicable conventions and mandatory instructions. These context reads are required; they grant no register-edit or scope-expansion authority. Work from this context package; open the named files to verify it, without broad exploration. If an essential seam is missing or changed, report RESULT: BLOCKED with the missing context.
>
> Edit only the explicitly assigned implementation files for the dispatched batch: source, tests, configuration/workflows and required documentation. You are not alone in these checkouts: preserve others' edits and stop on overlapping foreign changes. Use explicit UTF-8 file I/O. Do not edit MEMORY.md or WORK_ORDERS.md; register initialization/updates and review verdicts belong to the Orchestrator. Do not change effective governance before its separately recorded approval.
>
> Perform only the read-only context reads expressly required above. Beyond those reads, tools are for editing those assigned implementation files and running the tests you wrote, once; nothing else. Git orientation and all repository operations belong to the Orchestrator. No dependency installation, package-manager commands, lockfile changes, repository administration, server access, provisioning, workflow dispatch, secret delivery, cleanup, staging or git add/commit/push. Report blocked repo state instead of repairing it. Never read or modify .env or private SSH configuration, print credentials, create an investigation marker, or perform destructive/rebuild actions.
>
> Write Part A's tests for this batch and run only your own new tests once. Do not run the affected/full suite, self-review, launch reviewers or claim a review verdict. Your run is not a gate; the Orchestrator owns independent review and the authoritative scoped run. Leave all edits uncommitted.

## Progress contract

Start with PLAN: <bounded steps>. Before each relevant action emit PROGRESS: [n/total] <present-tense action>, then report completion; use unbuffered output and leave no narration gap over two minutes. Finish with exactly one RESULT: DONE|BLOCKED <reason>, listing changed paths, own test command/result and unresolved acceptance evidence. DONE describes the assigned implementation batch only; it never asserts live deployment, credential revocation or programme closure.

# C. Orchestrator only — NOT ADDRESSED TO THE IMPLEMENTER

> **If you are the implementer reading this WO as your own specification: STOP at this line. Everything below is addressed only to the Orchestrator. Do none of it: no reviewers, verification gate, register edit or commit. You ARE the implementation instance; do not launch another implementer.**

Source: night audit 2026-10-17, S268; requirements round in session
`local_4987837b-7646-4b63-b3ab-1fd970f129bd`. A new tag needs the operator's approval of the version. Report
back to: `local_4987837b-7646-4b63-b3ab-1fd970f129bd`.

Scope amended at the operator's request on 2026-10-18: base-image freshness moved to WFT-CI-45;
runner files and local key handling remain transitional, superseded for key custody by WM-SEC-18.
Preserve the original Gate #1 history but use this reduced Envelope for the queued implementation;
do not run the old build-action task in parallel. No rollout dispatch was authorized by this amendment.
S268 cannot be closed by this WO alone. Record direct permission evidence and the remaining root-equivalent exposure.

### Dispatch and evidence contract (prepared 2026-10-18)

- No implementer has been launched for this preparation. Gate #1 for the new programme and effective WM-SEC-19 governance is pending; authoring permission is not implementation approval. Preserve older recorded approvals only within their unchanged/reduced Envelopes.
- After approval, use the configured implementation route, currently codex/gpt-6-luna, in a fresh instance with explicit model and this entire WO. Native bootstrap: inherited cwd is C:/Users/Micha Bigler/Documents/webapps; every tool command uses the assigned absolute target repo. Record accepted agent/process ID, batch/path ownership and requested model; exposed effective model/provider only, otherwise unverified. No worktree or feature branch.
- Reorient before each bounded dispatch, reload nearest governance/MEMORY/register, check overlap and refresh anchors. Future operations paths are new-file assignments, not evidence that a checkout exists. Establish its private main checkout and orient it before dispatch; otherwise block that batch. Read-only reference repositories are not mutation authority.
- Implementation review uses the current review and sec_review routes from .claude/models.local.json (example defaults for absent keys): run all configured general lenses concurrently on the same assembled diff, with the configured security reviewer in the same background batch. At preparation these routes are codex/gpt-6-luna and the general lenses are envelope, regression, duplication and tests; this snapshot does not override later config. No UI review is indicated by these infrastructure maps. Follow review_fallback only for evidenced unavailability. Reviewers are fresh, read-only instances, static-only. Separate authoring reviews do not establish implementation Gate #2.
- The Orchestrator checks its own security/exposure paths while reviews run, fixes in-scope findings, repeats required reviews on corrections, then runs only the union of affected/directly-dependent tests including relevant security tests. No implementation test or live proof is claimed here. Commit/push only on clean required review and scoped tests, stage own paths explicitly, and preserve dispatch/release/promotion/infra gates.
- Keep statuses planned until actual implementation starts. Record authoring/map commits separately from implementation commits; never mark done from a map or first success. Persist operator approval wording/date/scope in the register only after it arrives. Preserve the canonical finding history and require implementation plus effective-consumer/live evidence per finding.

### Approval preservation and scoped gate

The original Gate #1 and operator's 2026-10-18 reduction remain the only implementation approval for this older WO; this mapping creates no new approval. No shared-role implementation or rollout was performed. Recheck WM-SEC-8's staging/concurrent-join proof before the queued rollout. Record independently reviewed release/pin and explicit version/dispatch authorization separately.

Authoritative scoped run after implementation: python -m pytest ansible/roles/github_runner/tests/test_gh_runner_ownership.py ansible/roles/github_runner/tests/test_gh_runner_restart.py ansible/roles/github_runner/tests/test_gh_runner_slot_labels.py -q. Permission probes use synthetic files/data; no real key read and no claim that root/Docker cannot bypass permissions. No build-action tests belong to this WO.
