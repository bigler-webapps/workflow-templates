# WFT-CI-46 — Operations runner profile carries the `ops-deploy` label

# A. Envelope

## Goal and expected outcome

WM-SEC-17's P1 deployment path runs `webapp-operations/.github/workflows/deploy.yml` on runner labels `self-hosted, ops-deploy`. The `github_runner` role in this repository already has an isolated operations profile (`github_runner_profile: operations`, one slot, `github_runner_operations_active: false`), but no label `ops-deploy` exists anywhere: the role knows only `github_runner_labels` and per-slot extra labels.

Outcome:

1. A runner rendered with the operations profile registers with exactly the labels the deploy workflow requests (`self-hosted`, `ops-deploy`, plus whatever the profile already sets), fixed by the role, not by inventory input.
2. No runner rendered with the CI profile can carry `ops-deploy`: an inventory that tries to add it through `github_runner_labels` or slot extra labels fails in the role, with a clear message.
3. Everything stays inactive: the operations profile's service is not enabled, no registration happens, `github_runner_operations_active` stays `false` by default.

## Scope

- `ansible/roles/github_runner/**` (defaults, templates, tasks for the operations profile), its tests and its README.

## Non-goals / do not touch

- No activation and no registration credential. Registration of the operations runner waits for WM-SEC-18's off-host JIT issuer and the operator's gates.
- No change to the CI profile's existing labels, slot count, runner version or service behaviour.
- No change to any inventory or host_vars in webapp-management, and no workflow file in any app repository.
- No change to the shared `app-ci.yml` or any other reusable workflow.

## Risks

- **Label crossing.** If a CI runner could carry `ops-deploy`, an ordinary CI job could be routed onto the privileged path, or a privileged deploy onto a CI runner. The refusal in outcome 2 is the safeguard; it must hold for every way labels reach the runner configuration.
- **Shared role.** Every runner host consumes this role. A rendering change in the CI profile would change live runners at their next provision.

## Required tests to write (scoped)

1. The operations profile renders the label set including `ops-deploy`; the value does not depend on inventory input.
2. The CI profile renders no `ops-deploy`; adding it via `github_runner_labels` or a slot extra label is refused.
3. The CI profile's rendered configuration is unchanged against the current output (labels, slots, service units).
4. With the defaults, the operations profile installs no enabled service and performs no registration.

## Tier

Tier 3 — CI runner configuration (privilege boundary of the operations path).

# B. Implementation map — ADDRESSED TO THE IMPLEMENTER

## Package: operations profile carries `ops-deploy` - current assignment, 2026-11-04

Target working directory: C:/Users/Micha Bigler/Documents/webapps/workflow-templates, branch main. Native inherits the workspace root; use explicit target paths. You are not alone: other sessions have uncommitted work in this checkout (`.github/workflows/staging-health.yml`, `work-orders/WFT-CI-44.md`, `.github/scripts/test_staging_health_probe.py`, `scratchpad/`); preserve it and touch ONLY your own files.

**Own ONLY** `ansible/roles/github_runner/templates/operations-launcher.sh.j2`, `ansible/roles/github_runner/tasks/main.yml` (the first assert task and ONE new assert task before the profile includes), `ansible/roles/github_runner/tests/test_gh_runner_operations.py`, a NEW test file `ansible/roles/github_runner/tests/test_gh_runner_ops_deploy_label.py`, and `ansible/roles/github_runner/README.md` (operations section only). Do NOT touch `templates/jit-config.sh.j2` (its SHA-256 is pinned by `test_unchanged_jit_template_remains_exact_baseline`), `tasks/ci.yml`, `defaults/main.yml`, the CI service templates, any inventory, or any file outside the role.

