# WFT-CI-39 — Audit pnpm dependencies without installing global shims

## A. Envelope — operator-approved correction

Spesix TEST-5 recorded an intermittent shared security-job EACCES: `corepack enable` attempts to create `/usr/bin/pnpm` on a runner where the current user cannot change that location. The shared `pnpm audit (report-only)` step still executes that command before attempting the audit. Expected outcome: run the project-selected pnpm audit directly through Corepack without installing global shims.

Scope: replace this step's shim-install plus bare-pnpm invocation with `corepack pnpm audit --audit-level high`. Preserve the existing frontend lockfile guard, frontend-path working directory, warning fallback, step continue-on-error:true, and all other workflows/jobs/policies. No dependency or version changes, new CI inputs, permission changes or runner/prod operations. Tier3: shared CI/security audit. Operator explicitly approved this exact correction, targeted tests, independent Luna reviews and candidate CI in the Spesix chat on2026-10-14.

Required tests to write: extract and execute the live step with a stub Corepack that rejects `enable`, records forwarded arguments and returns controllable audit success/failure. Assert the audit is actually invoked for a lockfile, skips absent lockfiles, runs in the configured frontend directory, forwards pnpm/audit/high, and preserves the non-blocking warning on audit failure. A guard must reject reintroduction of shim installation. No real package installs, audits, network or system-directory writes in unit tests. Risk: silently skipping the audit while still reporting a green job.

## B. Implementation map — addressed to the implementer

Target absolute directory: `C:/Users/Micha Bigler/Documents/webapps/workflow-templates`.

Own ONLY `.github/workflows/app-ci.yml` within the named pnpm-audit step, and new `.github/scripts/test_pnpm_audit_corepack.py`. Read `.github/scripts/test_app_ci_security_gates.py` for the existing report-only policy and `.github/scripts/test_app_ci_e2e_job.py` lines1–55 for live-step extraction and Windows Git Bash selection. Workflow security job's audit step is around lines433–441. Existing script: lockfile conditional; corepack enable; `(cd <frontend-path> && pnpm audit --audit-level high) || echo <existing warning>`; continue-on-error:true. Preserve all expression syntax and literal warning text. Test supplied frontend-path via controlled expression substitution, never execute arbitrary GitHub expressions. Prefer existing unittest/stdlib patterns; no new testing dependency. The repository has no local AGENTS/CLAUDE/MEMORY and is not a joined frontend app. Root owns maps, register and CI proof refs.

### Required preamble / progress contract

This is the COMPLETE specification. Read root AGENTS.md for conventions only. Do not expand scope or change neighboring audit policy, dependency pins, runner state, shared-core or other app files. Do not edit docs, register, MEMORY or package/lockfiles. You share the checkout: foreign scratchpad must remain untouched; preserve others' edits. Explicit UTF-8 absolute file I/O, native workspace-root cwd bootstrap. Only named reads and assigned source/test edits; RUN NOTHING. Root executes the new tests and affected gate. No git/add/commit/push, installs/package commands, self-review/reviewers/agents, Docker, app startup, env/secrets or network. PLAN first, PROGRESS within two minutes, final RESULT:DONE|BLOCKED with changed files. Leave source/tests uncommitted.

## C. Orchestrator only

> Implementer STOP here. You do none of the work below and dispatch nobody.

Gate #1 is the explicit approval above. Fresh native configured codex/gpt-6-luna/high/fresh context, record identity and exposed effective metadata honestly. Foreign scratchpad excluded. Four independent configured Luna read-only lenses plus security concurrently; no UI lane. Root verifies audit still executes without privileged writes and unchanged report-only behavior. Execute new test file plus directly affected security-gate structural tests, excluding unrelated real tool-install tests if unavailable and recording exact scope.

Shared workflow remains workflow_call-only. Before any main push, use a workflow-templates ci-test/WFT-CI-39 proof ref and a Spesix candidate pointing to app-ci.yml@ci-test/WFT-CI-39. Wait until the current Spesix CI38641555651 is terminal; at most one in-flight proof per repo. Dispatch only the app's permitted ci.yml proof. Preserve all Spesix browser/capture assertions; root owns temporary consumer reference and restoration to main. Confirm main workflow trigger invariant before ref pushes. Required independent reviews precede candidate commits. Never push workflow main prematurely: every @main caller would consume it immediately. Restore the consumer reference after proof, publish only verified workflow tree, and delete own proof refs after landing. No production promotion or infrastructure dispatch.
