import json
import sys
import os
import subprocess
import shlex
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".github/scripts"))

from routine_cli import MAX_MANIFEST_BYTES, PreflightError, check_cli, load_contract
from routine_cli import main


CONTRACT = ROOT / "ansible/roles/github_runner/files/routine-cli.json"
EXPECTED = {"schema_version": 1, "package": "@anthropic-ai/claude-code",
            "version": "2.1.308", "node_major": 22, "prefix": "/usr/local"}


def executable(tmp_path, source):
    path = tmp_path / "claude"
    path.write_text("#!/usr/bin/env python3\n" + source, encoding="utf-8")
    path.chmod(0o700)
    return str(path)


@pytest.mark.parametrize("tail", ["", "\n", "\r\n"])
def test_manifest_selects_exact_cli_and_accepts_one_terminal_newline(tmp_path, tail):
    contract = load_contract(CONTRACT)
    assert contract == EXPECTED
    cli = executable(tmp_path, f"import sys; sys.stdout.write({('2.1.308 (Claude Code)' + tail)!r})")
    check_cli(contract, cli)


@pytest.mark.parametrize("payload", [
    b'{"schema_version":1,"schema_version":1,"package":"@anthropic-ai/claude-code","version":"2.1.308","node_major":22,"prefix":"/usr/local"}',
    json.dumps({**EXPECTED,'schema_version':True}).encode(),
    json.dumps({**EXPECTED,'version':'latest'}).encode(),
    json.dumps({**EXPECTED,'node_major':20}).encode(),
    json.dumps({**EXPECTED,'extra':0}).encode(),
    json.dumps({**EXPECTED,'prefix':'/tmp/untrusted'}).encode(),
])
def test_manifest_rejects_duplicate_unknown_and_floating_or_incompatible_values(tmp_path, payload):
    manifest = tmp_path / "contract.json"
    manifest.write_bytes(payload)
    with pytest.raises(PreflightError, match="manifest_invalid"):
        load_contract(manifest)


def test_manifest_size_cap_and_utf8_are_enforced(tmp_path):
    manifest = tmp_path / "contract.json"
    manifest.write_bytes(b" " * (MAX_MANIFEST_BYTES + 1))
    with pytest.raises(PreflightError, match="manifest_invalid"):
        load_contract(manifest)
    manifest.write_bytes(b"\xff")
    with pytest.raises(PreflightError, match="manifest_invalid"):
        load_contract(manifest)


@pytest.mark.parametrize(("output", "stderr", "exit_code", "expected_code"), [
    ("2.1.3080 (Claude Code)", "", 0, "cli_version_mismatch"),
    ("v2.1.308 (Claude Code)", "", 0, "cli_version_mismatch"),
    ("2.1.308 (Claude Code) extra", "", 0, "cli_version_mismatch"),
    ("2.1.308 (Claude Code)\n\n", "", 0, "cli_version_mismatch"),
    ("2.1.308 (Claude Code)", "noise", 0, "cli_output_invalid"),
    ("2.1.308 (Claude Code)", "", 3, "cli_failed"),
])
def test_executable_output_is_exact_and_diagnostics_do_not_echo(tmp_path, output, stderr, exit_code, expected_code):
    marker = "HOSTILE_CLI_OUTPUT_91F7"
    cli = executable(tmp_path, "import sys\n"
                     f"sys.stdout.write({(output + marker)!r} if {marker in output!r} else {output!r})\n"
                     f"sys.stderr.write({stderr!r})\nraise SystemExit({exit_code})\n")
    with pytest.raises(PreflightError, match=expected_code) as raised:
        check_cli(EXPECTED, cli)
    assert marker not in str(raised.value)


def test_executable_timeout_output_cap_and_unavailable_are_fixed_failures(tmp_path):
    slow = executable(tmp_path, "import time; time.sleep(10)")
    with pytest.raises(PreflightError, match="cli_timeout"):
        check_cli(EXPECTED, slow)
    huge = executable(tmp_path, "import sys; sys.stdout.write('X' * 100000)")
    with pytest.raises(PreflightError, match="cli_output_invalid"):
        check_cli(EXPECTED, huge)
    with pytest.raises(PreflightError, match="cli_unavailable"):
        check_cli(EXPECTED, str(tmp_path / "missing"))


def test_action_uses_its_checkout_paths_and_exports_update_disable():
    action = (ROOT / ".github/actions/check-routine-cli/action.yml").read_text(encoding="utf-8")
    helper = ROOT / ".github/actions/check-routine-cli/../../scripts/routine_cli.py"
    manifest = ROOT / ".github/actions/check-routine-cli/../../../ansible/roles/github_runner/files/routine-cli.json"
    assert helper.resolve().is_file() and manifest.resolve().is_file()
    assert 'DISABLE_AUTOUPDATER=1' in action and "$GITHUB_ENV" in action
    assert "npm install" not in action and "curl " not in action


