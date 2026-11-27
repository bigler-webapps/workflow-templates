# WFT-CI-59 — Safe HRAM chat catalogue cutover during deployment

Companion to `hram/work-orders/HRAM-CHT-11.md`; the operator requested resolution of its deployment activation blocker on 2026-11-26.

## A. Envelope

**Tier 3, infrastructure lane.** Provide the narrowly scoped deployment sequencing required for the existing HRAM chat permission rollout, for both the prebuilt staging path and the legacy production action. Preserve the current behavior for other applications and ordinary subsequent deploys. No new product feature or production promotion is included.

Before the initial chat catalogue and legacy cohort are activated, all incompatible old application authorization writers must be stopped and kept stopped. Migrations must complete first; the activation must use one nonempty immutable deployment identity shared by the new application processes. The new application must not serve or write until activation succeeds. Fail closed on ambiguous inventory, missing configuration, failed migration or activation; a retry must not widen grants. Never accept arbitrary commands or executable content from a release artifact. Account for existing recovery admission, image verification, durable failed-deployment state and rollback behavior.

Tests must demonstrate the order of drain, migration, activation and start, no activation on failed prerequisites, safe repeated/failed attempts, unchanged non-HRAM deployment behavior and relevant security and recovery invariants. The orchestrator coordinates the required HRAM integration and installation of the reviewed staging SDK with `hram/HRAM-CHT-11`; managed-server applies need their own named operator approval per call.

## B. Implementation map

- `workflow-templates` main; staging owner `.github/scripts/prebuilt_target.py` and tests `.github/scripts/test_prebuilt_target*.py`.
- Legacy owner `.github/scripts/guarded_deploy.py`, `.github/actions/deploy-app/action.yml`, and `.github/scripts/test_stack_recovery_callers.py`.
- Relevant contracts: `.github/scripts/prebuilt_target_entry.py`, `.github/scripts/prebuilt_target_transport.py`, `docs/prebuilt-target.md` when present; recovery startup gate is normally open.
- HRAM composes backend, celery worker and beat; staging launcher and immutable SDK pin are managed in `webapp-management`; production uses the legacy action pinned by HRAM.
- HRAM activation implementation is `hram/backend/application/assistant/cohort_cutover.py`; it expects PostgreSQL, migrations and a consistent deployment identity.

## Preamble

> You are the dispatched implementer. Run `codex-hook-canary-ws-gov-60` first. Continue on its expected denial, otherwise report unverified coverage. Read this work order, the HRAM companion and relevant infrastructure governance. Implement only the assigned shared deployment portion and focused tests; preserve foreign changes. Do not access servers, credentials or `.env`, change other repositories, dependencies, the register or work orders, delegate, review, use Git, or run tests. Report changes and unresolved blockers.

## C. Orchestrator only

Coordinate design, reviews, tests, installation and acceptance with HRAM-CHT-11. No production activation or promotion is authorized by this order.

## Gate record

Gate #1 approved by the operator 2026-11-26 (request to resolve HRAM-CHT-11 deployment activation and isolated acceptance blockers).
Gate #1 clarification approved by the operator 2026-11-27: after HRAM activation recover forward with compatible code only; old images with incompatible authorization writers must not start against the activated database. Reverting to an old image is permitted only together with a verified restoration of the matching database state and requires a separate operator approval.

## Completion

Blocked: configured implementer `codex/gpt-6.1-terra` identified that neither deployment path currently provides a durable, recovery-compatible cutover drain. A fixed HRAM management command has been prepared, but its safe use still needs an authoritative writer inventory, a trusted stable rollout identity, interruption and retry handling, and a rollback rule that cannot restart incompatible old writers after activation. The staging target SDK is installed through webapp-management and needs a separately approved named apply after a reviewed implementation and pin update. No deployment implementation, review, test, push or activation has been completed in this companion order. The implementer reported unverified hook coverage in its restricted workspace. Part B: errors: the recovery and trusted target policy files also require mapping before implementation.

2026-11-27 read-only follow-up: `stack_recovery.RecoveryTransaction` already persists a blocked startup gate and stops its recorded services, but the prebuilt deployment requires that gate open and the legacy deployment uses an open gate; neither integrates activation. The stopped-app migration API uses the recorded Compose configuration, not necessarily the new released image. The trusted staging policy identifies backend, celery_worker and celery_beat as released-image services; this repository inventory alone does not establish the absence of outside writers. A safe implementation needs a durable journal and a verified drain, migrations and the fixed HRAM activation command from the verified released image, one rollout identity shared by those commands and new services, and a gated release after success. Interrupted runs must remain blocked and be reconcilable. The rollback choice after activation remains an operator decision: prohibit incompatible old images and recover forward with compatible code, or permit rollback only with an independently verified database rollback and restoration procedure. In both cases, old authorization writers must not run against the activated state. `workflow-templates`, `webapp-ops-scripts`, HRAM and the webapp-management staging SDK/policy are affected; no deployed state was inspected or changed.

2026-11-27 after operator decision: configured implementer `codex/gpt-6.1-terra` prepared an unwired fixed coordinator and tests, with environment-specific HRAM project validation. The `app-ci-e2e` local scope passed 113 existing tests (two skipped) and four selected coordinator tests; the recorded scope gate is green across those trees. This does not exercise an actual deployment or recovery fence. OPS-REC-6 supplies only an inert refusal because the trusted complete writer inventory and compatibility enforcement across all restart, recovery and restore paths are unresolved. No coordinator integration, independent review, managed-server apply or activation has occurred. Implementers reported the canary command unavailable in their restricted target roots, so hook coverage there was unverified.
