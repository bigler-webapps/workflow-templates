# WFT-CI-61 — Static target post-up inspect check expects 6 fields (pointer to WM-INF-172)

## A. Envelope

Pointer stub. Scope, Gate #1 and the operator decisions of 2026-11-27 (including the decision that this defect is fixed inside WM-INF-172, tonight) live in `webapp-management/work-orders/WM-INF-172.md`. Found by the first live static deploy (yopoulab_staging, ops run 46076691027): after `up` the post-deploy check failed with `container_inspect_invalid`, because `prebuilt_static_target.execute_static_deployment` splits the shared inspect template output into 6 fields. The template `target_exec._INSPECT_TEMPLATE` has 8 fields (image, state, restarting, health, project, service, working_dir, config_files) and Docker Engine appends one extra newline, so every real static deployment fails.

Outcome: the static check parses like the image path in `prebuilt_target.py` (one extra trailing newline tolerated, exactly 8 fields), keeps its identity and health checks, and additionally checks the two extra fields; regression tests prove it. No release behaviour changes beyond that.

Tier 3 (CI/CD, shared template, infra lane).

## B. Implementation map

- `.github/scripts/prebuilt_static_target.py` (around line 503, in `execute_static_deployment`): `fields = inspect_raw.decode(...).splitlines()` then `len(fields) != 6`, then the six-value unpacking. Reference: `.github/scripts/prebuilt_target.py` lines 1117-1135 (tolerates exactly one extra newline, eight values, `container_configuration_mismatch` for working directory and configuration files).
- Compose invocation in the static path (around line 436): `--project-directory <app>`, one `--file <control.filename>` per compose control, then `--file <override>`; commands run with `cwd=<app>`. Docker records the label `com.docker.compose.project.config_files` as the comma-joined absolute paths of these files in that order.
- `.github/scripts/test_prebuilt_static_target.py`: `FakeExecutor` (around line 105) returns six JSON lines without the trailing newline and without the two extra fields; update it to the real shape (eight lines, ends in two newlines) and add tests: real shape passes, one extra trailing newline passes, a further empty line fails, a missing field fails (`container_inspect_invalid`), wrong working directory and wrong configuration files fail (`container_configuration_mismatch`).
- `.github/scripts/test_prebuilt_target.py` around line 395 holds the equivalent image-path regression tests; mirror their style.
- `CHANGELOG.md` and version files are not part of this change; the Orchestrator releases.

## Preamble

> You are the dispatched implementer. Run `codex-hook-canary-ws-gov-60` as the first tool action.
> Continue on the expected denial. Otherwise report unverified coverage once and continue under the existing gates.
> Read this stub, WM-INF-172 Part A with its Decisions and Gate record, the nearest governance and the relevant role skill.
> Edit only the files named in Part B of this repository, and run only the new tests you wrote. Preserve parallel work.
> Do not access hosts or credentials, dispatch jobs or agents, run reviews, edit the register or write to git.
> Report progress and end with `RESULT: DONE` or `RESULT: BLOCKED` with the test evidence.

## C. Orchestrator only

> Implementer: stop here. The Orchestrator owns review, evidence, release and landing.

- The tag push needs the operator's explicit OK naming the version (relayed via the Infra Expertenchat); the pin bump in webapp-operations and the launcher install on staging follow.
- Foreign open paths in this repo (staging-health.yml, prebuilt-target-executor.md, WFT-CI-44.md, test_staging_health_probe.py) are not mine; stage only own files.

## Completion (2026-11-27)

- Change: `prebuilt_static_target.py` parses the inspect output like the image path (one extra trailing newline tolerated, exactly 8 fields) and checks working directory and configuration files (`container_configuration_mismatch`); `test_prebuilt_static_target.py` uses the real inspect shape and adds regression tests (real shape, one extra newline, further empty line, missing field, wrong directory, wrong files, order, override).
- Review: one round (infra lane), 5 lenses, 2 raised, 1 accepted (tests: the configuration-files check was not order or override sensitive; fixed with two tests); the duplication nit is not accepted (the image path stays untouched). The fix after the closing pass got no further round.
- Tests: static and image target test files 93 passed in a native WSL copy with the local PyYAML 6.0.1 substituted for the pinned 6.0.3 (the only red test is the pin test itself, caused by that substitution). No `project.yaml` scope covers these files, so this is not `tests.py` evidence.
- Open: tag release (operator OK naming the version), pin bump, launcher install on staging, live proof (the `config_files` label value is derived from the code).
- Part B: accurate.

## Gate record

Covered by WM-INF-172's Gate #1 (operator, 2026-11-27) and the operator decision of 2026-11-27 relayed by the Infra Expertenchat to fix this defect inside WM-INF-172.
