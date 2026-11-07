"""Offline security/selection tests; every target mutation is instrumented out."""
import hashlib
import base64
import configparser
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

ROLE = Path(__file__).resolve().parents[1]
DEFAULTS = yaml.safe_load((ROLE / 'defaults/main.yml').read_text(encoding='utf-8'))
MAIN = yaml.safe_load((ROLE / 'tasks/main.yml').read_text(encoding='utf-8'))
OPS = yaml.safe_load((ROLE / 'tasks/operations.yml').read_text(encoding='utf-8'))
ACTIVATION_SHA256 = 'a' * 64


def values(**overrides):
    result = {**DEFAULTS,
        'github_runner_profile': 'operations', 'github_runner_slots': 1,
        'github_runner_operations_packages': ['git', 'openssh-client', 'rsync', 'python3', 'curl', 'jq'],
        'getent_group': {},
        'getent_passwd': {'gh-operations': ['x', '1234', '1234', '', '/unused', '/usr/sbin/nologin']},
        'ansible_facts': {'services': {}, 'packages': {
            'git': [{'name': 'git', 'version': '1:2.43.0-1ubuntu7.5'}],
            'curl': [{'name': 'curl', 'version': '8.5.0-2ubuntu10.17'}],
            'jq': [{'name': 'jq', 'version': '1.7.1-3ubuntu0.24.04.2'}],
            'rsync': [{'name': 'rsync', 'version': '3.2.7-1ubuntu1.5'}],
            'python3': [{'name': 'python3', 'version': '3.12.3-0ubuntu2.1'}],
            'openssh-client': [{'name': 'openssh-client', 'version': '1:9.6p1-3ubuntu13.20'}],
        }},
        'mock_registers': {
            '_operations_issuer_socket': {'stdout': 'active'},
            '_operations_issuer_client_stat': {'stat': {
                'exists': True, 'isreg': True, 'pw_name': 'root', 'checksum': ACTIVATION_SHA256}},
            '_operations_runner_stat': {'stat': {'exists': True, 'isreg': True}},
        },
    }
    result.update(overrides)
    return result


def render(name, overrides=None):
    return Environment(undefined=StrictUndefined, keep_trailing_newline=True).from_string(
        (ROLE / 'templates' / name).read_text(encoding='utf-8')).render(
            values(**(overrides or {})))


def test_unchanged_jit_template_remains_exact_baseline():
    # Baseline of the local CI broker. Last matched 8ebd4e2 (WM-INF-79) and went stale unnoticed afterwards.
    # Re-baselined 2026-11-05 after reading the whole diff of jit-config.sh.j2 since 8ebd4e2; the only changes are
    #   da6bae4  WM-INF-102  drain gate: wait while /run/gh-runner.drain is valid, and the --deregister-if-idle mode
    #   70b775c  WM-INF-102  line endings restored to LF (no content change)
    #   d192671  WM-INF-105  generate-jitconfig names its HTTP status in the journal (curl status as the last line)
    # The next change to this template must update the digest in the same commit.
    expected = {
        'templates/jit-config.sh.j2': 'f5ff55989ec9424c6b6afe631dcdb60aea8cbc7cbd569a0362544091a66c5828',
    }
    for name, digest in expected.items():
        data = (ROLE / name).read_bytes().replace(b'\r\n', b'\n')
        # Template fixtures were measured on CRLF Windows checkouts.
        if name.startswith('templates/'):
            data = data.replace(b'\n', b'\r\n')
        assert hashlib.sha256(data).hexdigest() == digest, name
    assert DEFAULTS['github_runner_profile'] == 'ci'
    assert DEFAULTS['github_runner_operations_active'] is False
    # No exact version pins any more (an Ubuntu security update would break them); the list is names only.
    assert 'github_runner_operations_package_pins' not in DEFAULTS
    assert 'github_runner_operations_package_manifest_verified' not in DEFAULTS
    assert {'git', 'openssh-client', 'rsync', 'python3', 'curl', 'jq'} <= set(DEFAULTS['github_runner_operations_packages'])
    assert all('=' not in name for name in DEFAULTS['github_runner_operations_packages'])


def instrument(tasks):
    """Keep real assertions/includes/loops/when; replace host actions with safe fixtures.

    No user/file/apt/service/network module can run in this fixture. Most replaced
    module arguments pass through real Ansible templating and are retained in
    stdout; registered command/stat tasks receive the supplied fixture results.
    """
    result = []
    for source in tasks:
        task = dict(source)
        key = next(k for k in task if k.startswith('ansible.builtin.'))
        if key in ('ansible.builtin.assert', 'ansible.builtin.include_tasks', 'ansible.builtin.meta'):
            result.append(task)
            continue
        args = task.pop(key)
        task.pop('become', None)
        task.pop('notify', None)
        if key in ('ansible.builtin.command', 'ansible.builtin.stat') and 'register' in task:
            register = task.pop('register')
            task['ansible.builtin.set_fact'] = {
                register: "{{ mock_registers['" + register + "'] }}"}
            result.append(task)
            continue
        task['ansible.builtin.debug'] = {'msg': {'mock_module': key, 'arguments': args}}
        if key == 'ansible.builtin.get_url':
            task['changed_when'] = True
        result.append(task)
        if key == 'ansible.builtin.systemd' and task.get('name') == 'Stop the operations runner with explicit drain approval':
            fact_task = {
                'name': 'Mark the fixture operations service stopped after approved drain',
                'ansible.builtin.set_fact': {'_mock_operations_service_drain_complete': True},
            }
            if 'when' in task:
                fact_task['when'] = task['when']
            result.append(fact_task)
    return result


