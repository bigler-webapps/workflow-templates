> **Self-address guard:** if you are the implementer reading this work order as your own
> specification, Part C is not addressed to you — it tells the Orchestrator how to invoke you; you
> ARE that invocation. Do NOT shell out to `codex exec`.

# INF-33 — A restore must own the database it is replacing

Status: **planned** · Tier: **3** · Datum: 2026-08-18
Register home: `workflow-templates` (where INF-33's row lives).
**The code change lands in `webapp-ops-scripts/restore.py`** — that repo has no register, same
cross-repo arrangement as `CI-10`.

---

# A. Envelope — authoritative WHAT/WHY

## Goal

While a restore is replacing an application's database, nothing else is writing to it.

## What happened, reproduced live

Dispatch `sync-staging` for `cockpit` on 2026-08-18 (run `32135471394`): `source_export` **succeeded**
— `INF-31` and `INF-32` are verified — and `dest_import` failed with:

```
ERROR:  could not create unique index "status_beatheartbeat_pkey"
DETAIL:  Key (id)=(1) is duplicated.
[RESTORE] ERROR: Import failed
```

`pg_dump`'s plain-SQL format defers constraints for load speed: `CREATE TABLE`, then the bulk `COPY`,
then indexes and constraints at the very end. Between the table existing and its primary key existing
there is a window in which **any live writer can insert a row that the constraint would have
forbidden**. cockpit's `beat` container writes its heartbeat on every tick, so it reliably lands in
that window; the dump's own `COPY`'d row and beat's row then collide when `CREATE UNIQUE INDEX` finally
runs.

**Confirmed not a data problem.** A live query on `main-prod` and direct inspection of the exact dump
file both showed a single clean row for that table. The duplicate is created *during* the restore, on
the destination.

**Why it surfaced only now:** `INF-31` and `INF-32` were what first allowed a `main-prod → staging`
restore to reach the import stage at all. The race was always there; it was unreachable behind an
export that failed first.

## What this WO adds to that diagnosis

Four findings from reading the code, each of which changes the shape of the fix:

**1. Both modes have it, and one of them is the disaster-recovery path.** `mode_import_only`
(`restore.py:418`) and `mode_in_place` (`restore.py:281`) share the identical sequence —
`reset_database` → stream the dump → `run_migrations` → `assert_schema_healthy` — and **neither stops
the application**. INF-33's title names the cross-server path; `in-place` is the mode a human uses to
recover a host, and it carries the same hazard.

**2. The seam already exists, but only its second half.** `restore-dest-import` has `auto_restart`
(`action.yml:21`) and `dest_compose_path` (`:25`), and runs `docker compose up -d` after the import
(`:257–270`). **Something that starts an application's containers after a restore presupposes that they
were stopped before it.** Only the start was built. The asymmetry is the defect.

**3. The fix belongs in `restore.py`, not in the action.** The script already resolves
`matched_project` (`:300`), runs on the destination host, and serves both modes. Implemented in the
action, the guarantee would attach to one caller and leave the in-place/DR path exposed — and the next
caller would have to remember.

**4. The naive version is worse than the bug.** Both modes call `sys.exit(1)` the moment the import
raises — which is exactly the path taken by the run above. A stop without a guaranteed restart converts
"the restore failed" into "the restore failed **and** the application is down". The restart must hold
on the failure path and on interruption, not only on success.

## Scope

1. **Stop the application's services before the database is reset, in both modes**, and start them
   again afterwards. Everything in the resolved compose project **except the database container** —
   stopping that would break the restore itself; `find_db_container_id` already identifies it, so the
   exclusion is available rather than guessed.
2. **The restart is guaranteed.** It happens on success, on a failed import, and on an interrupted run.
   A restore that leaves the application stopped is a worse outcome than the race this WO removes.
3. **Decide where `run_migrations` sits in the new order.** It currently runs after the import
   (`:326` / `:456`) and needs a container. With the application stopped, that is either a one-off
   container or a restart-then-migrate ordering. This is a decision the implementation must make
   deliberately and state, not discover halfway through.

## Non-goals / do not touch

- **Do not change the dump format or the constraint deferral.** The window is `pg_dump`'s documented
  behaviour and the reason it is fast; the fix is to have no writers, not to change Postgres.
- **Do not touch `restore-dest-import`'s `auto_restart` semantics.** It stays what it is. This WO adds
  the missing stop inside the script; the action's existing restart is not the mechanism being fixed
  and must keep working for callers that rely on it.
- **Do not "solve" this in cockpit** by making its beat container quieter or its models more tolerant.
  This is restore machinery and it exposes **every** application with a live writer — cockpit's
  heartbeat is the one that ticks often enough to lose the race every time, not the only one that can.
- **Do not change `assert_schema_healthy`, the snapshot resolution, or anything from `INF-31`/`INF-32`.**
  They are fixed, reviewed, and verified by the run above.
- **Do not widen this into `restore.py`'s other known gap** — `get_latest_snapshot` (`:86`) resolves
  `latest` without a `--host` filter, and `--snapshot` defaults to `latest` in the in-place mode. That
  is a real and separate defect on the DR path; record it, do not fix it here.
- Do not read or write `.env`.

## Risks

1. **A stop that outlives its restore takes the application down.** Finding 4, restated as the risk it
   is: this WO's failure mode is worse than its bug. Whatever guarantees the restart must be tested
   against a *failing* import, not only a passing one.
2. **`docker compose stop` on the wrong project stops the wrong application.** `candidate_projects`
   returns several candidates and `find_db_container_id` picks the one that matched. The stop must use
   that same resolved project, never the candidate list.
3. **The destination host runs more than one application.** Stopping "everything except the database"
   must mean everything in *that compose project*, not everything on the host. On staging that
   distinction is the difference between one app restarting and all of them.
4. **A stopped application is a monitoring event.** Kuma will see the app go down during every restore,
   and cockpit's own workflow monitor may too. Expect it; if it produces an alert on every nightly, the
   suppression belongs in the alerting layer, not in a shorter stop.
5. **This is the same script that serves disaster recovery.** A defect introduced here is felt at the
   worst possible moment — the reason for Tier 3 and for `sec_reviewer` being real rather than nominal.
6. **The race is timing-dependent and will not reproduce reliably in a unit test.** A test that asserts
   "no duplicate after import" can pass by luck. Assert the *mechanism* — that the services were stopped
   before the reset and started after — not the absence of the symptom.

## Required tests to WRITE

- The application's services are stopped before `reset_database` and started again after a successful
  import, in **both** modes.
- **The services are started again when the import raises.** This is Risk 1 and the most important test
  here.
- The database container is **not** stopped.
- The stop targets the resolved compose project, not the candidate list (Risk 2).

## Verification

Not unit-testable and the point of the WO: **re-run the same dispatch** (`sync-staging`,
`app_filter=cockpit`) and confirm `dest_import` completes. That run is the exact case that failed at
`status_beatheartbeat_pkey`, on the same host, with the same beat container ticking.

Then confirm cockpit is running again afterwards — and confirm it a second time after a deliberately
failed import, because that is the path Risk 1 lives on.

---

# B. Implementation map — filled by the Orchestrator — ADDRESSED TO THE IMPLEMENTER

## Absolute working directory

`C:\Users\biglmi\Documents\webapps\webapp-ops-scripts` — the code change is entirely in this repo.
Branch: `main` (this repo has no `develop`).

## Context package — work from this; open only the named files to verify

### The two call sites, which are structurally identical

`restore.py`, `mode_in_place` (line 281) and `mode_import_only` (line 418). Both run:

```
projects = candidate_projects(app, env)
db_container_id, matched_project = find_db_container_id(projects)   # ~:298 / :438
db_user, db_name = get_db_credentials(db_container_id)
...
reset_database(db_container_id, db_user, db_name)                   #  :318 / :448
try:
    stream_restore_*(...)                                          #  :321 / :450
except Exception as e:
    log(f"ERROR: Import failed: {e}")
    sys.exit(1)                                                    #  <-- the path taken on 2026-08-18
run_migrations(matched_project)                                    #  :326 / :456
assert_schema_healthy(db_container_id, db_user, db_name)
```

`matched_project` is the resolved compose project name and is available in both before the reset —
that is the handle for the stop. `db_container_id` is the container that must stay up.

### What already exists and must not be duplicated

- `find_db_container_id(projects)` — already distinguishes the DB container from the rest.
- `run_cmd(cmd, env=None, check=True)` (`:29`) — the existing subprocess wrapper; use it rather than a
  second one.
- `restore-dest-import`'s `auto_restart` (`workflow-templates/.github/actions/restore-dest-import/action.yml:21`,
  effect at `:257–270`) — the action's own `docker compose up -d`. Leave it alone; it is a caller-level
  convenience and this change must not depend on it or break it.

