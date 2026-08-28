# INF-70 — `tailscale` role's check-mode assert always fails, on every host

Target repo: `workflow-templates` (branch `main`). Touches
`ansible/roles/tailscale/tasks/main.yml` only.

Tier: **3** — this role is part of the `biglerconsult.infra` collection consumed by
`webapp-management/ansible/site.yml` on every host provision, gated behind the CI
`ansible-provision` workflow. A change here reaches every host on its next provision run
via the collection pin bump in `webapp-management/ansible/requirements.yml`.

Companion webapp-management-side work (pin bump once this lands) is not created yet —
out of scope for this WO, raised separately once this fix is reviewed and merged.

---

## A. Envelope

### The bug

`--check --diff` (dry-run) against any playbook that includes this role fails on the
`tailscale` role's own assertion — observed on both an old and current `main` of
`webapp-management`, before the play ever reaches later roles (e.g. `app_cron`). The
failure is misleading: it reads as "Tailscale daemon did not respond to status query
after up", suggesting a live daemon problem, on hosts where the real daemon is healthy.

### Root cause

`ansible/roles/tailscale/tasks/main.yml:70-113`: `tailscale up` (line 70) and the
subsequent re-check `tailscale status --json` (line 88, registers
`_tailscale_status_post`) are both `ansible.builtin.command` tasks. `command` does not
declare `check_mode: true` support, so Ansible skips both unconditionally in `--check`
mode — `_tailscale_status_post` is never populated with real command output, only a
skipped-task result with no `rc`/`stdout` keys.

The two `ansible.builtin.assert` tasks that follow (lines 95-113) DO run in check mode
(assert has no side effects to skip) and immediately dereference
`_tailscale_status_post.rc` / `.stdout`, which are `Undefined` on the skipped result.
`rc == 0` against `Undefined` evaluates false, so the first assert fails every time,
regardless of the real backend state — this is structural, not host-specific: every
`--check` run through this role hits it, on every commit that carries these tasks.

Only reachable `when: tailscale_authkey | length > 0` — a host provisioned with no
authkey (the "install-only, stays disconnected" path documented in `defaults/main.yml`)
skips both asserts and is unaffected.

### The fix

Guard both assert tasks (lines 95 and 105) with an added `when` clause excluding check
mode — `when: tailscale_authkey | length > 0 and not ansible_check_mode` — so a dry run
skips the post-`up` verification it cannot meaningfully perform (the `up` command that
would produce the state to verify never ran) instead of failing on an artifact of its own
skip.

Do **not** solve this by adding `check_mode: false` to the three `command` tasks instead
— that would make a nominal dry run actually execute `tailscale up --reset --authkey=...`
against the real tailnet, which is precisely the side effect `--check` exists to prevent,
and on an already-`Running` host with `tailscale_force_up: false` it is skipped by its own
`when` anyway, so `check_mode: false` on the `up` task alone would not even reach a
consistent state to assert against.

### Scope

1. `ansible/roles/tailscale/tasks/main.yml`: add `and not ansible_check_mode` to the
   `when` clause of both assert tasks (lines ~95-103 and ~105-113).

### Non-goals

- Not a rewrite of the role's idempotency check (the pre-`up` `_tailscale_status` /
  `_tailscale_backend_state` logic, lines 50-61) — unaffected by this bug and out of
  scope.
- Not the webapp-management-side collection pin bump — raised as a follow-up once this
  is merged, per the existing INF-* pattern (`requirements.yml`'s versioned comment
  history) of bumping only after reading the merged SHA.
- Not a change to what the role verifies in a REAL (non-check) run — the two asserts keep
  exactly their current behaviour outside `--check`.

### Risks

**Risk 1 — a dry run against this role now asserts nothing about Tailscale's live state.**
Accepted: it never could — the commands producing that state don't run in check mode
either, before or after this fix. The fix removes a false failure, not real coverage; a
dry run was never able to prove Tailscale is healthy, only a real run was.

**Risk 2 — masking a genuinely broken host during `--check --diff`.** A host where
Tailscale is actually down would previously fail this assert during a dry run (for the
wrong stated reason) and now passes silently through it. This is judged acceptable
because `--check --diff` is a preview step, not the gate — the real (non-check) run,
which still executes and asserts fully, is what an operator relies on for host health,
and no current workflow treats a green `--check` as sufficient evidence of a healthy
daemon.

### Tests