def run_role(tmp_path, overrides, *, hostname='ops', groups=None, ci_probe=False, check=False):
    fixture = tmp_path / 'fixture'
    tasks = fixture / 'tasks'
    tasks.mkdir(parents=True, exist_ok=True)
    for name, source in [('main.yml', MAIN), ('operations.yml', OPS),
                         ('ci.yml', ([{'ansible.builtin.debug': {'msg': 'PROFILE_CI_SELECTED'}}] if ci_probe else yaml.safe_load((ROLE / 'tasks/ci.yml').read_text(encoding='utf-8'))))]:
        (tasks / name).write_text(yaml.safe_dump(instrument(source), sort_keys=False), encoding='utf-8')
    inventory = {'all': {'children': {group: {'hosts': {hostname: {'ansible_connection': 'local'}}}
                                     for group in (groups or ['operations'])}}}
    (fixture / 'hosts.yml').write_text(yaml.safe_dump(inventory), encoding='utf-8')
    variables = values(**overrides)
    service = variables['ansible_facts']['services'].get('gh-operations.service')
    if service and service.get('state') == 'running':
        # The mock systemd stop sets this marker; keep the fixture fact dynamic
        # so the unchanged post-drain refusal assertion sees the stopped state.
        service['state'] = "{{ 'stopped' if _mock_operations_service_drain_complete | default(false) else 'running' }}"
    play = [{'hosts': 'all', 'gather_facts': False, 'vars': variables,
             'tasks': [{'ansible.builtin.include_tasks': str(tasks / 'main.yml')}]}]
    (fixture / 'play.yml').write_text(yaml.safe_dump(play), encoding='utf-8')
    config = fixture / 'ansible.cfg'
    config.write_text('[defaults]\nhost_key_checking=True\nretry_files_enabled=False\n', encoding='utf-8')
    proc = subprocess.run([sys.executable, '-I', '-m', 'ansible.cli.playbook', '-i', str(fixture / 'hosts.yml'),
                           *(['--check'] if check else []), str(fixture / 'play.yml')], cwd=fixture,
                          env={**os.environ, 'ANSIBLE_CONFIG': str(config), 'ANSIBLE_NOCOLOR': '1'},
                          text=True, capture_output=True, timeout=60)
    return proc.returncode, proc.stdout + proc.stderr


def activation_inputs(**overrides):
    result = {
        'github_runner_operations_active': True,
        'github_runner_operations_activation_authorized': True,
        'github_runner_operations_containment_verified': True,
        'github_runner_operations_issuer_client_verified': True,
        'github_runner_operations_issuer_client_sha256': ACTIVATION_SHA256,
    }
    result.update(overrides)
    return result


def mocked_modules(output):
    messages = []
    decoder = json.JSONDecoder()
    for start in re.finditer(r'=> (\{)', output):
        data, _ = decoder.raw_decode(output[start.start(1):])
        if isinstance(data.get('msg'), dict) and 'mock_module' in data['msg']:
            messages.append(data['msg'])
    return messages


@pytest.mark.parametrize('changes,hostname,groups', [
    ({'github_runner_profile': 'unknown'}, 'ops', ['operations']),
    ({'github_runner_operations_repository': 'bigler-webapps/other'}, 'ops', ['operations']),
    ({'github_runner_slots': 2}, 'ops', ['operations']),
    ({'github_runner_slots': 1.5}, 'ops', ['operations']),
    ({'github_runner_operations_slots': 1.5}, 'ops', ['operations']),
    ({'github_runner_app_private_key': 'synthetic-not-a-key'}, 'ops', ['operations']),
    ({'github_runner_operations_issuer_user': 'gh-operations'}, 'ops', ['operations']),
    pytest.param({'github_runner_user': 'gh-runner-issuer'}, 'ops', ['operations'], id='ci_user_is_issuer'),
    pytest.param({'github_runner_user': 'gh-operations-poller'}, 'ops', ['operations'], id='ci_user_is_poller'),
    ({'github_runner_operations_packages': []}, 'ops', ['operations']),
    ({'github_runner_operations_packages': ['git', 'curl']}, 'ops', ['operations']),
    ({'github_runner_operations_packages': 'git'}, 'ops', ['operations']),
    ({'github_runner_operations_packages': ['git', 'openssh-client', 'rsync', 'python3', 'curl', 'jq', 'curl=8.5.0']}, 'ops', ['operations']),
    ({'github_runner_operations_packages': ['git', 'openssh-client', 'rsync', 'python3', 'curl', 'jq', 'jq']}, 'ops', ['operations']),
    ({'github_runner_operations_active': True}, 'ops', ['operations']),
    ({}, 'ci-host', ['operations']),
    ({}, 'ops', ['operations', 'runners']),
])
def test_real_ansible_rejects_invalid_profile_before_any_host_module(tmp_path, changes, hostname, groups):
    code, output = run_role(tmp_path, changes, hostname=hostname, groups=groups)
    assert code != 0, output
    assert 'mock_module' not in output, output
    failed = re.search(r'"assertion": ("(?:\\.|[^"\\])*")', output)
    assert failed, output
    expected = ('github_runner_operations_activation_authorized'
                if changes == {'github_runner_operations_active': True}
                else next(iter(changes)) if changes else ('inventory_hostname' if hostname != 'ops' else 'group_names'))
    assert expected in json.loads(failed.group(1)), output


