# WFT-CI-44 — Staging health probes without the deploy identity (closes S270)

# A. Envelope

**Goal.** Close S270 from the 2026-10-17 estate audit (`webapp-management/SECURITY_FINDINGS.md`). Expected
outcome: the pull-request-triggered staging health check never receives the OAuth client that mints
`tag:ci-deploy`; its probes use their own identity that can only reach staging over HTTPS.

**Background (from the finding).** `.github/workflows/staging-health.yml` runs with `environment: staging`,
joins the tailnet through `tailnet-connect` with `ts_tag: tag:ci-deploy`, then SSHes as `deploy` to the staging
host and curls the origin there. The apps call it on `pull_request` to `main` with `secrets: inherit`. A
same-repo PR that edits the caller runs with the PR's version and can exfiltrate the OAuth secret, which mints a
`tag:ci-deploy` node (deploy SSH to production) from anywhere, also after WM-SEC-8.

**Scope.**
1. The health probes join the tailnet as `tag:ci-health` with a separate OAuth client and probe the staging
   origin over the tailnet on HTTPS, without SSH. `tag:ci-health` reaches only staging hosts, only on TCP 443.
2. The reusable workflow no longer uses the deploy-capable OAuth client or `environment: staging`. The new
   client's secrets are declared in `secrets.yaml` and synced (per app or once centrally, whichever the
   existing secret flow supports).
3. Operator steps, written out in the WO's runbook text: create the OAuth client for `tag:ci-health`; add the tag
   owner, a grant `tag:ci-health` → `tag:server-stage` `tcp:443` and policy tests (accept 443; deny 22; deny
   `tag:server-prod`); create the Proton entry.

**Residual (not fixed here).** Someone with write access to an app repo can still reference
`environment: staging` in a workflow on their own branch; environment branch policies stay out of scope.

**Non-goals / do not touch.** Environment branch policies; the opt-in auth smoke unless it uses the tailnet;
deploy workflows.

**Risks.** Health checks going red on every PR during the switch; HTTPS over the tailnet must reach the right
host name and certificate (`--resolve`); WM-SEC-9 must keep allowing the tailnet on 443.

**Required tests (write as part of the implementation).**
- `staging-health` passes `tag:ci-health` and the new secrets, never `tag:ci-deploy` or the old client, and has
  no `environment: staging`.
- The probe step makes no SSH call.
- A `ci-test` run against one app's staging is green.

**Target repo.** `workflow-templates`, branch `main`. **Tier 3** (CI, credentials).

# B. Implementation map — ADDRESSED TO THE IMPLEMENTER

_To be filled by the Orchestrator (context package, preamble, progress contract) before dispatch._

# C. Orchestrator only — NOT ADDRESSED TO THE IMPLEMENTER

> Implementer: STOP here. Integration, verification, reviews and git belong to the Orchestrator.

Source: night audit 2026-10-17, S270; requirements round in session
`local_4987837b-7646-4b63-b3ab-1fd970f129bd`. Report back to: `local_4987837b-7646-4b63-b3ab-1fd970f129bd`.
