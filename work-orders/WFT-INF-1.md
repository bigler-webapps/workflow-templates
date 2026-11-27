# WFT-INF-1 — base role owns the Ubuntu package sources (stub of webapp-management/WM-INF-166)

## A. Envelope

Pointer stub. The full Envelope, Gate #1 and the rollout live in `webapp-management/work-orders/WM-INF-166.md` (Gate #1 approved by the operator 2026-11-27, click in session local_1e5f212a-77db-4423-a507-983adf826bc4). This repo carries the role half: review runs here, the pin bump and the rollout there.

Goal: the `base` role owns the Ubuntu apt sources on every managed host, so one broken country mirror (`at.archive.ubuntu.com` returned 404 for `noble` Release on 2026-11-27) cannot block provisioning.

Outcome:
- one deb822 file `/etc/apt/sources.list.d/ubuntu.sources` with exactly two stanzas: `http://archive.ubuntu.com/ubuntu/` (suites noble, noble-updates, noble-backports) and `http://security.ubuntu.com/ubuntu/` (suite noble-security); components `main restricted universe multiverse`, `Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg`, `Types: deb`;
- no `<cc>.archive.ubuntu.com` in any source the role manages;
- a duplicate Ubuntu line in `/etc/apt/sources.list` is removed (a file holding only comments stays as it is), so each suite is listed once;
- Docker and Tailscale sources are untouched; the task is idempotent (a matching host reports no change);
- the sources task runs before the apt cache refresh block of the role.

Excluded: upgrade policy, hold list, unattended-upgrades, third-party sources, apt proxy, cloud-init configuration (the hosts already carry `apt: preserve_sources_list: true`, so cloud-init does not rewrite the file).

Tier 3 (prod infrastructure, all hosts). Required tests: the rendered file content exactly; replacement of a file with a country mirror and unchanged on a matching one; the `sources.list` duplicate removed and third-party files untouched; the sources task precedes the cache refresh.

## B. Implementation map

- `ansible/roles/base/tasks/main.yml`: the block `Apt update and upgrade, excluding transport-critical packages` (l.31-84) holds `ansible.builtin.apt` with `update_cache`; the new sources task goes inside the outer block before it. `ansible/roles/base/defaults/main.yml` takes the documented variables (a role switch `base_ubuntu_sources_managed: true`, the two URIs, suites, components, keyring); a template under `ansible/roles/base/templates/` renders the file.
- Tests beside the role: `ansible/roles/base/tests/` (idiom: `test_base_apt_hold.py`, pytest with PyYAML and Jinja2).
- Host facts (read 2026-11-27): `/etc/apt/sources.list.d/ubuntu.sources` is deb822 with the country mirror for the first stanza and `security.ubuntu.com` for the second (staging, monitoring); `/etc/apt/sources.list` is a one-line comment on staging and a 70-byte `archive.ubuntu.com` line on monitoring; `ubuntu.sources.curtin.orig`, `docker.list`, `tailscale.list` also exist; `/etc/cloud/cloud.cfg.d/curtin-preserve-sources.cfg` sets `apt: preserve_sources_list: true`.
- Do not touch: other roles, workflows, `CHANGELOG.md` release notes beyond one `[Unreleased]` entry (no version, no tag), anything under `webapp-management`.

## Preamble

> You are the dispatched implementer. Run `codex-hook-canary-ws-gov-60` as the first tool action.
> Continue on the expected denial. Otherwise report unverified coverage once and continue under the existing gates.
> Read this Envelope and its Gate record, the nearest governance and the relevant role skill.
> Edit only the `base` role's sources task, template, defaults and tests and one `CHANGELOG.md` entry, and run only the new tests you wrote. Preserve parallel work.
> Do not access hosts or credentials, dispatch jobs or agents, run reviews, edit the register or write to git.
> Report progress and end with `RESULT: DONE` or `RESULT: BLOCKED` with the test evidence.

## C. Orchestrator only

> Implementer: stop here. The Orchestrator owns review, evidence, release, pin and rollout.

Stub for the cross-repo work order `webapp-management/WM-INF-166`. Report back to: `local_1e5f212a-77db-4423-a507-983adf826bc4`.

## Gate record
Gate #1 approved by the operator 2026-11-27 for the full Envelope in `webapp-management/work-orders/WM-INF-166.md` (click in session local_1e5f212a-77db-4423-a507-983adf826bc4).

## Completion

Implemented 2026-11-27 (codex/gpt-6.1-terra medium; one fix dispatch fix_defect): the `base` role renders `/etc/apt/sources.list.d/ubuntu.sources` from `templates/ubuntu.sources.j2` (archive.ubuntu.com for the release, -updates, -backports; security.ubuntu.com for -security; components and keyring as before) and removes Ubuntu `deb` lines from `/etc/apt/sources.list`, before the apt cache refresh. Variables are in `defaults/main.yml`; the suites follow `ansible_facts.distribution_release` (default noble) and the block runs only on Ubuntu with `base_ubuntu_sources_managed` true. Added the scope `base-role` to `project.yaml` (13 tests, GREEN, gate across trees). The collection is pinned by commit SHA, so the landing commit is the release.

Review (infra lane, five lenses, codex/gpt-6.1-terra and gpt-6-luna), two rounds. Round 1: 6 raised, 5 accepted and fixed (noble hardcoded on other releases; `deb-src` removal; tests that wrote their own expected output or ignored variables; replacement not exercised). Round 2 (closing): 7 raised, none a blocker, all carried: (1) a non-noble host gets its own release's suites, which the envelope lens reads as contrary to the Envelope text (all checked hosts are noble amd64; the round-1 regression finding required this); (2) a `deb-src` line on a country mirror in `sources.list` stays by design; (3) arm64 hosts would need the ports archive: none in the estate (staging, monitoring, research-prod read as amd64, no arm64 hint in the repos; prod boxes not agent-readable); (4) tests render and parse but never run Ansible (raw ansible is not runnable from agent sessions): the real proof is the per-host evidence at rollout; the replacement and idempotence findings were raised by three lenses for that reason. Counts: 13 raised · 5 accepted · worst accepted: defect · carried: 4. Part B: accurate (the stub's Part B was written by the Orchestrator).

Foreign paths left open in workflow-templates: `.github/workflows/staging-health.yml`, `docs/prebuilt-target-executor.md`, `work-orders/WFT-CI-44.md`, `.github/scripts/test_staging_health_probe.py` (likely WFT-CI-44).