def test_activation_defaults_cannot_satisfy_the_gate(tmp_path):
    """The committed selector/evidence defaults keep operations inactive and cannot authorize activation."""
    assert DEFAULTS['github_runner_operations_active'] is False
    for name in ('github_runner_operations_activation_authorized',
                 'github_runner_operations_containment_verified',
                 'github_runner_operations_issuer_client_verified',
                 'github_runner_operations_drain_approved'):
        assert DEFAULTS[name] is False
    assert DEFAULTS['github_runner_operations_issuer_client_sha256'] == ''
    code, output = run_role(tmp_path, {})
    assert code == 0, output
    modules = mocked_modules(output)
    assert [message['arguments'] for message in modules
            if message['mock_module'] == 'ansible.builtin.systemd'] == [
                {'name': 'gh-operations.service', 'enabled': False, 'state': 'stopped'}]
    assert not [message for message in modules if message['mock_module'] == 'ansible.builtin.command']


@pytest.mark.parametrize('changes,expected', [
    ({'github_runner_operations_activation_authorized': False}, 'github_runner_operations_activation_authorized'),
    ({'github_runner_operations_containment_verified': False}, 'github_runner_operations_containment_verified'),
    ({'github_runner_operations_issuer_client_verified': False}, 'github_runner_operations_issuer_client_verified'),
    ({'github_runner_operations_issuer_client_sha256': ''}, 'github_runner_operations_issuer_client_sha256'),
    ({'github_runner_operations_issuer_client_sha256': 'not-a-digest'}, 'github_runner_operations_issuer_client_sha256'),
    ({'github_runner_operations_activation_authorized': 'true'}, 'github_runner_operations_activation_authorized'),
])
def test_activation_missing_or_non_boolean_evidence_refuses_before_host_modules(tmp_path, changes, expected):
    """Each activation evidence input must be present and strictly valid before any host module runs."""
    code, output = run_role(tmp_path, activation_inputs(**changes))
    assert code != 0, output
    assert 'Operations runner activation refused:' in output
    assert expected in output
    assert 'mock_module' not in output


def test_activation_selector_must_be_a_real_boolean(tmp_path):
    """A string-valued activation selector is rejected before host modules run."""
    code, output = run_role(tmp_path, activation_inputs(github_runner_operations_active='true'))
    assert code != 0, output
    assert 'github_runner_operations_active is boolean' in output
    assert 'mock_module' not in output


def test_activation_issuer_client_digest_mismatch_refuses_before_start(tmp_path):
    """The installed issuer client checksum must match its pinned digest before systemd starts the runner."""
    registers = values()['mock_registers']
    registers['_operations_issuer_client_stat']['stat']['checksum'] = 'b' * 64
    code, output = run_role(tmp_path, activation_inputs(mock_registers=registers))
    assert code != 0, output
    assert 'the issuer client must be a root-owned regular file' in output
    assert 'Start the operations runner through the activation gate' not in output


@pytest.mark.parametrize('registers,expected', [
    ({'_operations_issuer_socket': {'stdout': 'inactive'}}, 'the runner issuer socket on ops is not active'),
    ({'_operations_issuer_socket': {'stdout': 'active'},
      '_operations_issuer_client_stat': {'stat': {
          'exists': True, 'isreg': True, 'pw_name': 'root', 'checksum': ACTIVATION_SHA256}},
      '_operations_runner_stat': {'stat': {'exists': False, 'isreg': False}}},
     'the pinned runner archive is not unpacked'),
])
def test_activation_requires_live_issuer_socket_and_unpacked_runner(tmp_path, registers, expected):
    """Activation stops when the issuer socket is inactive or the pinned runner archive is absent."""
    code, output = run_role(tmp_path, activation_inputs(mock_registers=registers))
    assert code != 0, output
    assert expected in output
    assert 'Start the operations runner through the activation gate' not in output


def test_activation_starts_only_after_ordered_precondition_checks(tmp_path):
    """The issuer socket, pinned client and unpacked runner are checked before the enabled service starts."""
    code, output = run_role(tmp_path, activation_inputs())
    assert code == 0, output
    modules = mocked_modules(output)
    systemd = [message['arguments'] for message in modules
               if message['mock_module'] == 'ansible.builtin.systemd']
    assert systemd == [{'name': 'gh-operations.service', 'enabled': True, 'state': 'started'}]
    names = [
        'Require the runner issuer socket to be active before the operations runner starts',
        'Inspect the pinned runner issuer client before the operations runner starts',
        'Require the runner issuer client to match its pinned digest',
        'Inspect the unpacked runner before the operations runner starts',
        'Require the unpacked runner to be present',
        'Start the operations runner through the activation gate',
    ]
    positions = [output.index(name) for name in names]
    assert positions == sorted(positions)


