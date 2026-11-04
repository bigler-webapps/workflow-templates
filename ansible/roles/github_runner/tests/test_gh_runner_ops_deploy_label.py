"""Regression tests for the fixed operations deployment-runner label."""

import re

from jinja2 import Environment, StrictUndefined
import pytest

from test_gh_runner_operations import DEFAULTS, ROLE, render as render_operations, run_role


CI_TEMPLATE_PATH = ROLE / "templates" / "jit-config.sh.j2"
CI_TEMPLATE = CI_TEMPLATE_PATH.read_text(encoding="utf-8")
OPTIONAL_SLOT_LABEL_BLOCK = re.compile(
    r"{% if github_runner_slot_extra_labels %}.*?{% endif %}\n", re.DOTALL
)


def render_ci(source=CI_TEMPLATE):
    environment = Environment(
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
    )
    return environment.from_string(source).render(dict(DEFAULTS))


def test_operations_launcher_sends_fixed_inventory_independent_labels():
    rendered = render_operations("operations-launcher.sh.j2")
    assert 'readonly LABELS="self-hosted,ops-deploy"' in rendered
    assert (
        'jit_config="$("$ISSUER" --repository "$REPOSITORY" --runner-name '
        '"$RUNNER" --labels "$LABELS")" || fail "issuer client failed"'
    ) in rendered

    for overrides in (
        {"github_runner_labels": "self-hosted,inventory-label"},
        {"github_runner_slot_extra_labels": {1: "inventory-label"}},
    ):
        assert render_operations("operations-launcher.sh.j2", overrides) == rendered


def test_default_ci_jit_render_matches_empty_extra_labels_baseline():
    assert DEFAULTS["github_runner_profile"] == "ci"
    rendered = render_ci()
    block = OPTIONAL_SLOT_LABEL_BLOCK.search(CI_TEMPLATE)
    assert block, "the pinned pre-change template must include the optional slot-label block"
    legacy_template = CI_TEMPLATE[:block.start()] + CI_TEMPLATE[block.end():]
    baseline = render_ci(legacy_template)

    # The existing template hash test pins the complete CI source; this render
    # comparison proves the default empty mapping adds no rendered bytes.
    assert rendered.encode("utf-8") == baseline.encode("utf-8")
    assert 'LABELS="self-hosted,netcup"' in rendered
    assert "ops-deploy" not in rendered.lower()


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        pytest.param("github_runner_labels", "self-hosted,ops-deploy", id="base-label"),
        pytest.param("github_runner_labels", "self-hosted,OpS-DePlOy", id="base-label-case"),
        pytest.param("github_runner_slot_extra_labels", {2: "netcup,OPS-DEPLOY"}, id="slot-label-case"),
        pytest.param("github_runner_labels", " self-hosted , OpS-DePlOy ", id="base-label-case-space"),
        pytest.param("github_runner_slot_extra_labels", {2: "netcup,ops-deploy"}, id="slot-label"),
        pytest.param("github_runner_slot_extra_labels", {2: " netcup , oPs-DePlOy "}, id="slot-label-case-space"),
    ],
)
def test_ci_profile_rejects_ops_deploy_from_inventory_before_profile_tasks(
    tmp_path, variable, value
):
    overrides = {
        "github_runner_profile": "ci",
        "github_runner_slots": 3,
        variable: value,
    }
    code, output = run_role(
        tmp_path,
        overrides,
        hostname="ci-host",
        groups=["runners"],
        ci_probe=True,
    )
    assert code != 0, output
    assert "ops-deploy label is reserved" in output, output
    assert "PROFILE_CI_SELECTED" not in output, output
    assert "mock_module" not in output, output


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        pytest.param("github_runner_labels", "self-hosted, Ops-Deploy ", id="base-label"),
        pytest.param("github_runner_slot_extra_labels", {1: " netcup,OPS-DEPLOY "}, id="slot-label"),
    ],
)
def test_operations_profile_also_rejects_ops_deploy_in_ci_label_inputs(
    tmp_path, variable, value
):
    code, output = run_role(tmp_path, {variable: value})
    assert code != 0, output
    assert "ops-deploy label is reserved" in output, output
    assert "mock_module" not in output, output


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        pytest.param("github_runner_labels", 'x"; LABELS="ops-deploy', id="shell-injection"),
        pytest.param("github_runner_labels", "self-hosted;ops-deploy", id="semicolon"),
        pytest.param("github_runner_labels", ["ops-deploy"], id="base-list"),
        pytest.param("github_runner_slot_extra_labels", ["ops-deploy"], id="slot-list"),
        pytest.param("github_runner_slot_extra_labels", {1: ["ops-deploy"]}, id="slot-value-list"),
        pytest.param("github_runner_slot_extra_labels", {1: 'a"; LABELS="ops-deploy'}, id="slot-injection"),
    ],
)
def test_malformed_or_injected_label_inputs_are_refused_with_the_clear_message(
    tmp_path, variable, value
):
    overrides = {"github_runner_profile": "ci", "github_runner_slots": 3, variable: value}
    code, output = run_role(tmp_path, overrides, hostname="ci-host", groups=["runners"], ci_probe=True)
    assert code != 0, output
    assert "ops-deploy label is reserved" in output, output
    assert "PROFILE_CI_SELECTED" not in output, output
    assert "mock_module" not in output, output
