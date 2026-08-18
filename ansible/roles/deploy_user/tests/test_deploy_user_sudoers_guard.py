"""CI-13 — the sudoers task must be skippable for a caller with no general
sudo commands at all (dbpull), while every existing caller (deploy,
provision) keeps behaving exactly as before, since both always pass a
non-empty `deploy_user_sudo_commands`.

Run: pytest ansible/roles/deploy_user/tests/
"""

from pathlib import Path

import pytest
import yaml

ROLE = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def tasks():
    return yaml.safe_load((ROLE / "tasks" / "main.yml").read_text(encoding="utf-8"))


def _find_task(tasks, name_fragment):
    """Depth-first search, since the role's tasks live inside a `block:`."""
    stack = list(tasks)
    while stack:
        task = stack.pop(0)
        if not isinstance(task, dict):
            continue
        if name_fragment in (task.get("name") or ""):
            return task
        for key in ("block", "always", "rescue"):
            stack.extend(task.get(key) or [])
    return None


def test_sudoers_task_has_a_guard(tasks):
    task = _find_task(tasks, "Configure passwordless maintenance sudoers entry")
    assert task is not None, "the sudoers task is missing"
    assert "when" in task, "the sudoers task must be skippable for an empty command list"


def test_guard_defaults_before_checking_length(tasks):
    # `| length` on an UNDEFINED variable raises in Jinja -- the guard must
    # default first, not just check length, or a caller that omits the var
    # entirely (rather than passing []) would error instead of skipping.
    task = _find_task(tasks, "Configure passwordless maintenance sudoers entry")
    guard = task["when"]
    assert "default([])" in guard.replace(" ", "")
    assert "length > 0" in guard or "length>0" in guard.replace(" ", "")


def test_guard_references_the_right_variable(tasks):
    task = _find_task(tasks, "Configure passwordless maintenance sudoers entry")
    assert "deploy_user_sudo_commands" in task["when"]