def test_real_ansible_valid_profile_renders_isolated_inactive_installation(tmp_path):
    code, output = run_role(tmp_path, {})
    assert code == 0, output
    messages = []
    decoder = json.JSONDecoder()
    for start in re.finditer(r'=> (\{)', output):
        data, _ = decoder.raw_decode(output[start.start(1):])
        if isinstance(data.get('msg'), dict) and 'mock_module' in data['msg']:
            messages.append(data['msg'])
    def module_arguments(name):
        return [message['arguments'] for message in messages if message['mock_module'] == name]
    assert module_arguments('ansible.builtin.get_url') == [{
        'url': 'https://github.com/actions/runner/releases/download/v2.338.2/actions-runner-linux-x64-2.338.2.tar.gz',
        'dest': '/opt/gh-operations/cache/runner.tar.gz',
        'checksum': 'sha256:baebf204307bac6193480852732f0aba2ae97b6c5597c244f4d5558390147c40',
        'mode': '0644',
    }]
    assert module_arguments('ansible.builtin.unarchive') == [{
        'src': '/opt/gh-operations/cache/runner.tar.gz',
        'dest': '/opt/gh-operations/runner', 'remote_src': True,
        'owner': 'root', 'group': 'root', 'mode': '0755',
    }]
    apt = module_arguments('ansible.builtin.apt')
    assert len(apt) == 1
    assert apt[0]['state'] == 'present' and apt[0]['update_cache'] is True
    assert apt[0]['name'] == ['git', 'openssh-client', 'rsync', 'python3', 'curl', 'jq']
    # Names only: no exact version, and no repository or key is added by this role.
    assert all('=' not in name for name in apt[0]['name'])
    assert module_arguments('ansible.builtin.apt_repository') == []
    assert module_arguments('ansible.builtin.apt_key') == []
    assert module_arguments('ansible.builtin.package_facts') == [{'manager': 'apt'}]
    copies = {args['dest']: args for args in module_arguments('ansible.builtin.copy')}
    pref = copies['/etc/apt/preferences.d/gh-operations-archive-only.pref']
    assert (pref['owner'], pref['group'], pref['mode']) == ('root', 'root', '0644')
    assert 'Package: git openssh-client rsync python3 curl jq\nPin: origin pkgs.tailscale.com\nPin-Priority: -1\n' in pref['content']
    manifest = copies['/opt/gh-operations/package-manifest.txt']
    assert (manifest['owner'], manifest['group'], manifest['mode']) == ('root', 'root', '0644')
    lines = manifest['content'].splitlines()
    assert lines[0].startswith('# Installed by Ansible') and 'not a pin' in lines[0]
    assert lines[1:] == ['curl 8.5.0-2ubuntu10.17', 'git 1:2.43.0-1ubuntu7.5', 'jq 1.7.1-3ubuntu0.24.04.2',
                         'openssh-client 1:9.6p1-3ubuntu13.20', 'python3 3.12.3-0ubuntu2.1', 'rsync 3.2.7-1ubuntu1.5']
    templates = {args['dest']: args for args in module_arguments('ansible.builtin.template')}
    assert templates == {
        '/opt/gh-operations/bin/launch.sh': {'src': 'operations-launcher.sh.j2',
            'dest': '/opt/gh-operations/bin/launch.sh', 'owner': 'root', 'group': 'root', 'mode': '0755'},
        '/etc/systemd/system/gh-operations.service': {'src': 'gh-operations.service.j2',
            'dest': '/etc/systemd/system/gh-operations.service', 'owner': 'root', 'group': 'root', 'mode': '0644'},
    }
    users = module_arguments('ansible.builtin.user')
    assert {user['name'] for user in users} == {'gh-operations', 'gh-runner-issuer', 'gh-operations-poller'}
    assert all(user['group'] == user['name'] and user['groups'] == [] and user['append'] is False
               and user['shell'] == '/usr/sbin/nologin' for user in users)
    directories = {args['path']: args for args in module_arguments('ansible.builtin.file')}
    for path in ('/opt/gh-operations', '/opt/gh-operations/runner', '/opt/gh-operations/bin',
                 '/opt/gh-operations/cache', '/var/lib/gh-operations', '/var/lib/gh-operations/slot'):
        assert directories[path] == {'path': path, 'state': 'directory', 'owner': 'root', 'group': 'root', 'mode': '0755'}
    for name, user in [('issuer', 'gh-runner-issuer'), ('poller', 'gh-operations-poller')]:
        path = '/var/lib/gh-operations/' + name
        assert directories[path] == {'path': path, 'state': 'directory', 'owner': user, 'group': user, 'mode': '0700'}
    assert module_arguments('ansible.builtin.systemd') == [{
        'name': 'gh-operations.service', 'enabled': False, 'state': 'stopped'}]
    assert module_arguments('ansible.builtin.command') == []
    assert module_arguments('ansible.builtin.shell') == []
    assert 'ansible.builtin.apt' in output and '1:2.43.0-1ubuntu7.5' in output
    assert 'root' in output and '0755' in output and '0700' in output
    assert 'gh-runner-issuer' in output and 'gh-operations-poller' in output
    assert '"enabled": false' in output and '"state": "stopped"' in output
    assert 'Grant gh-runner passwordless sudo' not in output
    assert 'Add gh-runner user to docker group' not in output
    assert 'ansible.builtin.unarchive' in output


def test_real_selector_routes_default_ci_without_entering_operations(tmp_path):
    # Probe only the changed routing seam; the complete CI body and its old
    # templates are independently hash-pinned and retain their existing tests.
    code, output = run_role(tmp_path, {
        'github_runner_profile': DEFAULTS['github_runner_profile'],
        'github_runner_slots': 5,
        'github_runner_app_id': '12345',
        'github_runner_app_private_key': 'synthetic-not-a-key',
        'github_runner_app_installation_id': '67890',
    },
        hostname='ci-host', groups=['runners'], ci_probe=True)
    assert code == 0, output
    assert 'PROFILE_CI_SELECTED' in output
    assert 'Assert operations profile is isolated' not in output