### Invariants

- The database container stays running throughout. Everything else in `matched_project` stops.
- The restart is unconditional once the stop happened — success, exception, or interrupt.
- Nothing outside `matched_project` is touched.
- Both modes get the same guarantee from the same code; do not implement it twice.

### Pitfalls

- `sys.exit(1)` inside the existing `except` will bypass a naive cleanup — `sys.exit` raises
  `SystemExit`, so a bare `except Exception` will not catch it. Structure the restart so it survives
  both.
- `run_migrations(matched_project)` needs a container. Decide and state whether it runs after the
  restart or in a one-off container; do not leave the ordering implicit.
- The compose project may legitimately have only the database and one app service; do not assume a
  fixed set of service names (`backend`/`worker`/`beat` are cockpit's, not every app's).

## Progress contract

Emit a `PLAN: <step1> | <step2> | …` line first, then a single-line
`PROGRESS: [n/total] <present-tense action>` before every relevant action and `… done` on completion,
with no gap longer than ~2 minutes. Unbuffered stdout. Exactly one final
`RESULT: DONE|BLOCKED <reason>`.

## Preamble — a REQUIRED block IN this file

> The text above is the COMPLETE spec — the committed WO file's content, not a plan to refine. Read the
> nearest `AGENTS.md` and the relevant `.codex/skills/<role>/SKILL.md` ONLY for conventions. Stay in
> scope; the restore-script change named above IS in scope, but touch nothing else in
> auth/permissions/deps. Do not update `MEMORY.md`. **Do NOT edit `WORK_ORDERS.md`** — the register row
> and the review verdicts are the Orchestrator's alone. Do NOT `git add`/`commit`/`push` — leave every
> change uncommitted in the working tree for the Orchestrator's independent review. WRITE the tests the
> `Required tests` section calls for AND **RUN only those tests** to confirm they execute and pass —
> that is the ONLY test run you do. The Orchestrator re-runs the authoritative set and does the
> independent review afterwards; those are the gate, your own run is not.