**How labels reach a runner today (verified).** CI profile: `github_runner_labels` (default `self-hosted,netcup`) and `github_runner_slot_extra_labels` are rendered by `templates/jit-config.sh.j2` (`LABELS=...`, per-slot arms), the slot-label shell-safety assert is in `tasks/ci.yml`. Operations profile: `templates/operations-launcher.sh.j2` calls the root-owned off-host issuer client `issue-runner-jit --repository <repo> --runner-name ops-slot-1`; the JIT registration labels are chosen by the issuer, the launcher passes none. So the role cannot pin the label by itself unless the fixed request carries it.

**Required change (decisions are fixed by Part A).**
1. Operations profile: the launcher's issuer call becomes `"$ISSUER" --repository "$REPOSITORY" --runner-name "$RUNNER" --labels "$LABELS"` with `readonly LABELS="self-hosted,ops-deploy"` written LITERALLY in the template (not a role variable, not inventory-derived, so no inventory value can change it). Update the README's "fixed request" paragraph to state the new fixed request and that the issuer (WM-SEC-18, not implemented here) must refuse any label set other than exactly these two. Everything else in the launcher is unchanged.
2. CI profile: add ONE assert task in `tasks/main.yml` that runs before both `include_tasks` and fails with a clear message when `ops-deploy` (compared case-insensitively, after splitting on commas and trimming spaces) appears in `github_runner_labels` or in any value of `github_runner_slot_extra_labels`. The assert must hold when the profile is `operations` too (an `ops-deploy` in those CI variables is rejected regardless of profile); it must not fail for the defaults and must not change any CI rendering. Quote any condition containing a colon followed by a space (a past defect: such conditions parse as mappings). The existing first assert task is only edited if the new condition belongs there; prefer a separate task named "Assert the ops-deploy label cannot reach a CI runner".

**Test seams (read first).** `ansible/roles/github_runner/tests/test_gh_runner_operations.py`: helpers `values()`, `render(name, overrides)`, `instrument(tasks)`, `run_role(tmp_path, overrides, hostname, groups, ci_probe)` (real Ansible with every host-mutating module instrumented), the fake issuer at line ~386 that checks `$1..$4` exactly (it must now also accept `--labels self-hosted,ops-deploy` as `$5 $6` and the launcher test must fail when the labels differ), `test_unchanged_jit_template_remains_exact_baseline`. `tests/test_gh_runner_slot_labels.py` for the CI label style.

**Tests to write.** (a) the rendered operations launcher contains the literal fixed labels and the issuer call passes them, and rendering with `github_runner_labels` or slot extras set to other values leaves the launcher unchanged; (b) the CI profile never renders `ops-deploy` and the new assert rejects it via `github_runner_labels`, via a slot extra label, in mixed case and with surrounding spaces, using the real-Ansible `run_role` helper; (c) the CI profile's rendered `jit-config.sh.j2` output for the default values is byte-identical to its current output (the existing baseline test covers the template hash; add a render comparison for the defaults); (d) with the defaults the operations profile still installs no enabled service and performs no registration (the existing assertions stay green; add one if none names it). Update the existing fake-issuer test for the new arguments. Run only your own new and updated tests once with `python -I -m pytest -q <files>` if the environment allows; if Ansible is unavailable in your sandbox, report that and the exact command.

**Invariants.** Role defaults stay inactive (`github_runner_operations_active: false`); `github_runner_profile` default stays `ci`; the operations profile service is never enabled by this change; no credential or registration step is added; CI output is unchanged for default inputs. Use explicit UTF-8 file I/O.

## Preamble — binding on the implementation instance

