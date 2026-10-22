"""Focused coverage for the exact shared routine CLI provisioning contract."""

import json
import re
import os
import subprocess
import sys
from pathlib import Path

import yaml
import pytest
from jinja2 import Environment, StrictUndefined
from jinja2.exceptions import TemplateError


ROLE = Path(__file__).resolve().parents[1]
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text(encoding="utf-8"))
TASKS = yaml.safe_load((ROLE / "tasks/ci.yml").read_text(encoding="utf-8"))
SERVICE = (ROLE / "templates/gh-runner@.service.j2").read_text(encoding="utf-8")
CONTRACT = json.loads((ROLE / "files/routine-cli.json").read_text(encoding="utf-8"))


def named(name):
    return next(task for task in TASKS if task.get("name") == name)


def test_role_defaults_derive_selected_values_from_one_role_manifest():
    assert CONTRACT == {"schema_version": 1, "package": "@anthropic-ai/claude-code",
                        "version": "2.1.308", "node_major": 22, "prefix": "/usr/local"}
    assert "github_runner_routine_cli_contract.node_major" in DEFAULTS["github_runner_node_major"]
    assert "github_runner_routine_cli_contract.version" in DEFAULTS["github_runner_claude_code_version"]
    first = TASKS[0]
    assert first["name"] == "Assert required variables are set"
    assert "github_runner_claude_code_version == github_runner_routine_cli_contract.version" in first["ansible.builtin.assert"]["that"]
    assert "github_runner_node_major is integer and github_runner_node_major == github_runner_routine_cli_contract.node_major" in first["ansible.builtin.assert"]["that"]


def test_node_22_install_precedes_claude_inspection_and_exact_install():
    names = [task.get("name") for task in TASKS]
    node_install = names.index("Install Node.js and npm from NodeSource")
    inspect = names.index("Check installed Claude Code CLI version")
    reconcile = names.index("Determine whether Claude Code CLI needs installation")
    install = names.index("Install configured Claude Code CLI release")
    verify = names.index("Verify exact Claude Code CLI executable version")
    assert node_install < inspect < reconcile < install < verify
    assert named("Install configured Claude Code CLI release")["ansible.builtin.command"]["cmd"] == (
        "npm install -g --prefix {{ github_runner_routine_cli_contract.prefix }} @anthropic-ai/claude-code@{{ github_runner_claude_code_version }}")
    assert "latest" not in str(named("Assert required variables are set"))
    assert "not in" not in str(named("Determine whether Claude Code CLI needs installation"))
    assert named("Verify exact Claude Code CLI executable version")["ansible.builtin.command"]["cmd"] == "{{ github_runner_routine_cli_contract.prefix }}/bin/claude --version"


def test_existing_install_reconciliation_uses_full_metadata_version():
    expression = named("Determine whether Claude Code CLI needs installation")["ansible.builtin.set_fact"]["_github_runner_claude_needs_install"]
    jinja = Environment(undefined=StrictUndefined)
    jinja.filters['from_json'] = json.loads
    evaluate = jinja.compile_expression(expression.strip().removeprefix("{{").removesuffix("}}").strip())
    correct = json.dumps({"dependencies": {"@anthropic-ai/claude-code": {"version": "2.1.308"}}})
    old = json.dumps({"dependencies": {"@anthropic-ai/claude-code": {"version": "2.1.307"}}})
    absent = json.dumps({"dependencies": {}})
    assert evaluate(_github_runner_claude_installed={"rc": 0, "stdout": correct},
                    github_runner_claude_code_version="2.1.308") is False
    assert evaluate(_github_runner_claude_installed={"rc": 0, "stdout": old},
                    github_runner_claude_code_version="2.1.308") is True
    assert evaluate(_github_runner_claude_installed={"rc": 1, "stdout": absent},
                    github_runner_claude_code_version="2.1.308") is True
    with pytest.raises((TemplateError, ValueError)):
        evaluate(_github_runner_claude_installed={"rc": 0, "stdout": "not-json"},
                 github_runner_claude_code_version="2.1.308")


def test_executable_verification_is_exact_and_service_disables_updater():
    verify = named("Verify exact Claude Code CLI executable version")
    assert "stdout not in" in verify["failed_when"]
    assert verify['ansible.builtin.command']['strip_empty_ends'] is False
    assert named('Install Node.js and npm from NodeSource')['ansible.builtin.apt']['name'] == 'nodejs={{ github_runner_node_major }}.*'
    assert verify["environment"] == {"DISABLE_AUTOUPDATER": "1"}
    assert "Environment=DISABLE_AUTOUPDATER=1" in SERVICE
    assert "github_runner_codex_version" not in str(verify)


def test_codex_install_and_selection_remain_unchanged():
    codex_check = named("Check installed Codex CLI version")
    codex_install = named("Install configured Codex CLI release")
    assert codex_check["ansible.builtin.command"]["cmd"] == "npm list -g @openai/codex --depth=0 --json"
    assert codex_install["ansible.builtin.command"]["cmd"] == "npm install -g @openai/codex@{{ github_runner_codex_version }}"
    assert codex_check["when"] == "github_runner_codex_version | length > 0"


