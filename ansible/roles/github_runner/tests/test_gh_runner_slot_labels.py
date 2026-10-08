"""Regression tests for optional per-slot GitHub runner labels."""

import re
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined


ROLE = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = ROLE / "templates" / "jit-config.sh.j2"
TEMPLATE = TEMPLATE_PATH.read_text(encoding="utf-8")
DEFAULTS = (ROLE / "defaults" / "main.yml").read_text(encoding="utf-8")
TASKS = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text(encoding="utf-8"))


def render(source, extra_labels):
    env = Environment(undefined=StrictUndefined, keep_trailing_newline=True, trim_blocks=True)  # Ansible template module defaults: trim_blocks=True
    variables = {
        name: "dummy"
        for name in re.findall(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}", source)
    }
    variables.update(
        github_runner_app_id="app-id",
        github_runner_app_installation_id="install-id",
        github_runner_org="example-org",
        github_runner_base_dir="/opt/gh-runner",
        github_runner_group_id=1,
        github_runner_labels="self-hosted,netcup",
        github_runner_slot_extra_labels=extra_labels,
    )
    return env.from_string(source).render(**variables)


def assert_empty_render_golden(output):
    """The old rendering has the base LABELS line directly followed by GH_API."""
    assert 'LABELS="self-hosted,netcup"\nGH_API="https://api.github.com"' in output
    assert "case \"${SLOT}\" in" not in output
    assert 'SLOT="${RUNNER_NAME##*-slot}"' not in output


def test_empty_mapping_keeps_the_pre_change_registration_rendering():
    rendered = render(TEMPLATE, {})
    assert_empty_render_golden(rendered)

    # Mutation guard: making the conditional unconditional must violate the
    # pinned empty-render golden, even though the mapping itself is empty.
    mutated = TEMPLATE.replace(
        "{% if github_runner_slot_extra_labels %}", "{% if true %}"
    )
    assert mutated != TEMPLATE
    try:
        assert_empty_render_golden(render(mutated, {}))
    except AssertionError:
        pass
    else:
        raise AssertionError("the empty-render golden accepted an always-emitted block")


def test_empty_mapping_renders_byte_identical_to_the_template_without_the_block():
    # Cut the whole `{% if github_runner_slot_extra_labels %} ... {% endif %}` span out of the source:
    # what remains is the pre-change template. The full rendering must match it exactly (every byte).
    block = re.compile(r"{% if github_runner_slot_extra_labels %}.*?{% endif %}\n", re.DOTALL)
    assert block.search(TEMPLATE)
    pre_change = block.sub("", TEMPLATE, count=1)
    assert render(TEMPLATE, {}) == render(pre_change, {})
    assert render(TEMPLATE, {2: "netcup-heavy"}) != render(pre_change, {})


def test_mapping_adds_one_label_arm_only_for_the_named_slot():
    rendered = render(TEMPLATE, {2: "netcup-heavy"})
    arms = re.findall(r"^\s*(\d+)\) LABELS=", rendered, re.MULTILINE)
    assert arms == ["2"]
    assert '2) LABELS="${LABELS},netcup-heavy" ;;' in rendered
    assert "netcup-heavy" not in render(TEMPLATE, {})
    # The runtime case is exact-match dispatch, so unmatched slots have no arm.
    assert "  1) LABELS=" not in rendered
    assert "  3) LABELS=" not in rendered


def test_defaults_declare_an_empty_slot_label_mapping():
    parsed = yaml.safe_load(DEFAULTS)
    assert parsed["github_runner_slot_extra_labels"] == {}


def test_slot_label_assertion_precedes_deploy_and_rejects_unsafe_inputs():
    task_names = [task.get("name") for task in TASKS]
    assertion_index = task_names.index("Assert per-slot runner labels are safe and in range")
    deploy_index = task_names.index("Deploy JIT-config broker script")
    assert assertion_index < deploy_index

    assertion = TASKS[assertion_index]
    assert "ansible.builtin.assert" in assertion
    conditions = assertion["ansible.builtin.assert"]["that"]
    condition_text = " ".join(conditions)
    assert "select('integer')" in condition_text
    assert "select('lt', 1)" in condition_text
    assert "select('gt', github_runner_slots)" in condition_text

    pattern_match = re.search(r"\^\[A-Za-z0-9_.-\].*?\$", condition_text)
    assert pattern_match, "assert task must constrain labels to the shell-safe pattern"
    label_pattern = re.compile(pattern_match.group())
    for unsafe in ("a b", "a;b", "$(x)"):
        assert not label_pattern.fullmatch(unsafe)
    assert label_pattern.fullmatch("netcup-heavy,extra.label_1")

    # Evaluate the REAL assert conditions from the YAML (Ansible's `match` test emulated with re.match).
    env = Environment(undefined=StrictUndefined)
    env.tests["match"] = lambda value, pattern: re.match(pattern, str(value)) is not None

    def passes(mapping, slots=3):
        for condition in conditions:
            expression = env.compile_expression(condition)
            if not expression(github_runner_slot_extra_labels=mapping, github_runner_slots=slots):
                return False
        return True

    assert passes({})
    assert passes({2: "netcup-heavy", 3: "a,b.c_d"})
    assert not passes({"2": "netcup-heavy"})      # non-integer key
    assert not passes({4: "netcup-heavy"})        # beyond github_runner_slots
    assert not passes({0: "netcup-heavy"})        # below 1
    for bad in ("a b", "a;b", "$(x)", 'a"b', "", "a,,b"):
        assert not passes({2: bad}), bad
    assert not passes(["netcup-heavy"])           # not a mapping


def test_rendered_script_is_valid_bash_with_and_without_a_mapping(tmp_path):
    # The first version of this template glued `esac` to the next line under Ansible's trim_blocks=True and
    # only the --check --diff of the real provision showed it. Render exactly like Ansible and syntax-check.
    import shutil
    import subprocess

    bash = shutil.which("bash")
    if not bash:
        import pytest
        pytest.skip("bash unavailable")
    for name, mapping in (("empty", {}), ("mapped", {1: "a", 3: "netcup-heavy"})):
        script = tmp_path / f"{name}.sh"
        script.write_bytes(render(TEMPLATE, mapping).encode("utf-8"))
        result = subprocess.run([bash, "-n", str(script)], capture_output=True, text=True)
        assert result.returncode == 0, (name, result.stderr)


def test_every_block_tag_sits_on_its_own_line_so_trim_blocks_cannot_glue_lines():
    # With trim_blocks the newline after a block tag is removed: a tag sharing a line with script text would
    # swallow that line's end. Pin that the slot-label tags are alone on their lines.
    for number, text in enumerate(TEMPLATE.splitlines(), 1):
        if "{%" in text and not text.lstrip().startswith("#"):
            assert re.fullmatch(r"\s*{%[^%]*%}\s*", text), (number, text)


def test_empty_mapping_keeps_the_original_adjacent_lines_under_ansible_settings():
    # The ORIGINAL (pre-WM-INF-79) template had these two lines directly adjacent; pinned independent of the
    # current block layout.
    assert 'LABELS="self-hosted,netcup"\nGH_API="https://api.github.com"\n' in render(TEMPLATE, {})
