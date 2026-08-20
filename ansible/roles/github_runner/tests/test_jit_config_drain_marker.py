"""INF-44 -- jit-config.sh.j2's drain-marker check must run before any
GitHub API call, and the marker must live OUTSIDE github_runner_base_dir
(which is the gh-runner user's own home directory, the same account every
CI job on this host runs as -- a marker inside it would let any job create
or delete it itself, DoS-ing registrations or defeating the drain).

Run: pytest ansible/roles/github_runner/tests/
"""

from pathlib import Path

ROLE = Path(__file__).resolve().parents[1]
JIT_CONFIG = (ROLE / "templates" / "jit-config.sh.j2").read_text(encoding="utf-8")


def test_drain_marker_is_not_under_github_runner_base_dir():
    assert "{{ github_runner_base_dir }}/.drain" not in JIT_CONFIG
    assert 'DRAIN_MARKER="/run/gh-runner.drain"' in JIT_CONFIG


def test_drain_marker_check_runs_before_any_github_api_call():
    marker_check_pos = JIT_CONFIG.index("DRAIN_MARKER=")
    app_id_pos = JIT_CONFIG.index("APP_ID=")
    assert marker_check_pos < app_id_pos, (
        "the drain check must run before APP_ID/JWT/API-call setup, so a "
        "drained slot never spends a GitHub API call it doesn't need"
    )


def test_drain_marker_check_exits_nonzero_when_present():
    snippet = JIT_CONFIG[JIT_CONFIG.index("DRAIN_MARKER=") : JIT_CONFIG.index("APP_ID=")]
    assert "if [[ -f \"${DRAIN_MARKER}\" ]]; then" in snippet
    assert "exit 1" in snippet
