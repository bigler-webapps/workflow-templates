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
