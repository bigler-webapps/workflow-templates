# WFT-CI-45 — Release builds separated from trusted prebuilt deployment

## A. Envelope

**Goal.** Make a release build on the existing CI runner produce an immutable, verifiable output that the separate operations runner can deploy without building images or executing source-repository code locally. Preserve supported deployment behaviour and existing consumers until their deliberate migration.

**Target repo.** `workflow-templates`, branch `main`. **Tier 3**: shared CI/CD and production credentials. Consumers migrate through `webapp-management/WM-INF-90`; trusted authorization is WM-SEC-17.

### Scope and contract

1. Split the release-build and deployment responsibilities at the existing `publish-backend-image` / `deploy-app` seams. A CI release build checks out an exact source SHA, builds/pushes with freshly pulled base images and only its own repository/package publishing authority. It receives no runtime secret, deploy/provision Tailscale credential, operations dispatch token or runner-registration key.
2. Publish a versioned, bounded manifest identifying source repository, full commit SHA, release workflow run ID/attempt, source ref and immutable `image@sha256:...` output in the expected image namespace. Read the pushed registry digest rather than trusting a tag or a CI-supplied digest string alone. Bind artifact retrieval to the originating run/attempt. No runtime credentials in outputs or artifacts. WM-SEC-17 independently authorizes the run and source; the manifest is evidence to validate, not an authority.
3. Add a strict prebuilt deployment mode. It requires a validated digest and checked source identity, uses that exact image and fails closed on missing/inconsistent build output. Neither the operations host nor the target host silently rebuilds as a fallback. Target-side image pull, service restart, migrations and health checks retain their existing behaviour. Build-only completion is not deployment success.
4. Support the estate's static/no-backend deployment shape explicitly: an immutable, bounded static output plus approved configuration, bound to source/run/hash, without a dummy backend image. Asset extraction must reject traversal, absolute paths, symlinks/hardlinks and overwriting trusted control files. Existing prebuilt third-party images remain pinned according to their deployment contract.
5. Operations mode runs with pre-provisioned tools and a configured host Tailscale identity, with no unrestricted sudo or Docker socket. Keep the existing per-job Tailscale join path for unmigrated callers; the new mode must not silently fall back to the old OAuth path. Mode selection and target routing come from trusted operations policy, not app artifacts.
6. Source files transferred to a target are not sourced/executed on the operations host. Use trusted parsers, validate project name/target/path and reject local hooks/actions or application setup commands in the privileged job. Credentials are scoped to the target app/environment. Preserve WFT-CI-42's private temporary directory, restrictive modes, atomic remote secret transfer and cleanup on all exit paths.
7. Publish an explicitly versioned contract/action release. Preserve old pinned consumers during rollout; remove their legacy paths through WM-INF-90 rather than force a global unproved switch. No new GitHub-hosted job per application deployment.
8. Use explicitly versioned pre-provisioned deployment tooling, including the dcm deployment CLI dependency, instead of open-ended `pip install` constraints. The hosted break-glass mode consumes the same reviewed version specification and strict prebuilt contract; it may establish its own narrowly scoped network identity but cannot fall back to legacy CI credentials or build behaviour. Do not modify dcm internals. WM-INF-91 owns adoption in Traefik/other operational consumers.
9. Remove unconditional full `tailscale status`/`netcheck` dumps from ordinary deployment logs. Keep bounded, redacted diagnostics that identify the failed connection without disclosing the entire tailnet; moving logs into a private repository alone is not the fix.
10. Validate project/target/path fields as complete scalar values before environment-file, rsync or remote-shell use. Reject newline/environment-output injection, shell metacharacters and out-of-policy paths; pass data without constructing executable commands from app fields. Static mode neither requests package-write authority nor forwards unnecessary registry credentials; preserve only justified read-only registry access.

### Dependencies, risks and non-goals

