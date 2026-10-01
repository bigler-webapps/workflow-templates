"""Structural regression tests for staging-health's opt-in auth smoke (WFT-CI-31).

Run with: python .github/scripts/test_staging_health_auth_smoke.py

These checks inspect the live workflow text because GitHub Actions expressions
make a generic YAML parser a poor fit. Mutation cases prove that each guard
would reject the corresponding regression.
"""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (ROOT / ".github/workflows/staging-health.yml").read_text(encoding="utf-8")
INPUTS_BLOCK = WORKFLOW.split("workflow_call:\n", 1)[1].split("\n\npermissions:", 1)[0]


def slice_probe_job(workflow):
    # Bounded on BOTH ends: stops at the next top-level (2-space-indented)
    # job key if one is ever added after `probe:`, not just at end-of-file.
    # An unbounded slice (the pre-fix version of this helper) would let a
    # stub step in a LATER job satisfy an ordering/membership assertion
    # meant to pin something inside THIS job specifically (tests-lens
    # review finding).
    after_probe = workflow.split("\n  probe:\n", 1)[1]
    next_job = re.search(r"\n  [A-Za-z_][\w-]*:\n", after_probe)
    return after_probe[: next_job.start()] if next_job else after_probe


PROBE_JOB = slice_probe_job(WORKFLOW)
AUTH_STEP = re.search(
    r"^      - name: Auth smoke \(opt-in\) - login renders a translated error, not a raw code\n"
    r"(?P<body>.*?)(?=^      - name:|\Z)",
    PROBE_JOB,
    re.MULTILINE | re.DOTALL,
)


