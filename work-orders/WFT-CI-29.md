# WFT-CI-29 — `deploy-traefik`'s `compose_profiles` allowlist rejects `geo-kg`

## Part A — Envelope

### Goal

Let the `deploy-traefik` composite action accept `geo-kg` as a compose profile, so
`webapp-management` can flip `mhaas-prod`'s `compose_profile` to `main,geo-kg` and start the
Kyrgyzstan Valhalla service there (`webapp-management` WO `WM-GEO-5`, mhaas-prod half).

### The gap

`.github/actions/deploy-traefik/action.yml:123` validates the `compose_profiles` input against the
fixed regex `^(main|staging|monitoring|geo)(,(main|staging|monitoring|geo))*$`. The `valhalla-kg`
service in `webapp-management/docker-compose.yml` is gated behind the distinct profile literal
`geo-kg` (deliberately NOT `geo`, so it never activates on the Swiss-tile hosts). On staging the
service runs via the `staging` literal, so the gap never showed. For mhaas-prod the inventory value
must be `main,geo-kg`, which this regex rejects at input validation, before anything is deployed.

### Scope

- Extend the allowlist regex on line 123 with the literal `geo-kg` (both alternations), and the
  human-readable list in the error message on line 124 accordingly.
- Nothing else in the action changes. The line-break `case` guard in front of the regex stays as
  is; the regex stays anchored `^...$` and stays a `[[ =~ ]]` match (see the comment block above it
  for why not `grep -qE`).

### Non-goals / do-not-touch

- No other input's validation, no other file in this repo.
- No generalisation of the allowlist (no wildcard like `geo-.*`): the list is an allowlist of
  literals on purpose, since the value is interpolated into an unquoted SSH heredoc downstream.
- No tag, no release, no consumer pin bump from this WO — the Orchestrator tags `v2.12.1` after the
  review (operator-approved 2026-09-14) and `webapp-management` bumps its own pin in `WM-GEO-5`.

### Tier · precondition

- Tier 3 — CI surface, composite action used by every Traefik deploy in the estate.
- No precondition. Consumer ordering (tag before the `webapp-management` profile flip) is outside
  this WO.

### Risks

- A regex typo would reject EVERY existing value (`main`, `staging`, `main,geo`, `monitoring`) and
  break every Traefik deploy at once. The mutation check below exists for exactly that.
- Widening the allowlist past a literal would reopen the injection path the comment block
  describes.

### Required tests to write

No test file — this repo has no harness for composite-action shell steps, same as WFT-CI-28. Verify
the regex change with a one-off bash mutation check and paste its output into your final report:
the new pattern must ACCEPT `main`, `staging`, `monitoring`, `main,geo`, `main,geo-kg`,
`geo-kg,staging` and must REJECT `geo-kgx`, `geo-k`, `main,geo-kg,`, `,geo-kg`, `main geo-kg`,
`main,,geo-kg`, `GEO-KG`. Run it with `bash -c` against the literal pattern copied from the edited
line 123 (not retyped).

---

## Part B — Implementation map — filled by the Orchestrator — ADDRESSED TO THE IMPLEMENTER

### Context package

- **Named file to change:** `.github/actions/deploy-traefik/action.yml`, lines 123-124, inside the
  step `Validate inputs` (the `[[ "$INPUT_COMPOSE_PROFILES" =~ ... ]] || { ... }` block that follows
  the `case "$INPUT_COMPOSE_PROFILES" in` line-break guard).
- **Current lines:**

  ```bash
  [[ "$INPUT_COMPOSE_PROFILES" =~ ^(main|staging|monitoring|geo)(,(main|staging|monitoring|geo))*$ ]] || {
    echo "❌ Invalid 'compose_profiles' input '$INPUT_COMPOSE_PROFILES' (allowed: comma-separated list of main, staging, monitoring, geo)"
  ```