No dedicated Python test harness exists in this repo for ansible role task logic (unlike
the shell-block extraction pattern used for `CI-18`/`INF-51`'s composite actions) — role
behaviour here is verified by running Ansible itself, not by a parser mimicking its
semantics.

Required verification: a real `ansible-provision --check --diff` dry run (via the
`ansible-provision.yml` CI dispatch, or locally against a real inventory host if available
to the Orchestrator) against at least one host carrying this role with a non-empty
`tailscale_authkey`, showing:

- the dry run no longer fails on the `tailscale` role's asserts;
- the play proceeds past `tailscale` to later roles (e.g. `app_cron`);
- a REAL (non-`--check`) run against the same host still executes and passes both
  asserts as before (no regression to the non-dry-run path).

### Verification

Live dispatch required — this cannot be proven by static reasoning alone, since the bug
is specifically about what Ansible's check-mode skip mechanics do to `Undefined` template
evaluation, which is worth confirming empirically rather than asserted from reading the
module docs. Use the `ansible-provision` workflow's existing `--check --diff` invocation
path; no new dispatch mechanism needed.

---

## B. Implementation map

### Context package

File: `ansible/roles/tailscale/tasks/main.yml` (114 lines total).

Relevant block, lines 88-113 (current):

```yaml
    - name: Re-check Tailscale state after up
      ansible.builtin.command: tailscale status --json
      register: _tailscale_status_post
      changed_when: false
      failed_when: false
      when: tailscale_authkey | length > 0

    - name: Assert Tailscale daemon responded with valid JSON
      ansible.builtin.assert:
        that:
          - _tailscale_status_post.rc == 0
          - (_tailscale_status_post.stdout | length) > 0
        fail_msg: >-
          Tailscale daemon did not respond to status query after up
          (rc={{ _tailscale_status_post.rc }}).
        when: tailscale_authkey | length > 0

    - name: Assert Tailscale BackendState is Running
      ansible.builtin.assert:
        that:
          - (_tailscale_status_post.stdout | from_json).BackendState == 'Running'
        fail_msg: >-
          Tailscale BackendState is
          {{ (_tailscale_status_post.stdout | from_json).BackendState }},
          expected Running.
        when: tailscale_authkey | length > 0
```

(Note: the `when:` in the excerpt above is indented as a task-level key sibling to
`that`/`fail_msg`, matching the file's actual current indentation — verify against the
live file before editing, don't retype from this excerpt blind.)

Target: append `and not ansible_check_mode` to both `when:` lines (the two assert tasks
only — leave the `Re-check Tailscale state after up` command task's own `when` at line 93
unchanged, since it already correctly gets skipped by Ansible's check-mode command
handling and touching it is not needed).

Invariant to preserve: outside `--check` mode, `ansible_check_mode` is `false`, so
`not ansible_check_mode` is always true there and the added clause is a no-op — the real
(non-dry-run) assert behaviour must be provably unchanged by this diff.

Work from this package — open only `ansible/roles/tailscale/tasks/main.yml` to verify
line numbers and exact indentation before editing.

### Target working directory

`workflow-templates/` (repo root) — role lives at `ansible/roles/tailscale/`.

### Progress contract

```
PLAN: <one line>
PROGRESS: [1/2] edit assert #1 when-clause
PROGRESS: [2/2] edit assert #2 when-clause
RESULT: DONE
```

(no test files to write — see Tests section; verification is a live dispatch run by the
Orchestrator, not by the implementer)

### Mini-handover

Repo: `workflow-templates` (branch `main`). WO: `work-orders/INF-70.md`. Follow
`orchestrate-codex`.

---

## C. Orchestrator-only

STOP — everything below this line is addressed to the Orchestrator, not the implementer.

### Review routing

Per `.claude/models.local.json`: `reviewer` only (new logic is minimal — a `when` clause
addition — but this is Tier 3 / CI-adjacent infra, so `reviewer` runs per the tier table;
no `ui_reviewer` — not frontend; no `sec_reviewer` — no auth/security surface touched).

### Verification procedure

Run the live `--check --diff` dispatch described under "Verification" above before
treating this as green. A clean diff/review with no live dispatch does not close this WO —
the bug this WO exists to fix is a live-behaviour claim, and "the YAML looks right" was
never sufficient evidence for it.

### Register

On green: add a row to `WORK_ORDERS.md` — `INF-70 | ... | 2026-08-28 | done | <SHA> | Tier
3 — review: <runtime>/<model> · <n> raised · <k> accepted · worst accepted: ... ·
verified live via ansible-provision --check --diff against <host>.`

### Execution directive

Small, single-file, mechanical diff (one added clause, twice). Implement directly in this
session rather than dispatching Codex for a two-line change, per `AGENTS.md` Tiering's
"smallest correct change" guidance for Tier 1-scale diffs riding inside a Tier 3
classification (the tier governs review/verification rigor, not implementer choice for a
change this small) — or dispatch per the configured `implementation.runtime` if the
Orchestrator prefers consistency with the standard flow. Either is acceptable; do not
skip the independent review or the live verification regardless of who edits the file.