@pytest.mark.parametrize('hostname,groups,expected', [
    ('ops', ['operations'], "inventory_hostname != 'ops'"),
    ('ops', ['runners'], "inventory_hostname != 'ops'"),
    ('ci-host', ['operations'], "'operations' not in group_names"),
    ('ci-host', ['operations', 'runners'], "'operations' not in group_names"),
])
@pytest.mark.parametrize('with_ci_credentials', [False, True])
def test_selector_rejects_ci_on_operations_target(tmp_path, hostname, groups, expected, with_ci_credentials):
    changes = {'github_runner_profile': 'ci', 'github_runner_slots': 5}
    if with_ci_credentials:
        changes.update({
            'github_runner_app_id': '12345',
            'github_runner_app_private_key': 'synthetic-not-a-key',
            'github_runner_app_installation_id': '67890',
        })
    code, output = run_role(tmp_path, changes, hostname=hostname, groups=groups, ci_probe=True)
    assert code != 0, output
    assert expected in output, output
    assert 'PROFILE_CI_SELECTED' not in output, output
    assert 'mock_module' not in output, output


@pytest.mark.parametrize('package', ['nodejs', 'docker.io', 'unapproved-tool'])
def test_package_manifest_rejects_unapproved_additions_before_mutation(tmp_path, package):
    packages = values()['github_runner_operations_packages'] + [package]
    code, output = run_role(tmp_path, {'github_runner_operations_packages': packages})
    assert code != 0, output
    assert 'reject' in output and 'github_runner_operations_packages' in output
    assert 'mock_module' not in output


def test_manifest_lists_every_default_package_with_its_installed_version(tmp_path):
    names = DEFAULTS['github_runner_operations_packages']
    facts = {'services': {}, 'packages': {name: [{'name': name, 'version': f'9.{i}-0ubuntu1'} for _ in [0]]
                                          for i, name in enumerate(names)}}
    del facts['packages'][names[-1]]  # one name not installed: it must show up as not-installed, not vanish
    code, output = run_role(tmp_path, {'github_runner_operations_packages': names, 'ansible_facts': facts})
    assert code == 0, output
    decoder = json.JSONDecoder()
    contents = []
    for start in re.finditer(r'=> (\{)', output):
        data, _ = decoder.raw_decode(output[start.start(1):])
        msg = data.get('msg')
        if isinstance(msg, dict) and msg.get('mock_module') == 'ansible.builtin.copy' \
                and msg['arguments']['dest'].endswith('package-manifest.txt'):
            contents.append(msg['arguments']['content'])
    assert len(contents) == 1
    lines = contents[0].splitlines()[1:]
    assert [line.split(' ')[0] for line in lines] == sorted(names)
    assert f'{names[-1]} not-installed' in lines
    assert all(line.split(' ')[1] for line in lines)
    versions = dict(line.split(' ', 1) for line in lines)
    assert versions[names[0]] == '9.0-0ubuntu1'


def test_no_task_of_the_operations_profile_writes_an_apt_source():
    # Not only the repository modules: any task naming an apt source location (a lineinfile/copy/template into
    # sources.list.d, trusted.gpg.d, keyrings) is refused. The one apt file the role writes is the preference below.
    for task in OPS:
        blob = json.dumps(task)
        for forbidden in ('sources.list', 'trusted.gpg', 'apt/keyrings', 'apt/sources'):
            assert forbidden not in blob, (task.get('name'), forbidden)
    writers = [task['name'] for task in OPS if '/etc/apt/' in json.dumps(task)]
    assert writers == ['Deny third-party apt origins for the operations runtime package names']


def test_packages_are_unpinned_and_the_manifest_is_recorded_only_outside_check_mode():
    tasks = {task.get('name'): task for task in OPS}
    install = tasks['Install operations runtime packages from the configured Ubuntu archive']
    assert install['ansible.builtin.apt']['state'] == 'present'
    assert install['ansible.builtin.apt']['name'] == '{{ github_runner_operations_packages }}'
    record = tasks['Record the operations runtime package manifest']
    assert record['when'] == 'not ansible_check_mode'
    # No task of the operations profile adds a repository or a key: archive packages only.
    modules = {key for task in OPS for key in task if key.startswith('ansible.builtin.')}
    assert not modules & {'ansible.builtin.apt_repository', 'ansible.builtin.apt_key', 'ansible.builtin.deb822_repository'}
    assert 'github_runner_operations_package_pins' not in (ROLE / 'tasks/operations.yml').read_text(encoding='utf-8')


def test_running_slot_is_rejected_before_any_provisioning_module(tmp_path):
    code, output = run_role(tmp_path, {'ansible_facts': {'services': {
        'gh-operations.service': {'state': 'running'}}}})
    assert code != 0, output
    assert 'Drain and verify the operations slot' in output
    assert 'Ensure separate operations service groups exist' not in output
    assert 'ansible.builtin.apt' not in output


def test_activation_drain_approval_stops_a_running_slot_and_allows_provisioning(tmp_path):
    """Explicit drain approval stops a running service and refreshes its fixture state before provisioning."""
    facts = values()['ansible_facts']
    facts['services'] = {'gh-operations.service': {'state': 'running'}}
    code, output = run_role(tmp_path, {
        'github_runner_operations_drain_approved': True,
        'ansible_facts': facts,
    })
    assert code == 0, output
    modules = mocked_modules(output)
    systemd = [message['arguments'] for message in modules
               if message['mock_module'] == 'ansible.builtin.systemd']
    assert systemd[0] == {'name': 'gh-operations.service', 'enabled': False, 'state': 'stopped'}
    assert 'Create protected operations directories' in output
    assert output.index('Stop the operations runner with explicit drain approval') \
        < output.index('Create protected operations directories')
    assert output.index('Stop the operations runner with explicit drain approval') \
        < output.index('Refresh operations service state after approved drain') \
        < output.index('Refuse changes to an active operations slot')


