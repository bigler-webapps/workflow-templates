import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).with_name("ci_test_workers.py")
WORKFLOW = SCRIPT.parents[1] / "workflows" / "app-ci.yml"


def resolve(requested):
    return subprocess.check_output(
        [sys.executable, str(SCRIPT), "resolve", "4", requested], text=True
    ).strip()


def pytest_args(requested, args):
    rendered = subprocess.check_output(
        [sys.executable, str(SCRIPT), "pytest-args", "4", requested, args],
    )
    return rendered.rstrip(b"\0").decode().split("\0") if rendered else []


def test_worker_input_clamping():
    for requested in ("", "x", "0", "-3"):
        assert resolve(requested) == "4"
    for requested, expected in (("2", "2"), ("4", "4"), ("9", "4"), (" 3 ", "3")):
        assert resolve(requested) == expected


def test_pytest_xdist_counts_are_bounded_and_auto_is_preserved():
    for form, expected in (
        ("-n 8", ["-n", "2"]),
        ("-n8", ["-n2"]),
        ("--numprocesses 8", ["--numprocesses", "2"]),
        ("--numprocesses=8", ["--numprocesses=2"]),
    ):
        assert pytest_args("2", form) == expected
    assert pytest_args("2", "-n 2") == ["-n", "2"]
    assert pytest_args("2", "-n auto") == ["-n", "auto"]
    assert pytest_args("2", "-n logical") == ["-n", "logical"]
    assert pytest_args("2", "-q") == ["-q"]


def test_workflow_wires_shared_worker_helper():
    source = WORKFLOW.read_text(encoding="utf-8")
    assert source.count("CI_TEST_WORKERS_CAP: '4'") == 1
    assert 'worker_limit="$(python3 "${GITHUB_WORKSPACE}/.wt-checkout/.github/scripts/ci_test_workers.py" resolve' in source
    test_line = next(line for line in source.splitlines() if 'VITEST_MAX_WORKERS="$worker_limit" pnpm test' in line)
    assert test_line.strip() == 'VITEST_MAX_WORKERS="$worker_limit" pnpm test'  # env only, no CLI flag
    assert "PYTEST_XDIST_AUTO_NUM_WORKERS=\"$(python3 .wt-checkout/.github/scripts/ci_test_workers.py resolve" in source
    assert "ci_test_workers.py pytest-args" in source
    # pytest runs with the CLAMPED arguments, never with the raw input, and no worker input is interpolated
    # into script text (they travel through env).
    pytest_line = next(line for line in source.splitlines() if line.strip().startswith("pytest --import-mode="))
    assert '"${pytest_args[@]}"' in pytest_line and "inputs.pytest-args" not in pytest_line
    for forbidden in ("'${{ inputs.frontend-test-max-workers }}'", "'${{ inputs.backend-test-max-workers }}'", "'${{ inputs.pytest-args }}'"):
        assert forbidden not in source
    assert source.count("BACKEND_TEST_MAX_WORKERS: ${{ inputs.backend-test-max-workers }}") == 1
    assert source.count("FRONTEND_TEST_MAX_WORKERS: ${{ inputs.frontend-test-max-workers }}") == 1
