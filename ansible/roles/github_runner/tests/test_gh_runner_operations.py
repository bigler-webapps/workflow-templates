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


def values(**overrides):
    result = {**DEFAULTS,
        'github_runner_profile': 'operations', 'github_runner_slots': 1,
        'github_runner_operations_package_manifest_verified': True,
        'github_runner_operations_package_pins': {
            name: f'{name}=1.0-1' for name in
            ('git', 'openssh-client', 'rsync', 'python3', 'curl', 'jq')},
        'getent_group': {},
        'getent_passwd': {'gh-operations': ['x', '1234', '1234', '', '/unused', '/usr/sbin/nologin']},
        'ansible_facts': {'services': {}},
    }
    result.update(overrides)
    return result


def render(name, overrides=None):
    return Environment(undefined=StrictUndefined, keep_trailing_newline=True).from_string(
        (ROLE / 'templates' / name).read_text(encoding='utf-8')).render(
            values(**(overrides or {})))


def test_unchanged_jit_template_remains_exact_baseline():
    expected = {
        'templates/jit-config.sh.j2': '95658badd360333c594a402a65338cccfb9b57b7456e28cafa943ef6fc066b65',
    }
    for name, digest in expected.items():
        data = (ROLE / name).read_bytes().replace(b'\r\n', b'\n')
        # Template fixtures were measured on CRLF Windows checkouts.
        if name.startswith('templates/'):
            data = data.replace(b'\n', b'\r\n')
        assert hashlib.sha256(data).hexdigest() == digest, name
    assert DEFAULTS['github_runner_profile'] == 'ci'
    assert DEFAULTS['github_runner_operations_active'] is False
    assert DEFAULTS['github_runner_operations_package_pins'] == {}
    assert DEFAULTS['github_runner_operations_package_manifest_verified'] is False


def instrument(tasks):
    """Keep real assertions/includes/loops/when; replace all host modules with debug.

    No user/file/apt/service/network module can run in this fixture. The replaced
    module arguments still pass through real Ansible templating and are retained
    in stdout for ownership/package/service assertions.
    """
    result = []
    for source in tasks:
        task = dict(source)
        key = next(k for k in task if k.startswith('ansible.builtin.'))
        if key in ('ansible.builtin.assert', 'ansible.builtin.include_tasks'):
            result.append(task)
            continue
        args = task.pop(key)
        task.pop('become', None)
        task.pop('notify', None)
        task['ansible.builtin.debug'] = {'msg': {'mock_module': key, 'arguments': args}}
        if key == 'ansible.builtin.get_url':
            task['changed_when'] = True
        result.append(task)
    return result


def run_role(tmp_path, overrides, *, hostname='ops', groups=None, ci_probe=False):
    fixture = tmp_path / 'fixture'
    tasks = fixture / 'tasks'
    tasks.mkdir(parents=True, exist_ok=True)
    for name, source in [('main.yml', MAIN), ('operations.yml', OPS),
                         ('ci.yml', ([{'ansible.builtin.debug': {'msg': 'PROFILE_CI_SELECTED'}}] if ci_probe else yaml.safe_load((ROLE / 'tasks/ci.yml').read_text(encoding='utf-8'))))]:
        (tasks / name).write_text(yaml.safe_dump(instrument(source), sort_keys=False), encoding='utf-8')
    inventory = {'all': {'children': {group: {'hosts': {hostname: {'ansible_connection': 'local'}}}
                                     for group in (groups or ['operations'])}}}
    (fixture / 'hosts.yml').write_text(yaml.safe_dump(inventory), encoding='utf-8')
    play = [{'hosts': 'all', 'gather_facts': False, 'vars': values(**overrides),
             'tasks': [{'ansible.builtin.include_tasks': str(tasks / 'main.yml')}]}]
    (fixture / 'play.yml').write_text(yaml.safe_dump(play), encoding='utf-8')
    config = fixture / 'ansible.cfg'
    config.write_text('[defaults]\nhost_key_checking=True\nretry_files_enabled=False\n', encoding='utf-8')
    proc = subprocess.run([sys.executable, '-I', '-m', 'ansible.cli.playbook', '-i', str(fixture / 'hosts.yml'),
                           str(fixture / 'play.yml')], cwd=fixture,
                          env={**os.environ, 'ANSIBLE_CONFIG': str(config), 'ANSIBLE_NOCOLOR': '1'},
                          text=True, capture_output=True, timeout=60)
    return proc.returncode, proc.stdout + proc.stderr


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
    ({'github_runner_operations_package_pins': {}}, 'ops', ['operations']),
    ({'github_runner_operations_package_manifest_verified': False}, 'ops', ['operations']),
    ({'github_runner_operations_package_pins': {'curl': 'curl=latest'}}, 'ops', ['operations']),
    ({'github_runner_operations_active': True}, 'ops', ['operations']),
    ({'github_runner_operations_active': True, 'github_runner_operations_activation_authorized': True,
      'github_runner_operations_containment_verified': True, 'github_runner_operations_issuer_client_verified': True,
      'github_runner_operations_issuer_client_version': '1.0.0',
      'github_runner_operations_issuer_client_sha256': 'a' * 64}, 'ops', ['operations']),
    ({}, 'ci-host', ['operations']),
    ({}, 'ops', ['operations', 'runners']),
])
def test_real_ansible_rejects_invalid_profile_before_any_host_module(tmp_path, changes, hostname, groups):
    code, output = run_role(tmp_path, changes, hostname=hostname, groups=groups)
    assert code != 0, output
    assert 'mock_module' not in output, output
    failed = re.search(r'"assertion": ("(?:\\.|[^"\\])*")', output)
    assert failed, output
    expected = next(iter(changes)) if changes else ('inventory_hostname' if hostname != 'ops' else 'group_names')
    assert expected in json.loads(failed.group(1)), output


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
        'url': 'https://github.com/actions/runner/releases/download/v2.338.0/actions-runner-linux-x64-2.338.0.tar.gz',
        'dest': '/opt/gh-operations/cache/runner.tar.gz',
        'checksum': 'sha256:af4b794c1bc41d73d40535e3fe092a39f9679cd8d965954c2aca25a05ca41d32',
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
    assert len(apt[0]['name']) == 6
    assert set(apt[0]['name']) == {name + '=1.0-1' for name in
        ('git', 'openssh-client', 'rsync', 'python3', 'curl', 'jq')}
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
    assert 'ansible.builtin.apt' in output and 'git=1.0-1' in output
    assert 'root' in output and '0755' in output and '0700' in output
    assert 'gh-runner-issuer' in output and 'gh-operations-poller' in output
    assert '"enabled": false' in output and '"state": "stopped"' in output
    assert 'Grant gh-runner passwordless sudo' not in output
    assert 'Add gh-runner user to docker group' not in output
    assert 'ansible.builtin.unarchive' in output


