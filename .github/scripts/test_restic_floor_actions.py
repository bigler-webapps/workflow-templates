"""Enforce the restic floor on every action that installs restic."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest


ACTION_ROOT = Path(__file__).resolve().parents[1] / "actions"
GUARD = "Check restic version floor"
MESSAGE = (
    "restic_version_floor: restic ${found} is below 0.16, "
    "refusing to open a B2 repository"
)


def steps(text):
    return re.split(r"(?=^    - name:)", text.split("\nruns:", 1)[1], flags=re.M)[1:]


def run_block(step):
    body = step.split("      run: |\n", 1)[1]
    lines = []
    for line in body.splitlines():
        if line.strip() and not line.startswith("        "):
            break
        lines.append(line[8:] if line.startswith("        ") else line)
    return "\n".join(lines).strip()


def actions():
    return {path.parent.name: steps(path.read_text(encoding="utf-8"))
            for path in ACTION_ROOT.glob("*/action.yml")}


def test_all_restic_installers_have_the_same_ordered_guard():
    all_actions = actions()
    install = re.compile(r"\bapt(?:-get)?\s+install\b[^\n]*\brestic\b")
    installers = {name for name, entries in all_actions.items()
                  if any(install.search(step) for step in entries)}
    guarded = {name for name, entries in all_actions.items()
               if any(GUARD in step for step in entries)}
    assert installers == guarded
    assert installers  # Avoid a vacuous pass if the inventory breaks.
    blocks = []
    for name in sorted(installers):
        entries = all_actions[name]
        install_index = next(i for i, step in enumerate(entries) if install.search(step))
        guard_indexes = [i for i, step in enumerate(entries) if GUARD in step]
        assert guard_indexes == [install_index + 1], name
        guard_index = guard_indexes[0]
        # The install step itself logs `restic version`; it opens no repository.
        repository_commands = [i for i, step in enumerate(entries)
                               if i != install_index and i != guard_index
                               and re.search(r"\brestic\s+(?:check|snapshots|init|ls|dump|restore|backup|forget|unlock)\b", step)]
        assert repository_commands and guard_index < min(repository_commands), name
        block = run_block(entries[guard_index])
        assert MESSAGE in block
        blocks.append(block)
    assert len(set(blocks)) == 1


@pytest.mark.parametrize(("output", "status", "expected", "message"), [
    ("restic 0.15.2 compiled with go", 0, 1, "restic 0.15.2 is below 0.16"),
    ("restic 0.16.0 compiled with go", 0, 0, ""),
    ("restic 1.0.0 compiled with go", 0, 0, ""),
    ("restic 0.9.10 compiled with go", 0, 1, "restic 0.9.10 is below 0.16"),
    ("unknown", 0, 1, "unparseable"),
    ("restic 0.16.0", 1, 1, "unable to run"),
])
def test_floor_behavior(output, status, expected, message):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash unavailable")
    block = run_block(next(step for step in actions()["restic-check-b2"] if GUARD in step))
    script = ('stub_output="$1"; stub_status="$2"\n'
              'restic() { printf "%s\\n" "$stub_output"; return "$stub_status"; }\n' + block)
    result = subprocess.run([bash, "-c", script, "floor-test", output, str(status)],
                            capture_output=True, text=True)
    if result.returncode == 0xC0000142:
        pytest.skip("bash failed to initialize on Windows")
    if "Windows Subsystem for Linux has no installed distributions" in result.stderr:
        pytest.skip("bash requires an installed WSL distribution")
    assert result.returncode == expected, result.stderr
    if message:
        assert "restic_version_floor: " in result.stderr
        assert message in result.stderr