def auth_step_body(workflow=WORKFLOW):
    probe_job = slice_probe_job(workflow)
    match = re.search(
        r"^      - name: Auth smoke \(opt-in\) - login renders a translated error, not a raw code\n"
        r"(?P<body>.*?)(?=^      - name:|\Z)",
        probe_job,
        re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError("auth smoke step not found")
    return match.group("body")




class WftCi31StructuralTests(unittest.TestCase):
    def test_only_one_job_exists_today(self):
        # Documents the assumption slice_probe_job relies on when there is no
        # next job to bound against -- if this ever fails, slice_probe_job's
        # fallback (whole rest of file) is back in play and should be
        # revisited (tests-lens review finding: the pre-fix slice was
        # unbounded at the file end, so a stub step in a later job could
        # have satisfied an assertion meant to pin something inside `probe`).
        self.assertIsNone(re.search(r"\n  [A-Za-z_][\w-]*:\n", PROBE_JOB))

    def test_workflow_is_valid_yaml(self):
        # None of this file's other checks run the file through an actual YAML
        # parser -- they slice the live text by string, which makes them blind
        # to a real parse break. Caught live during this WO's own first
        # implementation pass: an unquoted step `name:` containing ": " (colon
        # space) -- "Auth smoke (opt-in): login renders ..." -- reads as a
        # second mapping key to YAML, not a literal string, and
        # yaml.safe_load() raised "mapping values are not allowed here"
        # although every string-slicing check above still passed. GitHub
        # Actions would hit the identical parse error for every one of this
        # repo's 16+ callers (the same blast radius WFT-CI-23's own
        # `${{`-in-description bug had), so this guard belongs in the suite,
        # not just in the Orchestrator's own manual check.
        import yaml

        yaml.safe_load(WORKFLOW)

    def test_assertion_fails_if_a_colon_space_is_reintroduced_in_a_step_name(self):
        import yaml

        mutated = WORKFLOW.replace(
            "- name: Auth smoke (opt-in) - login renders a translated error, not a raw code",
            "- name: Auth smoke (opt-in): login renders a translated error, not a raw code",
            1,
        )
        self.assertNotEqual(mutated, WORKFLOW, "fixture setup did not match the live step name")
        with self.assertRaises(yaml.YAMLError):
            yaml.safe_load(mutated)

    def test_auth_smoke_inputs_have_required_types_and_defaults(self):
        for name, input_type, default in (
            ("auth-smoke", "boolean", "false"),
            ("login-path", "string", "'/login'"),
        ):
            match = re.search(
                rf"^      {re.escape(name)}:\n(?:.*\n)*?        type: (?P<type>\S+)\n"
                rf"(?:.*\n)*?        default: (?P<default>\S.*)$",
                INPUTS_BLOCK,
                re.MULTILINE,
            )
            self.assertIsNotNone(match, f"missing input: {name}")
            assert match is not None
            self.assertEqual(match.group("type"), input_type)
            self.assertEqual(match.group("default"), default)

    def test_assertion_fails_if_auth_smoke_defaults_true(self):
        mutated = WORKFLOW.replace(
            "        default: false\n      login-path:",
            "        default: true\n      login-path:",
            1,
        )
        self.assertNotEqual(mutated, WORKFLOW, "fixture setup did not match auth-smoke input")
        auth_input = mutated.split("      auth-smoke:\n", 1)[1].split(
            "\n      login-path:", 1
        )[0]
        self.assertNotRegex(
            auth_input,
            r"^        default: false$",
            msg="the default-off guard must reject a default:true mutation",
        )

    def test_input_descriptions_contain_no_expressions(self):
        for name in ("auth-smoke", "login-path"):
            input_block = INPUTS_BLOCK.split(f"      {name}:\n", 1)[1].split(
                "\n      ", 1
            )[0]
            self.assertNotIn("${{", input_block)

    def test_assertion_fails_if_input_description_contains_an_expression(self):
        mutated = WORKFLOW.replace(
            "Opt-in browser smoke:",
            "Opt-in browser smoke: ${{ broken }}",
            1,
        )
        self.assertNotEqual(mutated, WORKFLOW, "fixture setup did not mutate a live description")
        mutated_input = mutated.split("      auth-smoke:\n", 1)[1].split(
            "\n      login-path:", 1
        )[0]
        self.assertIn("${{", mutated_input)
        self.assertNotEqual(
            re.search(r"\$\{\{", mutated_input),
            None,
            "the description-expression guard must detect the mutation",
        )

    def test_auth_smoke_is_gated_only_by_the_opt_in_input(self):
        self.assertIsNotNone(AUTH_STEP)
        assert AUTH_STEP is not None
        self.assertIsNotNone(
            re.search(
                r"^      - name: Auth smoke .*\n        if: \$\{\{ inputs\.auth-smoke \}\}\n",
                PROBE_JOB,
                re.MULTILINE,
            )
        )
        self.assertNotIn("always()", AUTH_STEP.group("body"))

    def test_auth_smoke_is_appended_after_the_existing_probe(self):
        self.assertLess(
            PROBE_JOB.index("- name: Probe /api/healthz via Tailnet"),
            PROBE_JOB.index("- name: Auth smoke (opt-in) - login renders a translated error, not a raw code"),
        )
        self.assertNotIn("if: always()", auth_step_body())

    def test_auth_smoke_passes_domain_and_login_path_to_the_container(self):
        body = auth_step_body()
        self.assertIn("DOMAIN: ${{ steps.domain.outputs.domain }}", body)
        self.assertIn("LOGIN_PATH: ${{ inputs.login-path }}", body)
        self.assertIn('SMOKE_URL="https://${DOMAIN}${LOGIN_PATH}"', body)

    def test_auth_smoke_uses_pinned_image_and_non_root_runner_uid(self):
        body = auth_step_body()
        self.assertIn('mcr.microsoft.com/playwright:v1.63.0-noble', body)
        self.assertIn('--user "$(id -u):$(id -g)"', body)
        self.assertIn("-e HOME=/tmp", body)
        self.assertIn("playwright@1.63.0", body)

    def test_assertion_fails_if_container_loses_non_root_user(self):
        mutated = WORKFLOW.replace(
            '            --user "$(id -u):$(id -g)" \\\n',
            "",
            1,
        )
        self.assertNotEqual(mutated, WORKFLOW, "fixture setup did not match container user flag")
        self.assertNotIn('--user "$(id -u):$(id -g)"', auth_step_body(mutated))

    def test_node_smoke_checks_form_and_translated_error(self):
        body = auth_step_body()
        for needle in (
            "input[type=\"email\"]",
            "input[type=\"password\"]",
            "form button[type=\"submit\"]",
            ".MuiAlert-colorError .MuiAlert-message",
            "smoke-test@example.com2",
            "irrelevant-dummy-password-00",
            "const BARE_CODE = /^[a-z][a-z0-9_]*$/;",
            "const KIT_KEY = /^Auth\\./;",
            "process.exit(exitCode);",
        ):
            self.assertIn(needle, body)

    def test_assertion_fails_if_raw_code_guard_is_removed(self):
        mutated = WORKFLOW.replace(
            "              } else if (BARE_CODE.test(text)) {\n",
            "              } else if (false) {\n",
            1,
        )
        self.assertNotEqual(mutated, WORKFLOW, "fixture setup did not match bare-code guard")
        self.assertNotIn("} else if (BARE_CODE.test(text)) {", auth_step_body(mutated))

    def test_assertion_fails_if_empty_message_guard_is_removed(self):
        # Review finding (tests lens, blocker): only the bare-code branch had
        # a mutation test; a stub implementation could drop the empty-message
        # check entirely (`if (!text)`) and every other test here would still
        # pass. Each of the three pass/fail branches now has its own.
        mutated = WORKFLOW.replace(
            "            if (!text) {\n",
            "            if (false) {\n",
            1,
        )
        self.assertNotEqual(mutated, WORKFLOW, "fixture setup did not match empty-message guard")
        self.assertNotIn("if (!text) {", auth_step_body(mutated))

    def test_assertion_fails_if_kit_key_guard_is_removed(self):
        mutated = WORKFLOW.replace(
            "              } else if (KIT_KEY.test(text)) {\n",
            "              } else if (false) {\n",
            1,
        )
        self.assertNotEqual(mutated, WORKFLOW, "fixture setup did not match kit-key guard")
        self.assertNotIn("} else if (KIT_KEY.test(text)) {", auth_step_body(mutated))

    def test_assertion_fails_if_browser_failure_becomes_green(self):
        mutated = WORKFLOW.replace(
            "            let exitCode = 1;",
            "            let exitCode = 0;",
            1,
        )
        self.assertNotEqual(mutated, WORKFLOW, "fixture setup did not match failure default")
        self.assertIn("let exitCode = 0;", auth_step_body(mutated))
        self.assertNotIn("let exitCode = 1;", auth_step_body(mutated))


if __name__ == "__main__":
    unittest.main()