def test_existing_ci_uid_cannot_be_reused_by_operations(tmp_path):
    passwd = values()['getent_passwd']
    passwd['gh-runner'] = list(passwd['gh-operations'])
    code, output = run_role(tmp_path, {'getent_passwd': passwd})
    assert code != 0, output
    assert 'distinct non-root UIDs unrelated to CI' in output
    assert 'Ensure separate operations service groups exist' not in output


@pytest.mark.parametrize('kind', ['root', 'shared', 'other_uid', 'docker', 'sudo', 'wheel', 'primary'])
def test_service_identity_rejects_privileged_or_shared_accounts(tmp_path, kind):
    passwd = values()['getent_passwd']
    groups = {}
    if kind == 'root':
        passwd['gh-operations'][1] = '0'
    elif kind == 'shared':
        passwd['gh-runner-issuer'] = list(passwd['gh-operations'])
    elif kind == 'other_uid':
        passwd['unrelated-account'] = list(passwd['gh-operations'])
    elif kind == 'primary':
        groups['docker'] = ['x', '777', '']
        passwd['gh-operations'][2] = '777'
    else:
        account = {'docker': 'gh-operations', 'sudo': 'gh-runner-issuer', 'wheel': 'gh-operations-poller'}[kind]
        groups[kind] = ['x', '777', account]
    code, output = run_role(tmp_path, {'getent_passwd': passwd, 'getent_group': groups})
    assert code != 0, output
    assert 'Ensure separate operations service groups exist' not in output
    assert 'distinct non-root UIDs unrelated to CI' in output or 'Never repurpose' in output


@pytest.mark.parametrize('kind', ['root', 'privileged_alias', 'service_alias'])
def test_service_groups_cannot_introduce_privilege_before_account_creation(tmp_path, kind):
    groups = {'gh-operations': ['x', '777', '']}
    if kind == 'root':
        groups['gh-operations'][1] = '0'
    elif kind == 'privileged_alias':
        groups['docker'] = ['x', '777', '']
    else:
        groups['gh-runner-issuer'] = ['x', '777', '']
    code, output = run_role(tmp_path, {'getent_passwd': {}, 'getent_group': groups})
    assert code != 0, output
    assert 'distinct non-root GIDs' in output
    assert 'Ensure separate operations service groups exist' not in output


def parse_protected_unit(unit):
    # Only the two intended service Environment entries may repeat. Reject
    # every other duplicate so list accumulation cannot hide a permissive
    # earlier directive behind the value ConfigParser would retain last.
    environment, unique_lines, section = [], [], None
    for line in unit.splitlines():
        stripped = line.strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            section = stripped
        if re.match(r'Environment\s*=', stripped):
            assert section == '[Service]'
            environment.append(stripped.split('=', 1)[1].strip())
        else:
            unique_lines.append(line)
    assert len(environment) == 2
    assert set(environment) == {'HOME=/var/lib/gh-operations/slot/home', 'TMPDIR=/var/lib/gh-operations/slot/temp'}
    parsed = configparser.ConfigParser(interpolation=None, strict=True)
    parsed.optionxform = str
    parsed.read_string('\n'.join(unique_lines))
    return parsed


@pytest.mark.parametrize('directive,value', [('ProtectSystem', 'full'), ('RestrictAddressFamilies', 'AF_PACKET')])
def test_duplicate_security_directives_cannot_hide_permissive_values(directive, value):
    unit = render('gh-operations.service.j2')
    unit = unit.replace(directive + '=', directive + '=' + value + '\n' + directive + '=', 1)
    with pytest.raises(configparser.DuplicateOptionError):
        parse_protected_unit(unit)


def test_the_slot_path_is_only_a_tmpfs_and_never_also_bound_or_made_writable():
    """Pin the slot to one tmpfs: the 2026-11-07 finding showed a same-path write rule binds disk instead, blocking launcher directory creation."""
    parsed = parse_protected_unit(render('gh-operations.service.j2'))
    service = parsed['Service']
    slot = '/var/lib/gh-operations/slot'

    assert service['TemporaryFileSystem'].split()[0].split(':', 1)[0] == slot
    for directive in ('ReadWritePaths', 'BindPaths', 'InaccessiblePaths'):
        assert directive not in service
    assert slot not in service.get('ReadOnlyPaths', '').split()
    bind_read_only_targets = [mapping.rsplit(':', 1)[-1]
                              for mapping in service.get('BindReadOnlyPaths', '').split()]
    assert slot not in bind_read_only_targets


