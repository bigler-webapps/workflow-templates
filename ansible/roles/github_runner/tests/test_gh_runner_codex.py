"""Regression tests for the opt-in pinned Codex CLI install."""

from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined


ROLE = Path(__file__).resolve().parents[1]
DEFAULTS = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text(encoding="utf-8"))
TASKS = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text(encoding="utf-8"))


def named_task(name):
    return next(task for task in TASKS if task.get("name") == name)


def assert_codex_install_is_pinned(task):
    command = task["ansible.builtin.command"]["cmd"]
    assert command == "npm install -g @openai/codex@{{ github_runner_codex_version }}"
    assert "@openai/codex@" in command


def test_codex_default_is_empty_and_install_tasks_skip_when_unconfigured():
    assert DEFAULTS["github_runner_codex_version"] == ""
    env = Environment(undefined=StrictUndefined)
    for name in (
        "Check installed Codex CLI version",
        "Determine whether Codex CLI needs installation",
    ):
        condition = named_task(name)["when"]
        assert env.compile_expression(condition)(github_runner_codex_version="") is False
    install = named_task("Install configured Codex CLI release")
    # Ansible skips the task as soon as its first when condition is false.
    assert env.compile_expression(install["when"][0])(
        github_runner_codex_version=""
    ) is False


def test_codex_install_pins_exact_version_and_has_version_verification():
    install = named_task("Install configured Codex CLI release")
    assert_codex_install_is_pinned(install)

    verify = named_task("Verify configured Codex CLI version")
    assert verify["ansible.builtin.command"]["cmd"] == "codex --version"
    assert "github_runner_codex_version not in" in verify["failed_when"]

    # Mutation guard: an unpinned global install must fail the same assertion.
    mutated = {"ansible.builtin.command": {"cmd": "npm install -g @openai/codex"}}
    try:
        assert_codex_install_is_pinned(mutated)
    except AssertionError:
        pass
    else:
        raise AssertionError("the pin assertion accepted an unpinned Codex install")
