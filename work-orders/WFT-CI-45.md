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

## Context package and ownership

Baseline: workflow-templates main 6fab525. WFT-CI-43's revised Part A excludes all build changes; WFT-CI-44 and its staging-health workflow/test are foreign WIP and outside this assignment.

| Target file / function | Assignment and existing seam |
|---|---|
| .github/actions/publish-backend-image/action.yml: publish step, existing_image branch, image output | Today docker build lacks --pull; existing_image can bypass rebuilding, and output is a tag after docker push. Produce Part A's fresh-build and registry-resolved immutable digest/manifest; an unproved reused image cannot silently satisfy freshness. Keep legacy pinned consumers supported. |
| .github/actions/deploy-app/action.yml: inputs; Validate caller-input strings (188); Install tooling (232); Calculate configuration (244); Generate .env (375); Rsync project (412); runtime-secret transfer (450); Pull/build, restart & migrate (536); cleanup (741) | Add explicit strict prebuilt/operations and hosted-recovery contract; preserve legacy mode while callers migrate. Ensure validated digest reaches actual target compose service and remove all strict-mode local/remote build fallback. Static is an explicit independent shape. |
| New .github/scripts/release_manifest.py and test_release_manifest.py; new docs/release-deployment-contract.md | Bounded/versioned image or static manifest; exact repo/full-SHA/run/attempt/ref/output identity, archive limits and no secrets. Build evidence is not deploy authorization: WM-SEC-17 independently checks GitHub state. |
| New .github/scripts/prebuilt_deploy.py and test_prebuilt_deploy.py | Trusted parsers and complete-scalar/project/host/path validation; safe static archive handling and transfer boundary without running source hooks/actions/setup on controller. Reuse the shared deploy mechanics, do not create unrelated app-specific deploy code. |
| New toolchains/deploy-requirements.txt; docs/release-deployment-contract.md toolchain section; action setup branches | Select/test exact supported dcm CLI and Python dependency versions without changing dcm internals. Operations validates pre-provisioned tools; hosted emergency setup consumes the same pinned specification. No open range or package install in an ops job. WM-INF-89 host toolchain consumes this versioned specification. |
| .github/scripts/test_deploy_app_action.py and affected adjacent deploy cleanup tests; new test_release_build_action.py | Exercise commands with stub clients, assert pushed digest binding, fresh base requests, no strict build fallback and image reaching remote compose; synthetic secrets only. Preserve WFT-CI-42 temp/cleanup contract. |
| .github/actions/tailnet-connect/action.yml (read-only baseline unless an essential shared bounded diagnostic seam is changed) | Legacy per-job join remains for unmigrated callers; operations uses its configured identity and never falls back to CI OAuth/sudo. Do not change the foreign health workflow. |
| CHANGELOG.md, action release/version metadata | Orchestrator-owned version selection/release and pins after applicable approval; implementer does not release/tag or bump consumers. |

Key snippets: deploy-app currently installs django-core-micha>=2.17.5, logs full tailscale status/netcheck and permits use_build_fallback; publish-backend-image outputs image=<namespace>:<tag>. Existing grep line validation is not whole-scalar validation against CR/LF. Replace these in the new contract without declaring old pinned callers migrated.

Read-only consumer shapes: C:/Users/Micha Bigler/Documents/webapps/survey_app/.github/workflows/main.yml publishes then deploys at v2.13.0; yopoulab-hhs-2026/main.yml skips backend publishing, selects caddy:2-alpine and skip_migrate. Preserve supported static behaviour; S410 runtime pinning is not silently claimed here.

## Absolute target working directory

C:/Users/Micha Bigler/Documents/webapps/workflow-templates — main. Consumer edits belong to WM-INF-90. No github_runner ownership/key work here and no dcm/ucm internal changes.

## Contract integration and pitfalls

Coordinate manifest fields/limits with WM-SEC-17 before either consumer activates; reject invalid identity before secret acquisition or target contact. Share one deployment/toolchain contract for ordinary and hosted break-glass. Candidate proof needs shared candidate plus an app caller pointing to it and separately authorized dispatch; source app migrations are not this implementer's assignment. Static mode grants no package write/unneeded registry token. Minimal connection diagnostics must be bounded/redacted in success and failure. Digest/fresh-base pulls do not certify honest builds on root-equivalent CI.

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

