# WFT-CI-37: Optional blocking E2E capture

# A. Envelope — operator-approved scope

## Goal and expected outcome

An app can opt in to making capture failures fail the existing `ci / e2e` job.
The default remains non-blocking for existing callers. With the gate enabled,
failed manifest enumeration, an empty cell set, failed reseeding and failed
capture commands all fail the job; a successful capture remains green.
Capture artifacts are still uploaded after failure for diagnosis.

## Scope, non-goals and tier

Add boolean `e2e-capture-gate`, default false, to `app-ci.yml` and wire the
existing capture step's error policy and exit behavior to that input.
Reject gate=true with run-e2e=false rather than silently skipping it.
Retain cell enumeration, per-cell reseeding, all capture checks, command/output
inputs and artifact uploads. No broad allow-list, changed caller defaults,
deployment, secret, dependency or unrelated workflow changes. App-side opt-in
belongs to INNO-E2E-6, after INNO-FIX-11/12/13 land; it is not implemented here.
Tier3: shared CI behavior. No frontend/prototype. Risk: a swallowed failure can
claim a false green, while a changed default would unexpectedly block all apps.

## Required tests to write

Extend the existing structural/shell-fixture tests for the input/default and
invalid combination, conditional failure policy and always-upload contract.
Exercise the actual extracted capture shell with harmless Docker stubs: enabled
success/failure, disabled failure, enumeration failure, empty cells and reseed
failure. Keep existing E2E workflow and mail-copy guards. No real containers.

# B. Implementation map — ADDRESSED TO THE IMPLEMENTER

ROLE: implementation (CI source and scoped tests, one instance).
Target cwd: `C:/Users/Micha Bigler/Documents/webapps/workflow-templates`.
Native inherited bootstrap cwd is workspace root; use absolute assigned paths
and explicit workdir. You are not alone; preserve other edits and use UTF-8.

Own only:
- `.github/workflows/app-ci.yml`: inputs149-167, validate-inputs184-204,
  capture790-846, upload848-855 only where necessary for this contract.
- `.github/scripts/test_app_ci_e2e_job.py`: existing input/step tests and
  `step_body`/`shell_body` helpers; add scoped capture shell fixtures here.

Current capture step has `if: always()` and `continue-on-error: true`, enumerates
manifest cells with a Docker node command, then loops over cells. Enumeration
failure and empty output warn and exit0; reseed and capture failures accumulate
`worst`; the final command always exits0. That is why app capture process exit1
cannot currently make ci/e2e red. The app runner already returns1 on failures.
Existing upload follows with `if: always()` and retention/output settings;
preserve it. Existing tests inspect live YAML text and execute extracted shell
against stubs, not a generic parser. Only the two named files are needed.
Do not touch other jobs, workflow_call trigger, quarantine/spec execution,
Docker lifecycle design or WFT-CI-36 guarded mail-copy behavior.

## Preamble

> This committed WO is the COMPLETE spec. Edit only assigned source/test files.
> Explicit UTF-8 I/O; preserve others' changes. No git/add/commit/push, package
> manager/install/lockfile, register/memory/governance/other-repo edits, review,
> agents, app startup, real Docker or environment/settings/secret reads. Never
> read .env. Run only the tests you wrote, at most once; no full-suite run.
> Your own run and self-review are not acceptance gates. Leave all changes
> uncommitted. PLAN first, PROGRESS before relevant actions with no silence
> beyond2min, final RESULT: DONE or BLOCKED with changed paths and actual run.

# C. Orchestrator only — NOT ADDRESSED TO THE IMPLEMENTER

> IMPLEMENTER STOP HERE. You ARE the implementation instance. Nothing below is your work.

Operator explicitly approved this additional shared-workflow scope2026-10-13
in this Codex chat, after the concrete plan. Initial implementation MUST use
fresh configured codex/gpt-6-luna, explicit model and no inherited context.
Record accepted ID/requested model; effective native resolution only if exposed.
Assigned paths are scope, not technical containment. Parent owns integration
and fixes review findings; do not re-dispatch an initial author for fixes.
Four configured read-only review lenses concurrently, full scoped context;
no UI/auth lens because no frontend/auth changes. Parent targeted pass is additional.
Run affected test file, not whole suite. Pair candidate refs in this repo and
innoservice; app points at app-ci.yml@ci-test/WFT-CI-37. Prove gate=true green
and deliberate capture failure red with artifact retained before main landing.
Respect estate CI budget; the earlier ONE exception was consumed by run38444366349,
not reusable here. App opt-in waits for FIX11/12/13 landing. No app promotion.

Accepted initial native instance `/root/capture_gate_initial`, explicit requested
gpt-6-luna/high, fresh context; effective native model/provider metadata unexposed.
Delivered only the two assigned files, no commits. Own scoped class run6passed1skip
was not acceptance. Parent corrected integration findings: YAML colon quoting,
preserved original mail-test class ownership, live mutation fixture anchors,
and Windows-safe Bash capture fixtures with strict stub-only PATH.
Authoritative affected file:81tests79passed2pre-existing Linux-only mail skips;
all11 capture shell scenarios execute and pass, including earlier failure followed
by a successful cell. Gate-r1 four independent concurrent codex/gpt-6-luna/openai
read-only lenses:2 deduplicated raised,1 accepted,worst accepted defect.
Aggregation fixture gap accepted/fixed; requiring unit execution of GitHub's
artifact upload rejected because the unchanged always-upload step is structurally
pinned and real negative paired CI must prove retained artifacts before landing.
Source-only corrected review and paired CI proofs remain pending; no main push.

Corrected gate-r2 accepted the concrete artifact ordering/action-stub countercase:
parsed YAML now pins capture < upload < cleanup, upload always(), real
upload-artifact action and exact artifact name/output path. Parent affected-file
run remains81tests79passed2pre-existing Linux mail skips, all11 capture shell
cases executed. Gate-r3 four fresh concurrent readonly codex/gpt-6-luna/openai
lenses completed: envelope/regression/duplication no findings. Tests R1 claimed
empty-cell exit mutation could escape; rejected against the actual enabled empty
case expecting exit1 versus disabled exit0 at test lines1035-1036, checked by
assertEqual(returncode, expected) at1094. Returning0 there fails the enabled
scenario; no requirement for additional self-mutation tests is in Part A.
Across rounds:3 deduplicated raised,2accepted,worst accepted defect. Source
review is complete; paired green/red CI and retained-artifact proof remain open.
Operator explicitly approved scoped source/WO/repo-context transmission to
configured OpenAI/Luna reviews and the74 E2E test capture images2026-10-13.

## Final gates and automatic publication

Final shared gate2026-10-14: explicit initial Luna authors retained; final four concurrent independent codex/gpt-6-luna/medium envelope/regression/duplication/tests all clean,0open. Unchanged affected file87passed2pre-existing Linux skips13subtests7.88s. Positive38601444526 allfive jobs/capture74green; regular-negative38610424378 only sentinel fails,23reseeds/22later files89cases pass/early artifacts retained; capture-negative38617202940 regulargreen,first cell2widths fail,72later records/artifact retained. [Final source/real-proof evidence](assets/WFT-CI-37-38-final-proof.json). App temporary faults removed locally; shared main publication automatic on these gates. Permanent app capture opt-in still follows FIX11/12/13 landing.
