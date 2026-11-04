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

_Not yet filled. The Orchestrator fills the context package, target working directory, progress contract and preamble here before any dispatch. Dispatch is blocked while this placeholder remains._

# C. Orchestrator only — NOT ADDRESSED TO THE IMPLEMENTER

> Implementer: STOP here. Integration, verification, reviews and git belong to the Orchestrator.

- Source lands inactive; any provision of a runner host with the operations profile active is a separate operator-confirmed step under WM-SEC-17/WM-SEC-18.
- Report to the Infra Expertenchat on landing.