def test_service_caps_state_and_mounts_root_owned_executables_read_only():
    unit = render('gh-operations.service.j2')
    parsed = parse_protected_unit(unit)
    service = parsed['Service']
    assert service['User'] == 'gh-operations' and service['Group'] == 'gh-operations'
    assert service['ExecStart'] == '/opt/gh-operations/bin/launch.sh'
    assert service['StandardOutput'] == service['StandardError'] == 'null'
    assert service['BindReadOnlyPaths'] == '/opt/gh-operations/runner/bin:/var/lib/gh-operations/slot/bin /opt/gh-operations/runner/externals:/var/lib/gh-operations/slot/externals'
    assert service['TemporaryFileSystem'] == '/var/lib/gh-operations/slot:rw,nosuid,nodev,size=268435456,nr_inodes=32768,mode=0700,uid=1234,gid=1234 /tmp:rw,nosuid,nodev,size=16777216,nr_inodes=4096,mode=1777 /var/tmp:rw,nosuid,nodev,size=16777216,nr_inodes=4096,mode=1777 /dev/shm:rw,nosuid,nodev,noexec,size=16777216,nr_inodes=4096,mode=1777'
    assert service['PrivateDevices'] == service['MemoryAccounting'] == 'true'
    assert service.getint('MemoryMax') == 1073741824 and service.getint('MemorySwapMax') == 0
    assert service['CapabilityBoundingSet'] == '' and service['AmbientCapabilities'] == ''
    assert service['NoNewPrivileges'] == 'true'
    assert service['ProtectSystem'] == 'strict'
    for name in ('ProtectHome', 'ProtectKernelTunables', 'ProtectKernelModules',
                 'ProtectControlGroups', 'RestrictSUIDSGID', 'LockPersonality'):
        assert service[name] == 'true'
    assert set(service['RestrictAddressFamilies'].split()) == {'AF_UNIX', 'AF_INET', 'AF_INET6'}
    assert service['KillMode'] == 'control-group'
    assert service['ReadOnlyPaths'] == '/opt/gh-operations'
    assert parsed['Unit'].getint('StartLimitIntervalSec') == 0
    assert service['Restart'] == 'always' and 30 <= service.getint('RestartSec') <= 60
    assert 0 < service.getint('TimeoutStopSec') <= 120
    assert service.getint('RuntimeMaxSec') == 3600
    assert 'ReadWritePaths' not in service
    assert 'docker.service' not in ' '.join(parsed['Unit'].get(name, '') for name in ('After', 'Wants', 'Requires')).split()
    assert service.get('PrivateTmp', 'false') == 'false'
    launcher = render('operations-launcher.sh.j2')
    assert 'cp -a' not in launcher and 'chmod -R' not in launcher
    assert 'config.sh' not in launcher and 'run --jitconfig' not in launcher
    assert 'exec "$STATE/bin/Runner.Listener" run' in launcher


@pytest.mark.parametrize('corruption', [None, 'real_string_flags', 'string_false', 'string_yes', 'empty', 'path',
    'non_ephemeral', 'update', 'work', 'repo',
    'bad_owner', 'bad_group', 'bad_mode', 'bad_hash', 'bad_parent_owner', 'bad_parent_mode',
    'docker', 'sudo', 'root_user', 'wrong_user'])
