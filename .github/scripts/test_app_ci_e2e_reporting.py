from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest

import yaml

from test_app_ci_e2e_job import CAPTURE_BASH, E2E_JOB, shell_body, step_body

WORKFLOW = Path(__file__).parents[1] / 'workflows' / 'app-ci.yml'
workflow = yaml.safe_load(WORKFLOW.read_text(encoding='utf-8'))
inputs = workflow[True]['workflow_call']['inputs']
steps = {step.get('name'): step for step in workflow['jobs']['e2e']['steps']}
capture = steps['Capture E2E screens and report']


def test_uploads_are_opt_in_and_keep_eligibility_and_retention():
    assert inputs['e2e-upload-artifacts']['type'] == 'boolean'
    assert inputs['e2e-upload-artifacts']['default'] is False
    failure = steps['Upload E2E failure artifacts']
    screen = steps['Upload E2E capture artifact']
    assert 'always() && inputs.e2e-upload-artifacts && (failure() || steps.quarantine.outputs.had_failure' in failure['if']
    assert screen['if'] == '${{ always() && inputs.e2e-upload-artifacts }}'
    assert failure['with']['retention-days'] == 7
    assert failure['with']['path'] == '${{ inputs.frontend-path }}/test-results'
    assert screen['with']['path'] == '${{ inputs.frontend-path }}/${{ inputs.e2e-capture-output-path }}'
    summary = steps['Summarize E2E diagnostic uploads']['run']
    assert 'disabled' in summary and 'uploaded' in summary and 'unavailable' in summary
    assert 'FAILURE_ARTIFACT_ID' in summary and 'CAPTURE_ARTIFACT_ID' in summary
    assert 'Trace + error context: artifact' not in steps['Playwright specs']['run']
    assert 'Trace + error context: artifact' not in steps['Playwright quarantine specs']['run']


