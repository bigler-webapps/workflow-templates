"""Render and exercise runner drain templates with local command fixtures."""
from __future__ import annotations
import os, shutil, subprocess, time
from pathlib import Path
import pytest
from jinja2 import Environment

ROLE = Path(__file__).parents[1]
TEMPLATES = ROLE / "templates"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash is required")

def bash_path(path: Path) -> str:
    value = str(path)
    if os.name == "nt":
        value = "/" + value[0].lower() + value[2:].replace("\\", "/")
    return value

def executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="\n")
    path.chmod(0o755)

@pytest.fixture
def setup(tmp_path: Path):
    ctx = dict(github_runner_app_id="123", github_runner_app_installation_id="456",
        github_runner_org="example-org", github_runner_base_dir=bash_path(tmp_path/"runner"),
        github_runner_group_id=1, github_runner_labels="self-hosted,netcup",
        github_runner_slot_extra_labels={}, github_runner_user="gh-runner",
        github_runner_restart_sec=5, github_runner_slots=2, github_runner_retired_slots=[])
    env = Environment(trim_blocks=True, lstrip_blocks=True)
    runner = tmp_path/"runner"; (runner/"bin").mkdir(parents=True)
    marker = tmp_path/"marker"; files = {}
    for name in ("jit-config.sh.j2", "gh-runner-drain.sh.j2"):
        body = env.from_string((TEMPLATES/name).read_text(encoding="utf-8")).render(**ctx)
        body = body.replace("/run/gh-runner.drain", bash_path(marker))
        dest = runner/"bin"/("jit-config.sh" if name.startswith("jit") else "gh-runner-drain.sh")
        dest.write_text(body, encoding="utf-8", newline="\n"); dest.chmod(0o755); files[name] = dest
    bindir = tmp_path/"bin"; bindir.mkdir()
    executable(bindir/"curl", "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> \"$GH_CALL_LOG\"\nif [[ \" $* \" == *generate-jitconfig* ]]; then printf '{}\\n%s' \"${GH_JIT_CODE:-201}\"; elif [[ \" $* \" == *\" -w %{http_code} \"* ]]; then printf '%s' \"$GH_DELETE_CODE\"; else printf '{}'; fi\n")
    executable(bindir/"jq", "#!/usr/bin/env bash\ncat >/dev/null\ncase \"$*\" in\n  *.token*) echo token ;;\n  *select*) printf '%s' \"$GH_RECORD\" ;;\n  *.encoded_jit_config*) echo encoded-config ;;\n  *) echo '{}' ;;\nesac\n")
    executable(bindir/"openssl", "#!/usr/bin/env bash\ncat\n")
    executable(bindir/"hostname", "#!/usr/bin/env bash\necho netcup-runner-1\n")
    executable(bindir/"systemctl", "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> \"$SYSTEMCTL_LOG\"\n")
    executable(bindir/"sleep", "#!/usr/bin/env bash\nrm -f \"$REMOVE_MARKER\"\n")
    shenv = os.environ.copy()
    shenv.update(PATH=str(bindir)+os.pathsep+shenv.get("PATH",""), GH_CALL_LOG=str(tmp_path/"gh.log"),
        SYSTEMCTL_LOG=str(tmp_path/"systemctl.log"), REMOVE_MARKER=str(marker),
        GH_RUNNER_PROC_ROOT=bash_path(tmp_path/"proc"), GH_DELETE_CODE="204", GH_RECORD="")
    (tmp_path/"key.pem").write_text("fixture", encoding="utf-8")
    return tmp_path, runner, marker, files, shenv

def run(setup, script: Path, *args: str, extra: dict[str,str]|None=None):
    root, _, _, _, shenv = setup
    return subprocess.run([BASH,str(script),*args],capture_output=True,text=True,
        env={**shenv,**(extra or {}),"PEM_FILE":str(root/"key.pem")},check=False)

def test_marker_blocks_and_expired_or_absent_proceeds(setup):
    root, _, marker, files, _ = setup; jit=files["jit-config.sh.j2"]
    marker.write_text(str(int(time.time())+60),encoding="utf-8")
    assert run(setup,jit,"host-slot1").returncode == 0 and not marker.exists()
    assert "generate-jitconfig" in (root/"gh.log").read_text(encoding="utf-8")
    for expiry in ("1\n",None):
        (root/"gh.log").write_text("",encoding="utf-8")
        if expiry is None: marker.unlink(missing_ok=True)
        else: marker.write_text(expiry,encoding="utf-8")
        assert run(setup,jit,"host-slot1").returncode == 0
        assert "generate-jitconfig" in (root/"gh.log").read_text(encoding="utf-8")

@pytest.mark.parametrize(("record","code","expected"),[
    ("","204",0),("12\tfalse","204",0),("12\ttrue","204",3),
    ("12\tfalse","422",3),("12\tfalse","500",1)])
def test_deregister_exit_codes_and_never_generates_jit(setup,record,code,expected):
    root,_,_,files,_=setup
    result=run(setup,files["jit-config.sh.j2"],"--deregister-if-idle","host-slot1",
        extra={"GH_RECORD":record,"GH_DELETE_CODE":code})
    assert result.returncode == expected
    calls=(root/"gh.log").read_text(encoding="utf-8")
    assert "generate-jitconfig" not in calls
    if record and expected == 0: assert "/12" in calls