@pytest.mark.parametrize('version', ['latest','next','^2.1.308','2.1.x','2.1.3080',' 2.1.308','2.1.307'])
def test_effective_override_refuses_before_first_mutating_task(version):
    env = Environment(undefined=StrictUndefined)
    env.filters['from_json'] = json.loads
    env.tests['match'] = lambda value, pattern: bool(re.match(pattern, str(value)))
    values = {'github_runner_routine_cli_contract': CONTRACT,
              'github_runner_claude_code_version': version,
              'github_runner_node_major': CONTRACT['node_major'],
              'github_runner_app_id': 'synthetic', 'github_runner_app_private_key': 'synthetic',
              'github_runner_app_installation_id': 'synthetic',
              'github_runner_sha256': 'sha256:'+'a'*64,
              'role_path': str(ROLE),
              'lookup': lambda *_args: json.dumps(CONTRACT)}
    conditions = TASKS[0]['ansible.builtin.assert']['that']
    assert not all(env.compile_expression(c)(**values) for c in conditions)
    values['github_runner_claude_code_version'] = CONTRACT['version']
    assert all(env.compile_expression(c)(**values) for c in conditions)
    values['github_runner_node_major'] = 20
    assert not all(env.compile_expression(c)(**values) for c in conditions)


@pytest.mark.parametrize(('output','stderr','rc','failed'),[
    ('2.1.308 (Claude Code)','',0,False),
    ('2.1.308 (Claude Code)\n','',0,False),
    ('2.1.308 (Claude Code)\r\n','',0,False),
    ('2.1.3080 (Claude Code)','',0,True),
    ('2.1.308 (Claude Code)\n\n','',0,True),
    (' 2.1.308 (Claude Code)','',0,True),
    ('2.1.308 (Claude Code)','warning',0,True),
    ('2.1.308 (Claude Code)','',1,True),
])
def test_actual_executable_verification_expression(output,stderr,rc,failed):
    evaluate=Environment(undefined=StrictUndefined).compile_expression(
        named('Verify exact Claude Code CLI executable version')['failed_when'])
    assert evaluate(github_runner_claude_code_version=CONTRACT['version'],
                    _github_runner_claude_version_check={'stdout':output,'stderr':stderr,'rc':rc}) is failed


def test_actual_ansible_assert_and_default_lookup_without_host_mutations(tmp_path):
    # Execute the actual validation task under real Ansible/native templating.
    play = [{'hosts':'localhost','gather_facts':False,
             'vars': {**DEFAULTS, 'role_path':str(ROLE), 'github_runner_app_id':'synthetic',
                      'github_runner_app_private_key':'synthetic',
                      'github_runner_app_installation_id':'synthetic',
                      'github_runner_sha256':'sha256:'+'a'*64},
             'tasks':[TASKS[0]]}]
    source=tmp_path/'assert.yml';source.write_text(yaml.safe_dump(play))
    env={'PATH':str(Path(sys.executable).parent)+':/usr/bin:/bin',
         'HOME':str(tmp_path),'ANSIBLE_LOCAL_TEMP':str(tmp_path/'ansible-local'),
         'ANSIBLE_REMOTE_TEMP':str(tmp_path/'ansible-remote'),
         'ANSIBLE_NOCOLOR':'1'}
    run=subprocess.run([str(Path(sys.executable).with_name('ansible-playbook')),
                        '-i','localhost,','-c','local',str(source)],env=env,
                       cwd=tmp_path,capture_output=True,text=True,timeout=30)
    assert run.returncode==0,run.stdout+run.stderr


@pytest.mark.parametrize(('version','accepted'),[
    ('v22.23.4',True), ('v20.20.1',False),('v25.9.0',False),
    ('v22.23.4\nuntrusted',False),('unavailable',False)])
def test_actual_ansible_node_gate_refuses_wrong_runtime(tmp_path,version,accepted):
    node=tmp_path/'node'
    node.write_text('#!/bin/sh\nprintf "%s\\n" '+repr(version)+'\n')
    node.chmod(0o700)
    gate=dict(named('Verify installed Node.js major before installing Claude Code'))
    gate.pop('become',None)
    gate['environment']={'PATH':str(tmp_path)+':/usr/bin:/bin'}
    play=[{'hosts':'localhost','gather_facts':False,
           'vars':{'github_runner_node_major':CONTRACT['node_major']},'tasks':[gate]}]
    source=tmp_path/'node.yml';source.write_text(yaml.safe_dump(play))
    env={'PATH':str(Path(sys.executable).parent)+':/usr/bin:/bin','HOME':str(tmp_path),
         'ANSIBLE_LOCAL_TEMP':str(tmp_path/'local'),
         'ANSIBLE_REMOTE_TEMP':str(tmp_path/'remote'),'ANSIBLE_NOCOLOR':'1'}
    run=subprocess.run([str(Path(sys.executable).with_name('ansible-playbook')),
                       '-i','localhost,','-c','local',str(source)],env=env,cwd=tmp_path,
                       capture_output=True,text=True,timeout=30)
    assert (run.returncode==0) is accepted,run.stdout+run.stderr
