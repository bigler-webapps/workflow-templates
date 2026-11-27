"""WFT-INF-1: Ubuntu sources content and task boundaries."""
from pathlib import Path
import re

from jinja2 import Environment, FileSystemLoader
import yaml

ROLE = Path(__file__).resolve().parents[1]
DEFAULTS = yaml.safe_load((ROLE / 'defaults/main.yml').read_text(encoding='utf-8'))
TASKS = yaml.safe_load((ROLE / 'tasks/main.yml').read_text(encoding='utf-8'))[0]['block']
SOURCES = next(task for task in TASKS if task['name'] == 'Manage Ubuntu apt sources')
TEMPLATE = next(task for task in SOURCES['block'] if task['name'] == 'Configure Ubuntu deb822 sources')
CLEANUP = next(task for task in SOURCES['block'] if task['name'] == 'Remove duplicate Ubuntu archive lines from the legacy sources list')
EXPECTED = '''Types: deb
URIs: http://archive.ubuntu.com/ubuntu/
Suites: noble noble-updates noble-backports
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg

Types: deb
URIs: http://security.ubuntu.com/ubuntu/
Suites: noble-security
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
'''


def render(overrides=None, facts=None):
    env = Environment(loader=FileSystemLoader(ROLE / 'templates'), keep_trailing_newline=True)
    values = dict(DEFAULTS)
    values.update(overrides or {})
    values['ansible_facts'] = facts or {}
    # Resolve the role defaults as Ansible does, in dependency order.
    for key in ('base_ubuntu_release', 'base_ubuntu_archive_suites', 'base_ubuntu_security_suites'):
        value = values[key]
        if isinstance(value, list):
            values[key] = [env.from_string(item).render(**values) for item in value]
        else:
            values[key] = env.from_string(value).render(**values)
    return env.get_template(TEMPLATE['ansible.builtin.template']['src']).render(**values)


def test_rendered_content_exactly_matches_expected():
    assert render() == EXPECTED
    assert render(facts={'distribution_release': 'noble'}) == EXPECTED
    assert 'at.archive.ubuntu.com' not in render()


def test_render_respects_release_and_overrides():
    result = render({
        'base_ubuntu_release': 'jammy',
        'base_ubuntu_archive_uri': 'https://mirror.example/ubuntu',
        'base_ubuntu_security_uri': 'https://security.example/ubuntu',
        'base_ubuntu_components': ['main', 'universe'],
        'base_ubuntu_keyring': '/example/keyring.gpg',
    })
    assert 'URIs: https://mirror.example/ubuntu\nSuites: jammy jammy-updates jammy-backports' in result
    assert 'URIs: https://security.example/ubuntu\nSuites: jammy-security' in result
    assert result.count('Components: main universe') == 2
    assert result.count('Signed-By: /example/keyring.gpg') == 2
    assert 'noble' not in result
    assert 'Suites: focal focal-updates focal-backports' in render(facts={'distribution_release': 'focal'})
    assert 'Suites: custom' in render({'base_ubuntu_archive_suites': ['custom']})


def test_country_mirror_replaced_and_matching_file_unchanged():
    """Ansible itself is not executed in these tests."""
    existing = EXPECTED.replace('http://archive.', 'http://at.archive.')
    desired = render()
    assert existing != desired
    assert 'at.archive.ubuntu.com' not in desired
    assert TEMPLATE['ansible.builtin.template']['dest'] == '/etc/apt/sources.list.d/ubuntu.sources'


def test_country_mirror_replaced_and_render_idempotent():
    """The template module reports a change only when the content differs, so identical renders mean no change."""
    assert render().encode() == render().encode()
    assert render() == EXPECTED
    assert list(TEMPLATE) == ['name', 'ansible.builtin.template']


def test_legacy_duplicate_removed_comments_and_third_party_preserved():
    options = CLEANUP['ansible.builtin.lineinfile']
    assert options['path'] == '/etc/apt/sources.list'
    assert options['state'] == 'absent'
    pattern = re.compile(options['regexp'])
    lines = [
        '# Ubuntu sources\n',
        'deb http://at.archive.ubuntu.com/ubuntu noble main\n',
        'deb http://archive.ubuntu.com/ubuntu noble main\n',
        'deb [arch=amd64] https://security.ubuntu.com/ubuntu/ noble-security main\n',
        'deb-src http://archive.ubuntu.com/ubuntu noble main\n',
        'deb https://download.docker.com/linux/ubuntu noble stable\n',
        'deb https://pkgs.tailscale.com/stable/ubuntu noble main\n',
    ]
    kept = [line for line in lines if not pattern.search(line)]
    assert kept == [lines[0], *lines[4:]]
    assert not pattern.search('deb https://archive.ubuntu.com.example.org/ubuntu noble main')
    assert len(SOURCES['block']) == 2


def test_sources_are_switchable_and_before_apt_cache_refresh():
    assert DEFAULTS['base_ubuntu_sources_managed'] is True
    assert SOURCES['when'] == [
        'base_ubuntu_sources_managed | bool',
        "ansible_facts.distribution | default('Ubuntu') == 'Ubuntu'",
    ]
    upgrade = next(task for task in TASKS if task['name'] == 'Apt update and upgrade, excluding transport-critical packages')
    assert TASKS.index(SOURCES) < TASKS.index(upgrade)
    assert any(task.get('ansible.builtin.apt', {}).get('update_cache') is True for task in upgrade['block'])