- **Invariants:** anchored regex, literal alternatives only, `[[ =~ ]]` not `grep`, the `case`
  newline guard untouched, the comment block above the `case` untouched (it documents a real
  injection fix from `v2.5.6`; nothing in it becomes wrong). Alternation order is irrelevant to
  bash ERE here because the match is anchored end-to-end, so `geo` before `geo-kg` is fine.
- **Do not touch** any other step, `action.yml`'s `inputs:` block, or the workflows under
  `.github/workflows/`.
- Directive: work from this package; open only the named file to verify. There is nothing to
  explore.

### Target repo working directory (absolute)

`C:\Users\biglmi\Documents\webapps\workflow-templates`

### Preamble

> The text above is the COMPLETE spec — the committed WO file's content, not a plan to refine; there
> is no separate plan file. Read the nearest `AGENTS.md`, the relevant `.codex/skills/<role>/SKILL.md`, and the
> app `MEMORY.md` ONLY for conventions. Stay in scope; do not touch auth/permissions/deps/schema/CI
> unless the spec says so; do not update `MEMORY.md`. **Do NOT edit `WORK_ORDERS.md` — the register
> row and the review verdicts are the orchestrator's alone.** **Your tools are for editing source
> and test files and for running the tests you wrote — nothing else.** Do NOT install dependencies,
> touch a lockfile, run a package manager, or tidy up stray files; if something in the repo state
> blocks you, stop and report it as `RESULT: BLOCKED <reason>` instead of fixing it. Do NOT
> `git add`/`commit`/`push` — leave every
> change uncommitted in the working tree for the orchestrator's independent review. WRITE the tests
> the `Required tests` section calls for AND **RUN the tests you just wrote** to confirm they execute
> and pass — that is the ONLY test run you do (NOT the app's affected/full suite, NOT any review).
> The orchestrator re-runs the authoritative set + does the independent review after you finish —
> those are the gate; your own run does not count as the gate.
>
> Narrate continuously: a `PLAN: <step1> | <step2> | …` line up front, then a single-line
> `PROGRESS: [<n>/<total>] <present-tense action>` before every relevant action (and `… done` on
> completion), spaced so no gap exceeds ~2 min, stdout unbuffered, plus exactly one final
> `RESULT: DONE|BLOCKED <reason>`.

---

## Part C — Orchestrator only — NOT ADDRESSED TO THE IMPLEMENTER

> **If you are the implementer reading this work order as your own specification: STOP at this line.
> Everything below describes what the Orchestrator does AFTER you finish. You do none of it — no
> reviewers, no verification run, no register edit, no commit.** You ARE the invocation described
> below; do NOT shell out to `codex exec`.

### Execution directive

Implement through `codex exec` in the background — invoked directly via Bash (never the
`debugger`/`*_coder` Agent wrappers) with BOTH flags `--skip-git-repo-check` and
`--dangerously-bypass-approvals-and-sandbox`, `-m` per `.claude/models.local.json`. Fallback to
direct Claude implementation only on Codex quota / rate-limit / non-zero exit.

### Review routing

Tier 3: the four `review` lenses plus `sec_reviewer`, one background batch, per
`.claude/models.local.json`. This diff is reviewed TOGETHER with the `webapp-management` `WM-GEO-5`
mhaas-prod diff (pin bump to `v2.12.1`, `main,geo-kg` on mhaas-prod, manifest, runbook) — one
logical change across two repos, one consolidated review, the same review line recorded in both
registers. No `ui_reviewer`.

### Verification

The Orchestrator re-runs the mutation check from Part A itself against the edited line (the
implementer's run is not the gate). No CI run can prove the action before it is tagged and
consumed; the first consumer run (`webapp-management` `deploy-traefik.yml` on the `main,geo-kg`
target) is the live evidence and is recorded in `WM-GEO-5`'s Notiz.

### Register + commit

Row `WFT-CI-29` → `done` with the review line. Single commit on `main`, then annotated tag
`v2.12.1` (operator-approved 2026-09-14) pushed to `origin`; `webapp-management` pins that tag in
`WM-GEO-5`'s landing commit.