Coordinate with WFT-CI-43: this WO owns fresh-base-image behaviour in release-build actions; its root-owned-runner-file work remains separate, and WM-SEC-18 owns moving registration authority off CI. Do not duplicate concurrent edits. Preserve WFT-CI-42 and the approved WFT-CI-44 health separation. No changes to app features, dcm/ucm internals, PR test semantics, package publishing or customer placement.

Digest binding prevents substitution after selection but does not certify an honest build on a compromised CI host. Shared Docker cache/UID risks remain documented under S268. Changes to release contract, remote compose handling, static deployment and secret cleanup can break all consumers; rollout is additive then per-app, not a blanket pin bump.

### Required tests to write

- Execute build/manifest logic with stub registry clients: correct pushed digest, wrong/missing digest, mismatched repository/SHA/run/attempt, immutable output and no fixture secret in logs/artifacts. Verify fresh-base-image request.
- Strict prebuilt mode rejects missing output and never calls a build path locally or remotely; image digest reaches the actual compose/deployment command rather than only a summary.
- Static output validation rejects traversal, links, oversized data and metadata substitution; valid static deployment stays supported.
- Pre-provisioned operations mode uses no unrestricted sudo, Docker socket, per-job deploy OAuth or caller-local executable; legacy pinned mode retains its existing contract until migrated.
- Existing deploy secret cleanup and failure/cancellation tests stay green; add affected negative cases for the new paths. Candidate staging proofs cover one image-based app and the static app, including health failure reporting.
- Toolchain checks reject floating deploy dependencies; both operations and hosted break-glass modes use the same version specification with no legacy build fallback.
- Malicious project names/paths containing CR/LF, environment-output syntax or remote-shell syntax are rejected before transfer; valid allowed identifiers still deploy. Static mode has no unnecessary write/token handoff.
- Successful and failed connection fixtures disclose neither full tailnet topology nor synthetic secrets in diagnostics.

## B. Implementation map — ADDRESSED TO THE IMPLEMENTER

Not dispatch-ready. The Orchestrator fills the exact context package, preamble and progress contract before dispatch. Absolute repo root: `C:/Users/Micha Bigler/Documents/webapps/workflow-templates`.

Anchors: `.github/actions/publish-backend-image/action.yml` (`existing_image`, image naming and build/push); `.github/actions/deploy-app/action.yml` (inputs, tool installation, Tailscale, runtime configuration, rsync and remote compose); `.github/actions/tailnet-connect/action.yml`; `.github/scripts/test_deploy_app_action.py`; `work-orders/WFT-CI-42.md`, `WFT-CI-43.md`, `WFT-CI-44.md`. Consumer examples, read only: `survey_app/.github/workflows/main.yml`, `yopoulab-hhs-2026/.github/workflows/main.yml`. This WO does not own their migration.

## C. Orchestrator only — NOT ADDRESSED TO THE IMPLEMENTER

> Implementer: STOP here. Integration, verification, reviews and git belong to the Orchestrator.

Authored 2026-10-18 for the agreed dedicated 2 GB operations-host programme. Implementation not started. Follow current role/review gates; choose the shared release version through the applicable operator approval mechanism, not a version invented here. Review the combined contract, action and test diff independently, including security.

Ownership resolved with the operator on 2026-10-18: WFT-CI-43 retains transitional root-owned runner files/key use; all fresh-base-image build behaviour and its tests belong here. WM-SEC-18 replaces on-CI App-key custody. WFT-CI-44 has foreign WIP at authoring time and is not edited by this WO. Candidate proof requires the shared candidate and an app caller pointing to that candidate; dispatch approval and subsequent production promotion retain their own gates. Record residual S268 image-build risk accurately: `--pull` mitigates stale/retagged bases but does not establish build integrity against a root-equivalent CI attacker.

Finding closure: map S231, S288 and S304 to actual action paths and consumer adoption; coordinate S409 with WM-INF-90 and the remaining S288 operational paths with WM-INF-91. Do not close while production callers still select the vulnerable action. WM-INF-90 owns break-glass authorization/rollout; this WO supplies its reusable deployment mechanics.