def test_real_selector_routes_default_ci_without_entering_operations(tmp_path):
    # Probe only the changed routing seam; the complete CI body and its old
    # templates are independently hash-pinned and retain their existing tests.
    code, output = run_role(tmp_path, {'github_runner_profile': DEFAULTS['github_runner_profile']},
        hostname='ci-host', groups=['runners'], ci_probe=True)
    assert code == 0, output
    assert 'PROFILE_CI_SELECTED' in output
    assert 'Assert operations profile is isolated' not in output


@pytest.mark.parametrize('package', ['nodejs', 'docker.io', 'unapproved-tool'])
def test_package_manifest_rejects_unapproved_additions_before_mutation(tmp_path, package):
    pins = values()['github_runner_operations_package_pins']
    pins[package] = package + '=1.0-1'
    code, output = run_role(tmp_path, {'github_runner_operations_package_pins': pins})
    assert code != 0, output
    assert 'reject' in output and 'github_runner_operations_package_pins.keys()' in output
    assert 'mock_module' not in output


def test_running_slot_is_rejected_before_any_provisioning_module(tmp_path):
    code, output = run_role(tmp_path, {'ansible_facts': {'services': {
        'gh-operations.service': {'state': 'running'}}}})
    assert code != 0, output
    assert 'Drain and verify the operations slot' in output
    assert 'Ensure separate operations service groups exist' not in output
    assert 'ansible.builtin.apt' not in output


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


@pytest.mark.parametrize('directive,value', [('ReadWritePaths', '/'), ('RestrictAddressFamilies', 'AF_PACKET')])
def test_duplicate_security_directives_cannot_hide_permissive_values(directive, value):
    unit = render('gh-operations.service.j2')
    unit = unit.replace(directive + '=', directive + '=' + value + '\n' + directive + '=', 1)
    with pytest.raises(configparser.DuplicateOptionError):
        parse_protected_unit(unit)


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
    assert service['ReadWritePaths'] == '/var/lib/gh-operations/slot'
    assert 'docker.service' not in ' '.join(parsed['Unit'].get(name, '') for name in ('After', 'Wants', 'Requires')).split()
    assert service.get('PrivateTmp', 'false') == 'false'
    launcher = render('operations-launcher.sh.j2')
    assert 'cp -a' not in launcher and 'chmod -R' not in launcher
    assert 'config.sh' not in launcher and 'run --jitconfig' not in launcher
    assert 'exec "$STATE/bin/Runner.Listener" run' in launcher


@pytest.mark.parametrize('corruption', [None, 'empty', 'path', 'non_ephemeral', 'update', 'work', 'repo',
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
    issuer.write_text('#!/bin/sh\n[ "$1" = --repository ] && [ "$2" = bigler-webapps/webapp-operations ] && [ "$3" = --runner-name ] && [ "$4" = ops-slot-1 ] || exit 20\nprintf ' + payload + '\n', encoding='utf-8')
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
    if corruption:
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
