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

## Context package

**Named file to change:** `.github/workflows/staging-health.yml` (the single `probe` job). Add the
new steps at the END of the existing job, after the Tailnet probe step — do not restructure or touch
any existing step. Create `.github/scripts/test_staging_health_auth_smoke.py` (new structural test
file; no existing test file covers this workflow yet — follow the house idiom already established in
`.github/scripts/test_app_ci_security_gates.py` and `.github/scripts/test_app_ci_e2e_job.py`: slice
the job body out of the live YAML text by header string, `re.search` on that slice, one deliberate
"mutate the live text, assert the guard fires" test per assertion).

**New inputs** (place after the existing `runs-on` input, before `permissions:`):

```yaml
      auth-smoke:
        description: >
          Opt-in browser smoke: after the health probe passes, open the login page in a headless
          browser and confirm a malformed email produces a translated sentence, not a raw backend
          code. Off by default -- existing callers are unaffected.
        type: boolean
        default: false
      login-path:
        description: 'Path to the kit login page, relative to the resolved staging domain.'
        type: string
        default: '/login'
```

Do NOT write a literal `${{` inside either description — `WFT-CI-23` made exactly this mistake once
(an empty/malformed expression inside a description breaks the whole reusable workflow for every one
of the 16+ callers, with zero jobs and no log). Both descriptions above are safe as written; do not
add anything that embeds `${{`.

**New step, appended at the end of the `probe` job** (after the "Probe /api/healthz via Tailnet"
step — this step runs regardless of `HAS_TS`, since the smoke needs no Tailscale at all; default
GitHub Actions behaviour — no `if: always()` — already means it is skipped if any PRIOR step in the
job genuinely failed, which is exactly "after the existing probe passes"):

```yaml
      - name: Auth smoke (opt-in): login renders a translated error, not a raw code
        if: ${{ inputs.auth-smoke }}
        env:
          DOMAIN: ${{ steps.domain.outputs.domain }}
          LOGIN_PATH: ${{ inputs.login-path }}
        run: |
          set -euo pipefail
          SMOKE_DIR="${RUNNER_TEMP}/auth-smoke"
          mkdir -p "${SMOKE_DIR}"
          cat > "${SMOKE_DIR}/auth-smoke.js" <<'NODE'
          <the Node script, see below>
          NODE
          docker run --rm --network host \
            --user "$(id -u):$(id -g)" \
            -e HOME=/tmp \
            -e SMOKE_URL="https://${DOMAIN}${LOGIN_PATH}" \
            -v "${SMOKE_DIR}:/workspace" \
            -w /workspace \
            mcr.microsoft.com/playwright:v1.63.0-noble \
            bash -lc "npm install --no-save --no-audit --no-fund playwright@1.63.0 >/dev/null 2>&1 && node auth-smoke.js"
```

**Why this exact shape — verified facts, not guesses, do not re-derive differently:**
1. **The official `mcr.microsoft.com/playwright` image ships browser binaries + OS deps ONLY — the
   `playwright` npm PACKAGE itself is NOT pre-installed.** (Confirmed against the upstream
   `Dockerfile.noble`.) The image's own `ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright` (world-writable,
   777) is already set at the IMAGE level and persists into the running container automatically — do
   not set it again, and do not add `PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD`. `npm install
   playwright@1.63.0` (version pinned to EXACTLY match the image tag's own version) resolves this
   path and its own postinstall skip-check finds the matching browsers already present, so it does
   NOT re-download them — only the lightweight JS package itself is fetched.
2. **`--user "$(id -u):$(id -g)"` + `-e HOME=/tmp`** — the same fix `WFT-CI-30` needed: writing
   `node_modules` into a bind-mounted directory as the image's default root user leaves root-owned
   files a self-hosted runner's own non-root cleanup cannot remove (confirmed live, broke other jobs'
   checkouts on `netcup-runner-1` twice already). `$RUNNER_TEMP` is already writable by the runner's
   own user, so the mount is consistent.
3. **The exact DOM structure** (verified against `ui-core-micha` 3.9.1 source, not guessed):
   - Email input: `input[type="email"]`. Password: `input[type="password"]`.
   - Submit button: `form button[type="submit"]` — the ONLY button in the form with this type (every
     other button — passkey, signup, forgot-password — uses `type="button"`), so this selector is
     unique without needing a visible-text match.
   - **Error message container: `.MuiAlert-colorError .MuiAlert-message`.** MUI gives every `Alert`
     `role="alert"`, including unrelated INFO alerts on the same page (e.g. a two-factor hint) — a
     bare `getByRole('alert')` can match the wrong box. The `-colorError` class is what narrows it to
     the actual error alert.
   - A malformed email with a digit in the TLD (e.g. `smoke-test@example.com2`) passes the browser's
     own native `type="email"` constraint validation (HTML5's email pattern permits a digit in the
     last label) but fails the backend's stricter validator — exactly the value the Envelope
     specifies, verified against the kit's own validation code path, not assumed.