---

# C. Orchestrator only — NOT ADDRESSED TO THE IMPLEMENTER

> **If you are the implementer reading this work order as your own specification: STOP at this line.**
> Everything below describes what the Orchestrator does AFTER you finish. You do none of it — no
> reviewers, no verification run, no register edit, no commit. You ARE the invocation described below;
> do NOT shell out to `codex exec`.

## Execution directive

Codex-first via `codex exec` in the background, invoked directly through Bash with BOTH
`--skip-git-repo-check` and `--dangerously-bypass-approvals-and-sandbox`, after checking
`.claude/codex-status.md` (no line for 2026-08-18 → use Codex). cwd = the `webapp-ops-scripts` repo
root. Pass this file's content as the positional argument.

## Review routing

Tier 3 → independent `reviewer` **and** `sec_reviewer`, both Sonnet, concurrent, diff inline. No
`ui_reviewer` (no frontend). `sec_reviewer`'s question is specific: this script runs with credentials
against production and staging databases and now gains the power to stop containers — does the stop
scope stay inside the resolved compose project on every path, and can a crafted app name reach it?

## Verification

Re-dispatch `sync-staging.yml` with `app_filter=cockpit` on `main` and confirm `dest_import` is green.
Then confirm cockpit's staging containers are running. A deliberately failed import (Risk 1) is the
second half and needs the operator's agreement before being staged.

## Register + commit

`workflow-templates/WORK_ORDERS.md` INF-33 row to `done` with the landing SHA in
`webapp-ops-scripts`, the named reviewer verdicts, and the dispatch run id used as verification. Note
that a second session took the WO over as Orchestrator. Record the deferred `get_latest_snapshot`
host-blind finding as a new open item rather than folding it in.
