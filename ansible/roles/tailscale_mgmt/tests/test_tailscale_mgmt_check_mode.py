"""WM-INF-108: a --check run must not trip over the post-up status assertions.

Under --check the `tailscale up` and the status commands are skipped (rc 0, empty output), so asserting on their
output fails on every check run of a host that has an auth key. The real tasks run here under a real
`ansible-playbook --check`; the guard removed from a copy is the negative control."""
import os
import subprocess
import sys
from pathlib import Path

import yaml

ROLE = Path(__file__).resolve().parents[1]
POST_UP = ("Re-check mgmt tailscaled state after up", "Assert mgmt tailscaled daemon responded with valid JSON",
           "Assert mgmt tailscaled BackendState is Running")


def post_up_tasks():
    tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text(encoding="utf-8"))
    return [task for name in POST_UP for task in tasks if task.get("name") == name]


def runnable(tasks):
    """The same tasks without `become`: the test host has no sudo, and --check skips the command anyway."""
    return [{key: value for key, value in task.items() if key != "become"} for task in tasks]


def run_check(tmp_path, tasks):
    play = [{"hosts": "all", "gather_facts": False,
             "vars": {"tailscale_mgmt_socket": "/nonexistent/tailscaled-mgmt.sock", "tailscale_mgmt_authkey": "synthetic"},
             "tasks": tasks}]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play, sort_keys=False), encoding="utf-8")
    (tmp_path / "hosts.yml").write_text(yaml.safe_dump(
        {"all": {"hosts": {"mgmt": {"ansible_connection": "local"}}}}), encoding="utf-8")
    (tmp_path / "ansible.cfg").write_text("[defaults]\nretry_files_enabled=False\n", encoding="utf-8")
    proc = subprocess.run([sys.executable, "-I", "-m", "ansible.cli.playbook", "-i", str(tmp_path / "hosts.yml"),
                           "--check", str(tmp_path / "play.yml")], cwd=tmp_path, text=True, capture_output=True,
                          env={**os.environ, "ANSIBLE_CONFIG": str(tmp_path / "ansible.cfg"), "ANSIBLE_NOCOLOR": "1"},
                          timeout=60)
    return proc.returncode, proc.stdout + proc.stderr


def test_the_three_post_up_tasks_exist_and_are_guarded_for_check_mode():
    tasks = post_up_tasks()
    assert [task["name"] for task in tasks] == list(POST_UP)
    for task in tasks:
        assert "not ansible_check_mode" in task["when"], task["name"]
        assert "tailscale_mgmt_authkey | string | length > 0" in task["when"], task["name"]


def test_a_real_check_run_passes_the_post_up_tasks(tmp_path):
    code, output = run_check(tmp_path, runnable(post_up_tasks()))
    assert code == 0, output


def test_without_the_guard_the_same_check_run_fails(tmp_path):
    unguarded = []
    for task in runnable(post_up_tasks()):
        task = dict(task)
        task["when"] ="tailscale_mgmt_authkey | string | length > 0"
        unguarded.append(task)
    code, output = run_check(tmp_path, unguarded)
    assert code != 0 and "did not respond to status query" in output, output