def test_begin_restarts_only_idle_and_sets_expiry(setup):
    root,runner,marker,files,_=setup
    executable(runner/"bin"/"jit-config.sh","#!/usr/bin/env bash\nif [[ \"$2\" == *slot2 ]]; then exit 3; fi\nexit 0\n")
    assert run(setup,files["gh-runner-drain.sh.j2"],"begin","65").returncode == 0
    assert int(marker.read_text().strip()) >= int(time.time())+64
    assert (root/"systemctl.log").read_text().splitlines() == ["restart --no-block gh-runner@1.service"]

def test_status_reports_worker_and_unreadable_cgroup_as_busy(setup):
    root,runner,_,files,_=setup; proc=root/"proc"; worker=proc/"101"; worker.mkdir(parents=True)
    (proc/"uptime").write_text("500.00 0.00\n")
    (worker/"cmdline").write_bytes(b"/opt/gh-runner/bin/Runner.Worker\0")
    (worker/"stat").write_text("101 (Runner.Worker) S "+"0 "*18+"100\n")
    (worker/"cgroup").write_text("0::/system.slice/gh-runner@1.service\n")
    work=runner/"slot-1"/"_work"/"group"/"repo"; work.mkdir(parents=True)
    result=run(setup,files["gh-runner-drain.sh.j2"],"status")
    assert "slot 1 busy" in result.stdout and "elapsed=" in result.stdout and bash_path(work) in result.stdout
    assert "busy_slots=1" in result.stdout
    (worker/"cgroup").unlink()
    assert "slot 1 busy elapsed=unknown repository=unknown" in run(setup,files["gh-runner-drain.sh.j2"],"status").stdout

def test_end_idempotent_and_unit_timeout_infinite(setup):
    _,runner,marker,files,_=setup; marker.write_text("123\n")
    assert run(setup,files["gh-runner-drain.sh.j2"],"end").returncode == 0
    assert run(setup,files["gh-runner-drain.sh.j2"],"end").returncode == 0 and not marker.exists()
    unit=Environment(trim_blocks=True,lstrip_blocks=True).from_string((TEMPLATES/"gh-runner@.service.j2").read_text())
    rendered=unit.render(github_runner_user="gh-runner",github_runner_base_dir="/opt/gh-runner",github_runner_restart_sec=5)
    assert "TimeoutStartSec=infinity" in rendered


def test_expired_marker_that_the_runner_user_cannot_remove_does_not_stop_registration(setup, tmp_path):
    """The slot runs as the runner user; /run is root's. A refused rm of an expired marker must not abort the script."""
    root, _, marker, files, _ = setup
    marker.write_text(str(int(time.time()) - 5), encoding="utf-8")
    refuse = tmp_path / "refuse"
    refuse.mkdir()
    executable(refuse / "rm", "#!/usr/bin/env bash\nexit 1\n")
    path = str(refuse) + os.pathsep + setup[4]["PATH"]
    result = run(setup, files["jit-config.sh.j2"], "host-slot1", extra={"PATH": path})
    assert result.returncode == 0, result.stderr
    assert marker.exists()  # the expired marker is ignored, not required to be removed


def test_a_valid_marker_blocks_registration_until_it_is_gone(setup, tmp_path):
    """A future marker must stop jit-config.sh BEFORE any GitHub call: a failing sleep proves it reached the wait."""
    root, _, marker, files, _ = setup
    marker.write_text(str(int(time.time()) + 600), encoding="utf-8")
    failing = tmp_path / "failing"
    failing.mkdir()
    executable(failing / "sleep", "#!/usr/bin/env bash\nexit 1\n")
    path = str(failing) + os.pathsep + setup[4]["PATH"]
    result = run(setup, files["jit-config.sh.j2"], "host-slot1", extra={"PATH": path})
    assert result.returncode != 0
    log = root / "gh.log"
    assert not log.exists() or log.read_text(encoding="utf-8").strip() == ""


def test_status_counts_a_slot_without_worker_as_busy_when_github_says_busy(setup):
    root, runner, _, files, _ = setup
    (root / "proc").mkdir()
    executable(runner / "bin" / "jit-config.sh", "#!/usr/bin/env bash\nif [[ \"$2\" == *slot2 ]]; then exit 3; fi\nexit 0\n")
    result = run(setup, files["gh-runner-drain.sh.j2"], "status")
    assert "slot 1 idle" in result.stdout
    assert "slot 2 busy github=busy-or-unknown" in result.stdout
    assert "busy_slots=1" in result.stdout


def test_extend_never_shortens_the_marker(setup):
    _, _, marker, files, _ = setup
    marker.write_text(str(int(time.time()) + 7200), encoding="utf-8")
    assert run(setup, files["gh-runner-drain.sh.j2"], "extend", "40").returncode == 0
    assert int(marker.read_text().strip()) >= int(time.time()) + 7100
    marker.unlink()
    assert run(setup, files["gh-runner-drain.sh.j2"], "extend", "40").returncode == 0
    assert int(marker.read_text().strip()) >= int(time.time()) + 39 * 60


def test_a_refused_jit_request_names_its_http_status_for_the_provision_verification(setup):
    """WM-INF-105: curl -sf swallowed the status; the journal must carry "HTTP 409" for a stale busy registration."""
    root, _, marker, files, _ = setup
    marker.unlink(missing_ok=True)
    result = run(setup, files["jit-config.sh.j2"], "host-slot1", extra={"GH_JIT_CODE": "409"})
    assert result.returncode == 1
    assert "ERROR: jit-config.sh: generate-jitconfig failed (HTTP 409)" in result.stderr
    assert result.stdout == ""
    ok = run(setup, files["jit-config.sh.j2"], "host-slot1")
    assert ok.returncode == 0 and ok.stdout == "encoded-config"