def test_launcher_real_shell_keeps_jit_off_argv_and_rejects_invalid_config(tmp_path, corruption):
    if not shutil.which('bash'):
        pytest.skip('Linux/bash controller is required')
    base, runtime, fakebin = (tmp_path / part for part in ('install', 'runtime', 'fakebin'))
    for directory in (base / 'runner', runtime / 'slot/bin', runtime / 'slot/externals', fakebin):
        directory.mkdir(parents=True)
    issuer = tmp_path / 'issuer'
    def encode(data):
        return base64.b64encode(json.dumps(data).encode()).decode()
    settings = {'agentName': 'ops-slot-1', 'ephemeral': True,
        'disableUpdate': True, 'workFolder': 'work'}
    if corruption == 'real_string_flags':
        # Measured on the live generate-jitconfig API (2026-11-07): every value of the file is a string.
        settings.update({'ephemeral': 'True', 'disableUpdate': 'True'})
    if corruption == 'string_false':
        settings.update({'ephemeral': 'False', 'disableUpdate': 'True'})
    if corruption == 'string_yes':
        settings.update({'ephemeral': 'True', 'disableUpdate': 'yes'})
    if corruption == 'non_ephemeral':
        settings['ephemeral'] = False
    if corruption == 'update':
        settings['disableUpdate'] = False
    if corruption == 'work':
        settings['workFolder'] = '../escape'
    if corruption == 'repo':
        settings['gitHubUrl'] = 'https://github.com/bigler-webapps/other'
    config = {'.runner': encode(settings), '.credentials': encode({'synthetic': True})}
    if corruption == 'path':
        config['../escape'] = encode('bad')
    payload = '' if corruption == 'empty' else encode(config)
    issuer.write_text('#!/bin/sh\n[ "$1" = --repository ] && [ "$2" = bigler-webapps-ops/webapp-operations ] && [ "$3" = --runner-name ] && [ "$4" = ops-slot-1 ] && [ "$5" = --labels ] && [ "$6" = self-hosted,ops-deploy ] || exit 20\nprintf ' + payload + '\n', encoding='utf-8')
    issuer.chmod(0o750)
    listener = runtime / 'slot/bin/Runner.Listener'
    listener.write_text('#!/bin/sh\n[ "$#" = 1 ] && [ "$1" = run ] || exit 31\n[ -d work ] && [ -d temp ] && [ -d home ] && [ -f .runner ] && [ -f .credentials ] || exit 32\n[ "$HOME" = "$PWD/home" ] && [ "$TMPDIR" = "$PWD/temp" ] || exit 33\n', encoding='utf-8')
    listener.chmod(0o555)
    for directory in (base / 'runner', runtime / 'slot/bin', runtime / 'slot/externals'):
        directory.chmod(0o555)
    mocks = {
        'id': '#!/bin/sh\ncase "$1" in -u) echo 1234;; -un) echo gh-operations;; -nG) echo gh-operations;; esac\n',
        'stat': '#!/bin/sh\ncase "$2" in %u:%a) echo 0:755;; *) echo root:gh-operations:750;; esac\n',
        'sudo': '#!/bin/sh\nexit 1\n',
        'sha256sum': '#!/bin/sh\necho ' + 'a' * 64 + '\n',
    }
    decoder_probe = tmp_path / 'decoder-argv.json'
    mocks['python3'] = ('#!/usr/bin/python3\nimport json, os, sys\nfrom pathlib import Path\n'
        'arguments = sys.argv[1:]\n'
        f'payload = {payload!r}\n'
        'if len(arguments) != 2 or arguments[0] != "-c" or (payload and any(payload in arg for arg in arguments)):\n'
        '    sys.exit(91)\n'
        f'Path({str(decoder_probe)!r}).write_text(json.dumps(arguments), encoding="utf-8")\n'
        'os.execv("/usr/bin/python3", ["/usr/bin/python3", *arguments])\n')
    if corruption == 'bad_owner':
        mocks['stat'] = mocks['stat'].replace('root:gh-operations:750', 'gh-operations:gh-operations:750')
    if corruption == 'bad_group':
        mocks['stat'] = mocks['stat'].replace('root:gh-operations:750', 'root:other:750')
    if corruption == 'bad_mode':
        mocks['stat'] = mocks['stat'].replace('root:gh-operations:750', 'root:gh-operations:777')
    if corruption == 'bad_parent_owner':
        mocks['stat'] = mocks['stat'].replace('0:755', '1234:755')
    if corruption == 'bad_parent_mode':
        mocks['stat'] = mocks['stat'].replace('0:755', '0:775')
    if corruption == 'bad_hash':
        mocks['sha256sum'] = '#!/bin/sh\necho ' + 'b' * 64 + '\n'
    if corruption == 'docker':
        mocks['id'] = mocks['id'].replace('-nG) echo gh-operations', '-nG) echo gh-operations docker')
    if corruption == 'sudo':
        mocks['sudo'] = '#!/bin/sh\nexit 0\n'
    if corruption == 'root_user':
        mocks['id'] = mocks['id'].replace('-u) echo 1234', '-u) echo 0')
    if corruption == 'wrong_user':
        mocks['id'] = mocks['id'].replace('-un) echo gh-operations', '-un) echo other')
    for name, script in mocks.items():
        path = fakebin / name
        path.write_text(script, encoding='utf-8')
        path.chmod(0o700)
    launcher = tmp_path / 'launch.sh'
    rendered = render('operations-launcher.sh.j2', {
        'github_runner_operations_base_dir': str(base),
        'github_runner_operations_runtime_dir': str(runtime),
        'github_runner_operations_issuer_client': str(issuer),
        'github_runner_operations_issuer_client_sha256': 'a' * 64,
    })
    # Interpose only the fixed decoder executable; preserve its actual code
    # and stdin, then exec the real Python. The listener and issuer already
    # assert their own non-secret fixed argv separately.
    assert rendered.count('/usr/bin/python3') == 1
    launcher.write_text(rendered.replace('/usr/bin/python3', str(fakebin / 'python3')), encoding='utf-8')
    env = {**os.environ, 'PATH': str(fakebin) + os.pathsep + os.environ['PATH']}
    result = subprocess.run(['bash', str(launcher)], env=env, text=True, capture_output=True, timeout=10)
    if corruption and corruption != 'real_string_flags':
        assert result.returncode != 0
        assert not (runtime / 'slot/.credentials').exists()
        assert not (runtime / 'escape').exists()
        assert payload == '' or payload not in result.stdout + result.stderr
        return
    assert result.returncode == 0, result.stderr
    decoder_arguments = json.loads(decoder_probe.read_text(encoding='utf-8'))
    assert len(decoder_arguments) == 2 and decoder_arguments[0] == '-c'
    assert all(payload not in argument for argument in decoder_arguments)
    assert (runtime / 'slot/.runner').exists()
    assert payload not in result.stdout + result.stderr
    assert (runtime / 'slot/.credentials').stat().st_mode & 0o777 == 0o600
    issuer.unlink()  # Own synthetic fixture only.
    result = subprocess.run(['bash', str(launcher)], env=env, text=True, capture_output=True, timeout=10)
    assert result.returncode != 0 and 'unavailable' in result.stderr


def test_check_mode_on_a_host_without_the_service_identities_ends_the_profile_instead_of_failing(tmp_path):
    """WM-INF-108: the getent read cannot find an account that a check run only simulates."""
    task = next(t for t in OPS if t.get('name', '').startswith('Read operations numeric service identity'))
    assert task['ansible.builtin.getent']['fail_key'] == '{{ not ansible_check_mode }}'
    code, output = run_role(tmp_path, {'getent_passwd': {}}, check=True)
    assert code == 0, output
    assert 'Check mode: the operations service identities do not exist' in output
    assert 'Create protected operations directories' not in output
    assert 'Install operations runtime packages' not in output
    # The account exists (a later check run): the profile is checked to the end.
    code, output = run_role(tmp_path / 'present', {}, check=True)
    assert code == 0, output
    assert 'Check mode: the operations service identities do not exist' not in output
    assert 'Create protected operations directories' in output
    # A real run is never cut short: a missing account stays a failure of the getent read itself.
    code, output = run_role(tmp_path / 'real', {'getent_passwd': {}})
    assert 'Check mode: the operations service identities do not exist' not in output


def test_the_operations_repository_is_named_with_its_new_organization_everywhere_in_the_role():
    """WM-SEC-23: webapp-operations lives in bigler-webapps-ops; a redirect from the old owner must not hide a stale name."""
    old = re.compile(r"bigler-webapps/(webapp-operations|webapps)\b", re.IGNORECASE)
    offenders = [str(path.relative_to(ROLE)) for path in ROLE.rglob("*")
                 if path.is_file() and "__pycache__" not in path.parts and old.search(path.read_text(encoding="utf-8", errors="replace"))]
    assert offenders == []
    assert DEFAULTS["github_runner_operations_repository"] == "bigler-webapps-ops/webapp-operations"
