"""The issuer-mode switch of the CI profile (WM-SEC-18 C2): defaults stay local, the CI-side client signs
and parses the issuer protocol, the drain controller decides idleness locally, and the tasks gate the
switch."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment

ROLE = Path(__file__).parents[1]
TEMPLATES = ROLE / "templates"
BASH = shutil.which("bash")

GOLDEN_REQUEST = (b'{"version":1,"caller":"machine-a","slot":1,"issued_at":1000000,"expires_at":1000020,'
                  b'"nonce":"nonce-golden","request_id":"id-golden",'
                  b'"signature":"15a6af37c4b653bd9c78d1cd0e018baccd54669193bba31d4ea16028007904da"}\n')
KEY = "a" * 32  # client-side tests only; the issuer additionally needs ten distinct characters


def render(name: str, **context) -> str:
    env = Environment(trim_blocks=True, lstrip_blocks=True)
    return env.from_string((TEMPLATES / name).read_text(encoding="utf-8")).render(**context)


def bash_path(path: Path) -> str:
    value = str(path)
    if os.name == "nt":
        value = "/" + value[0].lower() + value[2:].replace("\\", "/")
    return value


class FakeIssuer:
    """One-connection TCP server: records the request bytes, answers with `reply(request_bytes)`."""

    def __init__(self, reply):
        self.reply, self.received = reply, None
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        connection, _ = self.server.accept()
        with connection:
            chunks = []
            while True:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
            self.received = b"".join(chunks)
            connection.sendall(self.reply(self.received))

    def close(self):
        self.server.close()


@pytest.fixture
def client(tmp_path):
    """The rendered CI-side client as a module, configured against a loopback port."""
    base = tmp_path / "runner"
    base.mkdir()
    source = tmp_path / "ci_issue_jit.py"
    source.write_text(render("ci-issue-jit.py.j2", github_runner_base_dir=str(base).replace("\\", "/")),
                      encoding="utf-8", newline="\n")
    spec = importlib.util.spec_from_file_location("ci_issue_jit", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def configure(port, *, host="127.0.0.1", caller="machine-a", key=KEY):
        (base / "issuer.json").write_text(json.dumps({"host": host, "port": port, "caller": caller}),
                                          encoding="utf-8")
        (base / "issuer.key").write_text(key + "\n", encoding="ascii")

    module.configure = configure
    return module


def run_main(client, capsys, name="netcup-runner-1-slot1"):
    with pytest.raises(SystemExit) as caught:
        client.main([name])
        raise SystemExit(0)
    out = capsys.readouterr()
    return caught.value.code, out.out, out.err


def test_defaults_keep_the_local_broker_and_the_switch_closed():
    defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text(encoding="utf-8"))
    assert defaults["github_runner_jit_broker"] == "local"
    assert defaults["github_runner_issuer_ready"] is False
    assert defaults["github_runner_issuer_key"] == ""


def test_the_request_is_signed_exactly_as_the_issuer_verifies_it(client, capsys, monkeypatch):
    values = iter(["nonce-golden", "id-golden"])
    monkeypatch.setattr(client.time, "time", lambda: 1000000.0)
    monkeypatch.setattr(client.secrets, "token_hex", lambda _n: next(values))
    server = FakeIssuer(lambda raw: json.dumps({"request_id": "id-golden", "expires_at": 1000020,
                                                "encoded_jit_config": "ENCODED"}).encode() + b"\n")
    client.configure(server.port)
    code, out, err = run_main(client, capsys)
    server.close()
    assert (code, out, err) == (0, "ENCODED", "")
    assert json.loads(server.received) == json.loads(GOLDEN_REQUEST)
    assert server.received.endswith(b"\n") and server.received.count(b"\n") == 1


@pytest.mark.parametrize("reply, expected", [
    (b'{"error":"slot_unavailable"}\n', "slot_unavailable"),
    (b'{"error":"Bad Code!"}\n', "issuer_refused"),
    (b'{"request_id":"other","expires_at":1,"encoded_jit_config":"X"}\n', "response_invalid"),
    (b'{"request_id":"id-golden","expires_at":1,"encoded_jit_config":"X","extra":1}\n', "response_invalid"),
    (b'not json\n', "response_invalid"),
    (b'{"error":"slot_unavailable"}', "response_invalid"),
])
def test_issuer_answers_become_one_fixed_code(client, capsys, monkeypatch, reply, expected):
    values = iter(["nonce-golden", "id-golden"])
    monkeypatch.setattr(client.secrets, "token_hex", lambda _n: next(values))
    server = FakeIssuer(lambda raw: reply)
    client.configure(server.port)
    code, out, err = run_main(client, capsys)
    server.close()
    assert code == 1 and out == "" and err == "ci-issue-jit: " + expected + "\n"
    assert KEY not in err


def test_an_unreachable_issuer_and_a_bad_setup_fail_with_fixed_codes(client, capsys, tmp_path):
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    client.configure(port)
    assert run_main(client, capsys)[2] == "ci-issue-jit: issuer_unavailable\n"
    for bad in ({"host": "not-an-ip"}, {"port": 80}, {"caller": "bad caller"}, {"key": "short"},
                {"key": "k" * 31 + " "}, {"key": "ab" * 20 + "$!"}):
        client.configure(bad.get("port", port), **{key: value for key, value in bad.items() if key != "port"})
        assert run_main(client, capsys)[2] == "ci-issue-jit: config_invalid\n", bad
    (Path(client.CONFIG_PATH)).unlink()
    assert run_main(client, capsys)[2] == "ci-issue-jit: config_unavailable\n"
    for name in ("netcup-runner-1-slot6", "netcup-runner-1-slot0", "x", "netcup-runner-1-slot1\n", "a/../slot1"):
        assert run_main(client, capsys, name)[2] == "ci-issue-jit: arguments_invalid\n", name


def test_the_issuer_mode_wrapper_has_no_github_credential_path_and_the_same_drain_wait():
    issuer = render("jit-config-issuer.sh.j2", github_runner_base_dir="/opt/gh-runner")
    local = render("jit-config.sh.j2", github_runner_app_id="1", github_runner_app_installation_id="2",
                   github_runner_org="o", github_runner_base_dir="/opt/gh-runner", github_runner_group_id=1,
                   github_runner_labels="self-hosted", github_runner_slot_extra_labels={})
    code = "\n".join(line for line in issuer.splitlines() if not line.lstrip().startswith("#"))
    for forbidden in ("curl", "openssl", "app.pem", "generate-jitconfig", "api.github.com", "--deregister-if-idle"):
        assert forbidden not in code
    assert 'exec /usr/bin/python3 -I "/opt/gh-runner/bin/ci-issue-jit" "${RUNNER_NAME}"' in issuer

    def wait(text):
        start = text.index('DRAIN_MARKER="/run/gh-runner.drain"')
        end = text.index("done\n", start) + len("done\n")
        return "\n".join(line.strip() for line in text[start:end].splitlines())

    assert wait(issuer) == wait(local)


@pytest.mark.skipif(BASH is None, reason="bash is required")
@pytest.mark.parametrize("option", ["--deregister-if-idle", "-x"])
def test_the_issuer_wrapper_refuses_options(tmp_path, option):
    script = tmp_path / "jit-config.sh"
    script.write_text(render("jit-config-issuer.sh.j2", github_runner_base_dir=bash_path(tmp_path)),
                      encoding="utf-8", newline="\n")
    result = subprocess.run([BASH, str(script), option, "host-slot1"], capture_output=True, text=True, check=False)
    assert result.returncode == 2 and result.stdout == ""


@pytest.fixture
def drain(tmp_path):
    runner = tmp_path / "runner"
    (runner / "bin").mkdir(parents=True)
    ctx = dict(github_runner_base_dir=bash_path(runner), github_runner_slots=2, github_runner_retired_slots=[],
               github_runner_jit_broker="issuer")
    script = runner / "bin" / "gh-runner-drain.sh"
    body = render("gh-runner-drain.sh.j2", **ctx).replace("/run/gh-runner.drain", bash_path(tmp_path / "marker"))
    script.write_text(body, encoding="utf-8", newline="\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, content in (
        ("hostname", "#!/usr/bin/env bash\necho netcup-runner-1\n"),
        ("systemctl", "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> \"$SYSTEMCTL_LOG\"\n"),
    ):
        (bindir / name).write_text(content, encoding="utf-8", newline="\n")
        (bindir / name).chmod(0o755)
    # A jit-config.sh that records any call: issuer mode must never call it from the drain controller.
    jit = runner / "bin" / "jit-config.sh"
    jit.write_text("#!/usr/bin/env bash\necho called >> \"$JIT_LOG\"\nexit 0\n", encoding="utf-8", newline="\n")
    jit.chmod(0o755)
    proc = tmp_path / "proc"
    proc.mkdir()
    env = os.environ.copy()
    env.update(PATH=str(bindir) + os.pathsep + env.get("PATH", ""), SYSTEMCTL_LOG=str(tmp_path / "systemctl.log"),
               JIT_LOG=str(tmp_path / "jit.log"), GH_RUNNER_PROC_ROOT=bash_path(proc))
    return tmp_path, script, proc, env


def add_process(proc: Path, pid: str, command: bytes, slot: int):
    (proc / pid).mkdir()
    (proc / pid / "cmdline").write_bytes(command + b"\0")
    (proc / pid / "cgroup").write_text("0::/system.slice/gh-runner@%d.service\n" % slot)


@pytest.mark.skipif(BASH is None, reason="bash is required")
def test_issuer_mode_drain_restarts_only_slots_without_a_worker_and_never_calls_the_broker(drain):
    root, script, proc, env = drain
    add_process(proc, "101", b"/opt/gh-runner/bin/Runner.Worker", 1)
    add_process(proc, "202", b"/opt/gh-runner/bin/Runner.Listener", 2)
    result = subprocess.run([BASH, str(script), "begin", "65"], capture_output=True, text=True, env=env, check=False)
    assert result.returncode == 0, result.stderr
    assert (root / "systemctl.log").read_text().splitlines() == ["restart --no-block gh-runner@2.service"]
    assert not (root / "jit.log").exists()
    assert int((root / "marker").read_text().strip()) >= int(time.time()) + 64


@pytest.mark.skipif(BASH is None, reason="bash is required")
def test_issuer_mode_status_counts_workers_and_listeners_as_busy_and_nothing_as_idle(drain):
    root, script, proc, env = drain
    add_process(proc, "101", b"/opt/gh-runner/bin/Runner.Worker", 1)
    (proc / "uptime").write_text("500.00 0.00\n")
    (proc / "101" / "stat").write_text("101 (Runner.Worker) S " + "0 " * 18 + "100\n")
    add_process(proc, "202", b"/opt/gh-runner/bin/Runner.Listener", 2)
    status = lambda: subprocess.run([BASH, str(script), "status"], capture_output=True, text=True, env=env,
                                    check=False).stdout
    out = status()
    assert "slot 1 busy" in out and "slot 2 busy listener=present-or-unknown" in out and "busy_slots=2" in out
    shutil.rmtree(proc / "202")
    out = status()
    assert "slot 2 idle" in out and "busy_slots=1" in out
    (proc / "101" / "cgroup").unlink()
    assert "busy_slots=2" in status()
    assert not (root / "jit.log").exists()


def test_the_local_drain_rendering_is_unchanged_by_the_switch():
    local = render("gh-runner-drain.sh.j2", github_runner_base_dir="/opt/gh-runner", github_runner_slots=2,
                   github_runner_retired_slots=[])
    explicit = render("gh-runner-drain.sh.j2", github_runner_base_dir="/opt/gh-runner", github_runner_slots=2,
                      github_runner_retired_slots=[], github_runner_jit_broker="local")
    assert local == explicit
    assert "--deregister-if-idle" in local and "listener_for_slot" not in local


def test_the_tasks_gate_the_switch_and_keep_local_the_default():
    tasks = yaml.safe_load((ROLE / "tasks" / "ci.yml").read_text(encoding="utf-8"))
    named = {task["name"]: task for task in tasks}
    issuer_only = ("Assert the issuer-mode switch is complete and acknowledged",
                   "Remove the GitHub App private key from this host (issuer mode)", "Deploy issuer client configuration",
                   "Deploy issuer caller key", "Deploy issuer client")
    for name in issuer_only:
        assert named[name]["when"] == "github_runner_jit_broker == 'issuer'", name
    assert named["Deploy GitHub App private key"]["when"] == "github_runner_jit_broker == 'local'"
    names = [task["name"] for task in tasks]
    gate = names.index("Assert the issuer-mode switch is complete and acknowledged")
    assert gate < names.index("Remove the GitHub App private key from this host (issuer mode)")
    assert gate < names.index("Deploy issuer caller key") < names.index("Deploy JIT-config broker script")
    assert named["Deploy issuer caller key"]["no_log"] is True
    assert named["Deploy issuer caller key"]["ansible.builtin.copy"]["mode"] == "0600"
    assert named["Deploy issuer client"]["ansible.builtin.template"]["owner"] == "root"
    assert "jit-config-issuer.sh.j2" in named["Deploy JIT-config broker script"]["ansible.builtin.template"]["src"]
    required = named["Assert required variables are set"]["ansible.builtin.assert"]["that"]
    for variable in ("github_runner_app_id", "github_runner_app_private_key", "github_runner_app_installation_id"):
        assert any(variable in line and "github_runner_jit_broker == 'issuer' or" in line for line in required), variable
    gate_conditions = named["Assert the issuer-mode switch is complete and acknowledged"]["ansible.builtin.assert"]["that"]
    assert "github_runner_issuer_ready is sameas true" in gate_conditions
    assert "github_runner_issuer_key | list | unique | length >= 10" in gate_conditions
    cleanup = named["Remove issuer client files after a return to the local broker"]
    assert cleanup["when"] == "github_runner_jit_broker == 'local'"
    assert cleanup["loop"] == ["issuer.key", "issuer.json", "bin/ci-issue-jit"]
