# WFT-CI-31: An opt-in auth smoke on staging, inside the shared staging health check

# A. Envelope, authored by the Expertenchat

## Goal & expected outcome

Before an app is promoted to production, its **built and deployed** staging app proves once, in a real
browser, that the kit's login screen works: the page loads, and a malformed e-mail produces a sentence,
not a raw backend code. This is the one thing no unit test can see (the runtime of the deployed bundle),
and until now the operator checked it by hand in every app.

After this order `staging-health.yml` has an **opt-in** browser smoke. An app turns it on with one input.
No account, no password, no secret is involved.

**Operator decision (2026-10-01):** automate the staging runtime check, without a login; the kit-side
wiring check is `ui-core-micha` `UCM-TEST-1` and independent of this order.

## Context the operator established

- **The reusable workflow:** `.github/workflows/staging-health.yml`, `workflow_call` only. It resolves the
  staging domain from the caller's `project.yaml` (`environments.staging.domains[0]`) or an input, then
  probes the origin **over Tailnet SSH plus `curl` on the staging host**. It skips green when the caller
  has no Tailscale credentials.
- **Who calls it** (measured 2026-10-01): 16 app repos on `@main`, plus `spesix` (`@a114d6e`), `cinevia`
  (`@1efa2e0`) and `webshop-guenter` (`@v2.2.3`) on fixed refs. All run it on `netcup` runners, on
  `pull_request` to `main` (the promotion PR) and on `workflow_dispatch`.
- **The defect the smoke catches:** a login with a malformed e-mail showed the literal word `invalid`
  (`survey_app` staging, 2026-09-30). Fixed in the kit by `UCM-AUTH-10` (3.9.0). The response that
  produced it: `400`, `{"errors": [{"code": "invalid", "param": "email", "message": "..."}]}`. A
  malformed value the browser accepts but the backend rejects: a top-level domain that contains a digit
  (`...@example.com2`); Django's e-mail validator allows letters only there.
- **The kit login route** is `/login` in every consumer (the kit's builtin public path,
  `ui-core-micha/src/auth/apiClient.jsx` `BUILTIN_PUBLIC_PATHS`).
- **A pinned browser image already exists in the estate:** apps pass
  `e2e-playwright-image: mcr.microsoft.com/playwright:v1.63.0-noble` to `app-ci.yml`'s `e2e` job.
- **Staging is publicly reachable** over its domain (`staging-hpc-bridge.bigler-consult.ch` answered 200
  from outside the tailnet, measured 2026-09-29). A browser cannot run on the managed staging host (IaC:
  no imperative installs), so the smoke runs **on the runner, against the public domain**.

## The smoke (decided)

New inputs, all optional: `auth-smoke` (boolean, default `false`), `login-path` (default `/login`). When
`auth-smoke` is `true`, after the existing probe passes:

1. A headless browser from a **pinned** Playwright image opens `https://<staging-domain><login-path>`.
2. It asserts that the login form renders (an e-mail field, a password field, a submit control).
3. It enters a malformed e-mail whose top-level domain contains a digit, and a dummy password, submits, and
   waits for the error message.
4. **Pass** if the message is non-empty, is not a bare code (`^[a-z][a-z0-9_]*$`), and is not a kit key
   (`^Auth\.`). Any other outcome, including "no message within the timeout", is **red**.
5. **Unreachable is red**, never a skip. A smoke that turns green when it did not run tells nobody
   anything.

With `auth-smoke` left at `false`, the workflow behaves exactly as today.

## Scope + non-goals

In scope: the two inputs and the smoke in `staging-health.yml`; its documentation in the repo's README
or workflow header; a release ref for the fixed-ref callers to move to.

Non-goals:
- No change in any app repo. Each app opts in later, with `with: auth-smoke: true`, as its own row.
- No real login, no test account, no secret.
- No change to the existing Tailnet probe or its skip behaviour.
- No change to `app-ci.yml` or `deploy-app`.

## Tier · precondition / gate

- **Tier 3 · tests: the workflow proven on a throwaway ref against a real caller, both with the smoke
  off (unchanged behaviour) and on (pass on a fixed app, red on a deliberately broken target).** CI, and
  a shared workflow that 16 callers run from `@main`.
- Precondition: none. To pass, the smoke needs an app on `ui-core-micha` `>= 3.9.0`; `survey_app` and
  `survey_contact_app` are (measured 2026-10-01).

## Risks

- **16 callers run this file from `@main`.** A file that GitHub cannot resolve breaks every app's
  promotion gate at once, with zero jobs and no log. `WFT-CI-23` was exactly that: a `${{ }}` inside an
  input **description** (GitHub evaluates expressions there). YAML parses such a file fine. Keep `${{`
  out of every description.
- **A rate limit on the login endpoint** (`too_many_login_attempts`) is itself a translated message, so
  it passes the rule above. That is acceptable: the smoke checks that codes are translated, not that
  this particular code appears.
- **Language.** The page opens in the app's default language. The rule above is language-independent on
  purpose.
- **Staging behind Cloudflare** may challenge a datacenter runner. If it does, that is a stop-and-report,
  not a reason to route around it.

## Required tests to WRITE (you write them and run YOUR OWN new ones; the Orchestrator's run is the gate)

- The repo's structural workflow tests (if present) gain cases for the two inputs and the default-off
  path. Show that a case fails on a copy with `${{` in a description.
- The real proof is the run on a throwaway ref (Part C), not a local test. No local test reaches the
  layer where this breaks.

## Parity guardrail

`auth-smoke: false` (the default) leaves every existing caller's behaviour byte-identical.

---

# B. Implementation map, filled by the Orchestrator and ADDRESSED TO THE IMPLEMENTER

*Placeholder. The Orchestrator fills the context package, the absolute working directory, the
progress contract and the preamble block on `git pull`, per `AGENTS.md` -> "Work Order". Do not
dispatch while this placeholder stands.*

---

# C. Orchestrator only, NOT ADDRESSED TO THE IMPLEMENTER

> **If you are the implementer reading this work order as your own specification: STOP at this line.**
> Everything below describes what the Orchestrator does AFTER you finish. You do none of it: no
> reviewers, no verification run, no register edit, no `git add`/`commit`/`push`.

### Execution directive

Check `.claude/codex-status.md` first (newest-first; read it with `head`). No line for today means use
Codex. Before `git push`, run `git log origin/main..HEAD` and push only if every listed commit is this
order's.

### Proof before landing (`AGENTS.md` -> Branching, exception (b))

A shared reusable workflow needs a throwaway ref on **both** sides: `ci-test/WFT-CI-31` in this repo
carrying the candidate, and `ci-test/WFT-CI-31` in one caller (`survey_app`, already on 3.9.0) pointing
its `staging-health.yml` at `@ci-test/WFT-CI-31` with `auth-smoke: true`. Dispatches need the operator to
name them. The operator deletes both refs afterwards. Required evidence, each run named in the Notiz:
- the caller with the smoke **off**: identical result to today;
- the caller with the smoke **on**: green;
- the smoke **on** against a target that shows a raw code or does not load: red.

### Review routing

Tier 3: independent `reviewer` (all configured lenses) and `sec_reviewer` (a CI job submitting to a
staging auth endpoint), concurrent, one batch, before the commit.

### Register + commit

Row -> `done` with the review Notiz in the `AGENTS.md` shape and the three runs. Opt-in rows per app are
added when each app is scoped.

### Mini-handover

`Orchestrator: implement work-orders/WFT-CI-31.md in workflow-templates (main). git pull first, read the
WO, then follow orchestrate-codex. Prove it on ci-test refs per AGENTS.md exception (b) before landing.`