def render(tmp_path, screen, report, fresh=True, status=0):
    manifest_dir = tmp_path / 'tests' / 'capture'
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / 'manifest.json').write_text(json.dumps({'screens': [screen]}), encoding='utf-8')
    output = tmp_path / 'output'
    output.mkdir(exist_ok=True)
    (output / 'report.json').write_text(json.dumps(report), encoding='utf-8')
    summary = tmp_path / 'summary'
    script = capture['run'].split("python3 - <<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
    env = dict(os.environ, CAPTURE_CELL=screen['id'], CAPTURE_FRESH=str(fresh).lower(),
               CAPTURE_STATUS=str(status), CAPTURE_OUTPUT_PATH=str(output), GITHUB_STEP_SUMMARY=str(summary))
    subprocess.run([sys.executable, '-c', script], cwd=tmp_path, env=env, check=True)
    return summary.read_text(encoding='utf-8')


def test_capture_results_and_diagnostics_are_not_disclosed(tmp_path):
    screen = {'id': 'view-a', 'widths': [375, 1280, 900]}
    report = {'cells': [{'id': 'view-a', 'width': 375, 'failures': []},
                        {'id': 'view-a', 'width': 1280, 'failures': ['SECRET failure body'],
                         'screenshot': 'SECRET screenshot', 'responses': ['SECRET response']}]}
    text = render(tmp_path, screen, report)
    assert '`view-a` / 375px: passed' in text
    assert '`view-a` / 1280px: failed' in text
    assert '`view-a` / 900px: missing evidence' in text
    assert 'SECRET' not in text
    failed_capture = render(tmp_path, screen, report, status=7)
    assert '`view-a` / 375px: capture command failed' in failed_capture
    assert '`view-a` / 1280px: failed' in failed_capture
    assert '`view-a` / 900px: missing evidence' in failed_capture
    assert 'missing evidence' in render(tmp_path, screen, report, fresh=False)


def test_na_and_invalid_or_duplicate_evidence(tmp_path):
    assert 'n.a.' in render(tmp_path, {'id': 'na', 'widths': [375], 'state': 'n.a.'}, {})
    duplicate = {'cells': [{'id': 'a', 'width': 375, 'failures': []}] * 2}
    assert 'missing evidence' in render(tmp_path, {'id': 'a', 'widths': [375]}, duplicate)
    assert 'Invalid view identifier' in render(tmp_path, {'id': 'bad|SECRET', 'widths': [375]}, {})
    assert 'SECRET' not in render(tmp_path, {'id': 'bad|SECRET', 'widths': [375]}, {})


def test_capture_gate_and_reseed_remain_in_charge():
    assert capture['continue-on-error'] == '${{ !inputs.e2e-capture-gate }}'
    run = capture['run']
    assert 'if [ "${{ inputs.e2e-capture-gate }}" = "true" ]; then exit "$cells_status"' in run
    assert 'if [ "${{ inputs.e2e-capture-gate }}" = "true" ]; then exit 1' in run
    assert 'exit "$worst"' in run
    assert 'worst=$reseed_status' in run
    assert 'capture_summary "$fresh" "$status" || true' in run
    assert 'report.json" -nt "$capture_marker"' in run


def test_all_declared_results_and_incomplete_evidence(tmp_path):
    screen = {'id': 'view', 'widths': list(range(1, 103))}
    report = {'cells': [{'id': 'view', 'width': w, 'failures': []} for w in screen['widths']]}
    assert '`view` / 102px: passed' in render(tmp_path, screen, report)
    report['cells'][-1]['complete'] = False
    assert '`view` / 102px: missing evidence' in render(tmp_path, screen, report)
    report['complete'] = False
    assert '`view` / 1px: passed' in render(tmp_path, screen, report)
    report['invalid'] = True
    assert '`view` / 1px: missing evidence' in render(tmp_path, screen, report)
    manifest = tmp_path / 'tests' / 'capture' / 'manifest.json'
    manifest.write_text(json.dumps({'screens': [{'id': f'v{i}', 'widths': [375]} for i in range(501)]}), encoding='utf-8')
    script = capture['run'].split("python3 - <<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
    summary = tmp_path / 'all-summary'
    env = dict(os.environ, CAPTURE_CELL='*', CAPTURE_FRESH='false', CAPTURE_STATUS='0', CAPTURE_OUTPUT_PATH=str(tmp_path / 'output'), GITHUB_STEP_SUMMARY=str(summary))
    subprocess.run([sys.executable, '-c', script], cwd=tmp_path, env=env, check=True)
    assert '`v500` / 375px: missing evidence' in summary.read_text(encoding='utf-8')


def test_upload_summary_shell(tmp_path):
    import shutil
    import pytest
    bash = str(Path('C:/Program Files/Git/bin/bash.exe')) if sys.platform == 'win32' and Path('C:/Program Files/Git/bin/bash.exe').is_file() else shutil.which('bash')
    if not bash:
        pytest.skip('requires bash')
    script = steps['Summarize E2E diagnostic uploads']['run']
    for enabled, failure, capture_id, expected in [
        ('false', '', '', 'disabled.'),
        ('true', '', '', 'failure artifact unavailable; capture artifact unavailable.'),
        ('true', '123', '456', 'failure artifact uploaded; capture artifact uploaded.'),
        ('true', '123', '', 'failure artifact uploaded; capture artifact unavailable.'),
        ('true', '', '456', 'failure artifact unavailable; capture artifact uploaded.'),
    ]:
        summary = tmp_path / 'summary-shell'
        summary.unlink(missing_ok=True)
        env = dict(os.environ, UPLOAD_ENABLED=enabled, FAILURE_ARTIFACT_ID=failure, CAPTURE_ARTIFACT_ID=capture_id, GITHUB_STEP_SUMMARY=str(summary))
        subprocess.run([bash, '--noprofile', '--norc', '-c', script], cwd=tmp_path, env=env, check=True)
        assert expected in summary.read_text(encoding='utf-8')


@unittest.skipIf(CAPTURE_BASH is None, "requires bash or Windows Git Bash")
class CaptureFreshnessShellTests(unittest.TestCase):
    def test_capture_uses_only_fresh_report_and_handles_missing_marker(self):
        import json
        for mode, expected, status in [('current', 'passed', 0), ('stale', 'missing evidence', 0),
                                       ('reseed', 'missing evidence', 6), ('missing', 'missing evidence', 0),
                                       ('marker', 'missing evidence', 0), ('two', 'passed', 0),
                                       ('failure', 'capture command failed', 7)]:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                bin_dir = root / 'bin'
                bin_dir.mkdir()
                manifest = root / 'tests/capture'
                manifest.mkdir(parents=True)
                (manifest / 'manifest.json').write_text(json.dumps({'screens': [{'id': 'view', 'widths': [375]}] + ([{'id': 'second', 'widths': [375]}] if mode == 'two' else [])}), encoding='utf-8')
                output = root / 'output'
                output.mkdir()
                report = output / 'report.json'
                report.write_text(json.dumps({'cells': [{'id': 'view', 'width': 375, 'failures': []}]}), encoding='utf-8')
                os.utime(report, (1, 1))
                docker = bin_dir / 'docker'
                docker.write_text('#!/usr/bin/env bash\n'
                    'case "$*" in *"node -e"*) echo view; [ "$MODE" = two ] && echo second; exit 0;; esac\n'
                    'if [ "$1" = exec ]; then [ "$MODE" = reseed ] && exit 6; exit 0; fi\n'
                    'if [ "$MODE" = current ] || [ "$MODE" = marker ] || [ "$MODE" = failure ] || [ "$MODE" = two ]; then\n'
                    '  case "$*" in *--routes=second*) cp "$SECOND_FIXTURE" "$CAPTURE_OUTPUT_PATH/report.json";; *) cp "$FIXTURE" "$CAPTURE_OUTPUT_PATH/report.json";; esac\n'
                    '  touch -d "next minute" "$CAPTURE_OUTPUT_PATH/report.json"\n'
                    'fi\n'
                    '[ "$MODE" = failure ] && exit 7\n'
                    'if [ "$MODE" = missing ]; then rm -f "$CAPTURE_OUTPUT_PATH/report.json"; fi\n'
                    'exit 0\n', encoding='utf-8')
                docker.chmod(0o755)
                fixture = root / 'fixture.json'
                fixture.write_text(report.read_text(encoding='utf-8'), encoding='utf-8')
                second_fixture = root / 'second-fixture.json'
                second_fixture.write_text(json.dumps({'complete': False, 'cells': [{'id': 'second', 'width': 375, 'failures': []}]}), encoding='utf-8')
                python = bin_dir / 'python3'
                python.write_text(f'#!/usr/bin/env bash\nexec "{Path(sys.executable).as_posix()}" "$@"\n', encoding='utf-8')
                python.chmod(0o755)
                if mode == 'marker':
                    marker = bin_dir / 'mktemp'
                    marker.write_text('#!/usr/bin/env bash\nexit 1\n', encoding='utf-8')
                    marker.chmod(0o755)
                script = shell_body(step_body(E2E_JOB, 'Capture E2E screens and report'))
                for key, value in {'${{ inputs.e2e-capture-gate }}': 'true', '${{ inputs.e2e-playwright-image }}': 'image', '${{ inputs.frontend-path }}': 'frontend', '${{ github.workspace }}': root.as_posix(), '${{ inputs.e2e-seed-command }}': 'seed', '${{ inputs.e2e-capture-command }}': 'capture'}.items():
                    script = script.replace(key, value)
                script = 'PATH="$PWD/bin:/usr/bin:/bin"; export PATH\n[ "$(command -v docker)" = "$PWD/bin/docker" ] || exit 99\n' + script
                env = dict(os.environ, MODE=mode, FIXTURE=fixture.as_posix(), SECOND_FIXTURE=second_fixture.as_posix(), CAPTURE_OUTPUT_PATH=output.as_posix(), GITHUB_STEP_SUMMARY=(root / 'summary').as_posix(), E2E_APP_CONTAINER='app')
                env.pop('BASH_ENV', None)
                result = subprocess.run([CAPTURE_BASH, '--noprofile', '--norc', '-c', script], cwd=root, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, status, result.stdout + result.stderr)
                summary = (root / 'summary').read_text(encoding='utf-8')
                self.assertIn(f'`view` / 375px: {expected}', summary)
                if mode == 'two':
                    self.assertIn('`second` / 375px: passed', summary)
                if mode == 'failure':
                    self.assertNotIn('`view` / 375px: passed', summary)