4. **No `e2e-playwright-image` input is added** — the Envelope's own "New inputs" list names only
   `auth-smoke` and `login-path`. This smoke needs no app-specific `@playwright/test` resolution at
   all (it never runs a project's own test suite, only a freshly-installed, hardcoded-version
   `playwright` package against a public URL), so the image + version are internal implementation
   details of this workflow, hardcoded, not a caller-configurable input.

**The Node script** (`${SMOKE_DIR}/auth-smoke.js`, written by the heredoc above):

```js
const { chromium } = require('playwright');

const url = process.env.SMOKE_URL;
const BARE_CODE = /^[a-z][a-z0-9_]*$/;
const KIT_KEY = /^Auth\./;

(async () => {
  const browser = await chromium.launch();
  let exitCode = 1;
  try {
    const page = await browser.newPage();
    console.log(`Opening ${url}`);
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });

    const email = page.locator('input[type="email"]').first();
    const password = page.locator('input[type="password"]').first();
    const submit = page.locator('form button[type="submit"]').first();

    await email.waitFor({ state: 'visible', timeout: 15000 });
    await password.waitFor({ state: 'visible', timeout: 15000 });
    await submit.waitFor({ state: 'visible', timeout: 15000 });
    console.log('Login form rendered (email, password, submit present).');

    await email.fill('smoke-test@example.com2');
    await password.fill('irrelevant-dummy-password-00');
    await submit.click();

    const errorLocator = page.locator('.MuiAlert-colorError .MuiAlert-message').first();
    await errorLocator.waitFor({ state: 'visible', timeout: 15000 });
    const text = (await errorLocator.innerText()).trim();
    console.log(`Error message: ${JSON.stringify(text)}`);

    if (!text) {
      console.error('SMOKE FAILED: error message is empty.');
    } else if (BARE_CODE.test(text)) {
      console.error('SMOKE FAILED: error message is a bare backend code, not a sentence.');
    } else if (KIT_KEY.test(text)) {
      console.error('SMOKE FAILED: error message is an untranslated kit key.');
    } else {
      console.log('SMOKE PASSED: a translated sentence was shown.');
      exitCode = 0;
    }
  } catch (err) {
    console.error(`SMOKE FAILED: ${err && err.message ? err.message : err}`);
  } finally {
    await browser.close();
  }
  process.exit(exitCode);
})();
```

Embed this script verbatim inside the YAML heredoc exactly as given — do not rewrite its logic. If a
genuine bug is found in it during your own test-writing, fix it narrowly and note what changed.

**Known pitfall already paid for elsewhere in this file — do not reintroduce:** every container
invocation across `app-ci.yml`'s `e2e` job needed `--user "$(id -u):$(id -g)"` + `-e HOME=/tmp` for
this exact reason (root-owned residue on the shared runner); this step must carry both from the
start, not discover the need later via a live failure.

## Target repo working directory (absolute)

`C:\Users\biglmi\Documents\webapps\workflow-templates`

## Preamble — REQUIRED, do not strip

> The text above is the COMPLETE spec — the committed WO file's content, not a plan to refine; there
> is no separate plan file. Read the nearest `AGENTS.md`, the relevant `.codex/skills/<role>/SKILL.md`,
> and the app `MEMORY.md` ONLY for conventions. Stay in scope; do not touch auth/permissions/deps/
> schema/CI beyond this file and its test unless the spec says so; do not update `MEMORY.md`. **Do NOT
> edit `WORK_ORDERS.md` — the register row and the review verdicts are the orchestrator's alone.**
> **Your tools are for editing source and test files and for running the tests you wrote —
> nothing else.** Do NOT install dependencies, touch a lockfile, run a package manager, or tidy up
> stray files; if something in the repo state blocks you, stop and report it as
> `RESULT: BLOCKED <reason>` instead of fixing it. Do NOT `git add`/`commit`/`push` — leave every
> change uncommitted in the working tree for the orchestrator's independent review. WRITE the tests
> the `Required tests` section calls for AND **RUN the tests you just wrote** to confirm they execute
> and pass — that is the ONLY test run you do (NOT any live `gh workflow run` dispatch — that proof is
> the orchestrator's, via `ci-test/<ID>` refs on both sides). The orchestrator re-runs the
> authoritative set + does the independent review + the live proof run after you finish — those are
> the gate; your own run does not count as the gate.
>
> Narrate continuously: a `PLAN: <step1> | <step2> | …` line up front, then a single-line
> `PROGRESS: [<n>/<total>] <present-tense action>` before every relevant action (and `… done` on
> completion), spaced so no gap exceeds ~2 min, stdout unbuffered, plus exactly one final
> `RESULT: DONE|BLOCKED <reason>`.

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