## C. Orchestrator only — NOT ADDRESSED TO THE IMPLEMENTER

> **If you are the implementer reading this WO as your own specification: STOP at this line. Everything below is addressed only to the Orchestrator. Do none of it: no reviewers, verification gate, register edit or commit. You ARE the implementation instance; do not launch another implementer.**

Authored 2026-10-18 for the agreed dedicated 2 GB operations-host programme. Implementation not started. Follow current role/review gates; choose the shared release version through the applicable operator approval mechanism, not a version invented here. Review the combined contract, action and test diff independently, including security.

Ownership resolved with the operator on 2026-10-18: WFT-CI-43 retains transitional root-owned runner files/key use; all fresh-base-image build behaviour and its tests belong here. WM-SEC-18 replaces on-CI App-key custody. WFT-CI-44 has foreign WIP at authoring time and is not edited by this WO. Candidate proof requires the shared candidate and an app caller pointing to that candidate; dispatch approval and subsequent production promotion retain their own gates. Record residual S268 image-build risk accurately: `--pull` mitigates stale/retagged bases but does not establish build integrity against a root-equivalent CI attacker.

Finding closure: map S231, S288 and S304 to actual action paths and consumer adoption; coordinate S409 with WM-INF-90 and the remaining S288 operational paths with WM-INF-91. Do not close while production callers still select the vulnerable action. WM-INF-90 owns break-glass authorization/rollout; this WO supplies its reusable deployment mechanics.

### Dispatch and evidence contract (prepared 2026-10-18)

- No implementer has been launched for this preparation. Gate #1 for the new programme and effective WM-SEC-19 governance is pending; authoring permission is not implementation approval. Preserve older recorded approvals only within their unchanged/reduced Envelopes.
- After approval, use the configured implementation route, currently codex/gpt-6-luna, in a fresh instance with explicit model and this entire WO. Native bootstrap: inherited cwd is C:/Users/Micha Bigler/Documents/webapps; every tool command uses the assigned absolute target repo. Record accepted agent/process ID, batch/path ownership and requested model; exposed effective model/provider only, otherwise unverified. No worktree or feature branch.
- Reorient before each bounded dispatch, reload nearest governance/MEMORY/register, check overlap and refresh anchors. Future operations paths are new-file assignments, not evidence that a checkout exists. Establish its private main checkout and orient it before dispatch; otherwise block that batch. Read-only reference repositories are not mutation authority.
- Implementation review uses the current review and sec_review routes from .claude/models.local.json (example defaults for absent keys): run all configured general lenses concurrently on the same assembled diff, with the configured security reviewer in the same background batch. At preparation these routes are codex/gpt-6-luna and the general lenses are envelope, regression, duplication and tests; this snapshot does not override later config. No UI review is indicated by these infrastructure maps. Follow review_fallback only for evidenced unavailability. Reviewers are fresh, read-only instances, static-only. Separate authoring reviews do not establish implementation Gate #2.
- The Orchestrator checks its own security/exposure paths while reviews run, fixes in-scope findings, repeats required reviews on corrections, then runs only the union of affected/directly-dependent tests including relevant security tests. No implementation test or live proof is claimed here. Commit/push only on clean required review and scoped tests, stage own paths explicitly, and preserve dispatch/release/promotion/infra gates.
- Keep statuses planned until actual implementation starts. Record authoring/map commits separately from implementation commits; never mark done from a map or first success. Persist operator approval wording/date/scope in the register only after it arrives. Preserve the canonical finding history and require implementation plus effective-consumer/live evidence per finding.

### Contract/release and scoped gate

Agree the manifest/deploy contract with WM-SEC-17 within the approved Envelope before consuming it. Record exact tested dcm/toolchain versions and shared action release/pin via applicable version approval. No version/tag, app pin, candidate push or live deployment was produced in this preparation.

Authoritative scoped tests after implementation: .github/scripts/test_release_manifest.py, test_release_build_action.py, test_prebuilt_deploy.py, test_deploy_app_action.py and existing test_publish_backend_image_build_args.py; include the adjacent cleanup tests actually affected. Candidate proof uses a shared candidate plus an authorized image/static app caller, read actual remote results and retain WFT-CI-42 temp/cancellation assertions. Authorized image/static staging plus hosted recovery proofs belong to WM-INF-90 acceptance; an action test pass is not estate adoption.