> The text above is the COMPLETE spec: this WO is not a plan to refine. Read the nearest AGENTS.md and CLAUDE.md, their mandatory named references, the repo WORK_ORDERS.md (open WOs and highest ID), and the relevant role skill for applicable conventions and mandatory instructions. These context reads are required; they grant no register-edit or scope-expansion authority. Work from this context package; open the named files to verify it, without broad exploration. If an essential seam is missing or changed, report RESULT: BLOCKED with the missing context.
>
> Edit only the explicitly assigned implementation files for the dispatched batch: source, tests, configuration and required documentation. You are not alone in this checkout: preserve others' edits and stop on overlapping foreign changes. Use explicit UTF-8 file I/O. Do not edit MEMORY.md or WORK_ORDERS.md; register updates and review verdicts belong to the Orchestrator. Do not change effective governance.
>
> Perform only the read-only context reads expressly required above. Beyond those reads, tools are for editing those assigned implementation files and running the tests you wrote, once; nothing else. Git orientation and all repository operations belong to the Orchestrator. No dependency installation, package-manager commands, lockfile changes, repository administration, server access, provisioning, workflow dispatch, secret delivery, cleanup or git add/commit/push. Report blocked repo state instead of repairing it. Never read or modify .env or private SSH configuration, print credentials, or perform destructive actions.
>
> Write Part A's tests for this batch and run only your own new tests once. Do not run the affected/full suite, self-review, launch reviewers or claim a review verdict. Your run is not a gate; the Orchestrator owns independent review and the authoritative scoped run. Leave all edits uncommitted.

## Progress contract

Start with PLAN: <bounded steps>. Before each relevant action emit PROGRESS: [n/total] <present-tense action>, then report completion; use unbuffered output and leave no narration gap over two minutes. Finish with exactly one RESULT: DONE|BLOCKED <reason>, listing changed paths, own test command/result and unresolved acceptance evidence. DONE describes the assigned implementation batch only; it never asserts live deployment, runner activation or programme closure.


# C. Orchestrator only — NOT ADDRESSED TO THE IMPLEMENTER

> Implementer: STOP here. Integration, verification, reviews and git belong to the Orchestrator.

- Source lands inactive; any provision of a runner host with the operations profile active is a separate operator-confirmed step under WM-SEC-17/WM-SEC-18.
- Report to the Infra Expertenchat on landing.

## Source Gate #2 - operations runner profile carries `ops-deploy`, 2026-11-04

Source: `templates/operations-launcher.sh.j2` now passes the literal fixed label string `self-hosted,ops-deploy` to the off-host issuer (`--labels`, not inventory-derived); `tasks/main.yml` gains one assert, run before both profile includes, that makes `ops-deploy` unreachable from the CI profile; README states the new fixed issuer request and that WM-SEC-18's issuer must refuse any other label set. `jit-config.sh.j2`, `ci.yml`, defaults and inventory are untouched. Nothing is activated or registered.

Implementer codex/gpt-6-luna (OpenAI, workspace-write, banner confirmed), initial dispatch only; its sandbox had no pytest, so it saw no results. Orchestrator ran the role tests on the pinned WSL controller: `ansible/roles/github_runner/tests` 140 passed, 1 failed, then the label file alone 16 passed after the last test addition. The single failure, `test_unchanged_jit_template_remains_exact_baseline`, is red on unmodified HEAD (the pinned hash no longer matches the committed, unchanged `jit-config.sh.j2`; a pre-existing defect since the template changed, not part of this diff and not fixed here).

review: codex/gpt-6-luna (openai, read-only) · lenses: envelope, regression, duplication, tests, security · 3 raised · 3 accepted · worst accepted: blocker · carried: 0. Round one: the security lens raised a blocker (a shell-injection string in `github_runner_labels`, e.g. `x"; LABELS="ops-deploy`, passed the label check because `jit-config.sh.j2` puts the value in a double-quoted shell assignment) and a defect (a list value raised an attribute error before the clear refusal); both fixed: the assert requires `github_runner_labels` to be a string and both label inputs to match the shell-safe pattern `ci.yml` already uses for slot labels, checks mapping-ness first, then refuses `ops-deploy` case-insensitively; tests for injection and list inputs added. Closing round: envelope, regression, duplication and security No findings; tests 1 defect (the mixed-case cases also contained spaces), fixed by two pure mixed-case cases without a further review pass.

Not done and still open: activation and registration of the operations runner (WM-SEC-18 issuer and the operator's gates), the issuer's enforcement of the label set, the pre-existing red baseline hash test.