def test_manifest_nonregular_symlink_missing_and_deep_json_refuse(tmp_path):
    regular = tmp_path / 'regular'; regular.write_text(json.dumps(EXPECTED))
    link = tmp_path / 'link'; link.symlink_to(regular)
    fifo = tmp_path / 'fifo'; os.mkfifo(fifo)
    for path in (link, fifo, tmp_path, tmp_path / 'missing'):
        with pytest.raises(PreflightError):
            load_contract(path)
    deep = tmp_path / 'deep'; deep.write_text('[' * 1100 + '0' + ']' * 1100)
    with pytest.raises(PreflightError):
        load_contract(deep)


def test_child_has_private_context_and_no_inherited_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv('WM_RT30_TEST_CREDENTIAL', 'SYNTHETIC_SENTINEL')
    cli = executable(tmp_path, "import os,sys\n"
                     "assert 'WM_RT30_TEST_CREDENTIAL' not in os.environ\n"
                     "assert os.environ['DISABLE_AUTOUPDATER']=='1'\n"
                     "assert os.environ['HOME']==os.environ['CLAUDE_CONFIG_DIR']==os.getcwd()\n"
                     "assert os.listdir(os.getcwd())==[]\n"
                     "assert sys.argv[1:]==['--version']\n"
                     "print('2.1.308 (Claude Code)')\n")
    check_cli(EXPECTED, cli)


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
def test_main_refuses_real_hostile_cli_output_without_echo(tmp_path, capsys, stream):
    marker = 'UNTRUSTED_OUTPUT_9173'
    cli = executable(tmp_path, f"import sys; sys.{stream}.write({marker!r})")
    assert main(['--manifest', str(CONTRACT), '--executable', cli]) == 1
    output = capsys.readouterr()
    assert output.out == '' and marker not in output.err
    assert output.err.startswith('routine CLI preflight failed: cli_')
    assert main(['--unrecognized-'+marker]) == 1
    assert marker not in capsys.readouterr().err


@pytest.mark.parametrize('valid', [True, False])
def test_actual_composite_steps_execute_helper_and_export_env(tmp_path, valid):
    import yaml
    action_path = ROOT / '.github/actions/check-routine-cli'
    action = yaml.safe_load((action_path / 'action.yml').read_text())
    cli = executable(tmp_path, "print('"+('2.1.308' if valid else '2.1.307')+" (Claude Code)')")
    # A PATH-spoof interpreter must never be selected. The action exposes no
    # executable override; append one only after validating its complete argv.
    shim=tmp_path/'python3'
    shim.write_text('#!/bin/sh\nexit 77\n')
    shim.chmod(0o700)
    envfile = tmp_path / 'jobenv'
    env = {'PATH': str(tmp_path)+':/usr/bin:/bin', 'GITHUB_ENV': str(envfile)}
    for step in action['runs']['steps']:
        assert step['shell']=='/usr/bin/bash --noprofile --norc -euo pipefail {0}'
        command = step['run'].replace('${{ github.action_path }}', str(action_path))
        if step is action['runs']['steps'][1]:
            argv=shlex.split(command)
            assert argv[:2]==['/usr/bin/python3','-I']
            assert [str(Path(argv[2]).resolve()),argv[3],str(Path(argv[4]).resolve())]==[
                str(ROOT/'.github/scripts/routine_cli.py'),'--manifest',str(CONTRACT)]
            assert len(argv)==5
            command+=' --executable '+shlex.quote(cli)
        run = subprocess.run(['/usr/bin/bash','--noprofile','--norc','-euo','pipefail','-c',command], cwd=tmp_path, env=env,
                             capture_output=True, text=True, timeout=15)
        if step is action['runs']['steps'][0]:
            assert run.returncode == 0, run.stderr
    assert envfile.read_text() == 'DISABLE_AUTOUPDATER=1\n'
    assert run.returncode == (0 if valid else 1), run.stderr
    assert run.stdout == ('routine CLI preflight passed\n' if valid else '')
    if not valid:
        assert 'cli_version_mismatch' in run.stderr


def test_default_location_ignores_untrusted_path(tmp_path, monkeypatch):
    import routine_cli
    fake=executable(tmp_path,"print('2.1.308 (Claude Code)')")
    monkeypatch.setenv('PATH',str(tmp_path))
    observed=[]
    def observe(_contract, executable, _directory):
        observed.append(executable)
    monkeypatch.setattr(routine_cli,'_check_cli',observe)
    check_cli(EXPECTED)
    assert observed==['/usr/local/bin/claude'] and fake not in observed


def test_timeout_kills_pipe_holding_descendant(tmp_path):
    import time
    pidfile=tmp_path/'child-pid'
    cli=executable(tmp_path,'import os,time\n'
        'pid=os.fork()\n'
        'if pid==0:\n time.sleep(30)\nelse:\n'
        ' open('+repr(str(pidfile))+',"w").write(str(pid))\n')
    with pytest.raises(PreflightError,match='cli_timeout'):
        check_cli(EXPECTED,cli)
    pid=int(pidfile.read_text())
    for _ in range(30):
        try:
            status=Path('/proc/'+str(pid)+'/stat').read_text().split()[2]
        except FileNotFoundError:
            break
        if status=='Z':break
        time.sleep(0.02)
    else:
        pytest.fail('descendant remains running after timeout')
