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

_To be filled by the Orchestrator (context package, preamble, progress contract) before dispatch._

# C. Orchestrator only — NOT ADDRESSED TO THE IMPLEMENTER

> Implementer: STOP here. Integration, verification, reviews and git belong to the Orchestrator.

Source: night audit 2026-10-17, S268; requirements round in session
`local_4987837b-7646-4b63-b3ab-1fd970f129bd`. A new tag needs the operator's approval of the version. Report
back to: `local_4987837b-7646-4b63-b3ab-1fd970f129bd`.

Scope amended at the operator's request on 2026-10-18: base-image freshness moved to WFT-CI-45;
runner files and local key handling remain transitional, superseded for key custody by WM-SEC-18.
Preserve the original Gate #1 history but use this reduced Envelope for the queued implementation;
do not run the old build-action task in parallel. No rollout dispatch was authorized by this amendment.
S268 cannot be closed by this WO alone. Record direct permission evidence and the remaining root-equivalent exposure.
