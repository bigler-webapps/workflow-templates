"""WM-SEC-7 — guard Promtail's Docker log redaction pipeline."""

import re
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment

ROLE = Path(__file__).resolve().parents[1]
TEMPLATE = ROLE / "templates" / "promtail-config.yml.j2"


@pytest.fixture(params=[False, True], ids=["keep-debug", "drop-debug"])
def rendered_config(request):
    source = TEMPLATE.read_text(encoding="utf-8")
    rendered = Environment().from_string(source).render(
        promtail_loki_url="http://127.0.0.1:3100/loki/api/v1/push",
        inventory_hostname="staging",
        promtail_drop_debug=request.param,
        promtail_redact_secrets=True,
    )
    return request.param, yaml.safe_load(rendered)


def _docker_stages(config):
    jobs = config["scrape_configs"]
    docker = next(job for job in jobs if job["job_name"] == "docker")
    return docker["pipeline_stages"]


def _apply_replace_stages(line, stages):
    for stage in stages:
        if "replace" not in stage:
            continue
        spec = stage["replace"]
        expression = spec["expression"]
        assert not re.search(r"\(\?[=!<]|\\[1-9]", expression), expression
        regex = re.compile(expression)

        def replace_capture(match):
            start, end = match.span(1)
            return match.string[match.start():start] + spec["replace"] + match.string[end:match.end()]

        line = regex.sub(replace_capture, line)
    return line


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('{"RequestPath":"/reset/UID123/TOKEN456/","Status":200}', '{"RequestPath":"/reset/<redacted>/<redacted>/","Status":200}'),
        ('GET /invite/USER7/INVITE8/ HTTP/1.1', 'GET /invite/<redacted>/<redacted>/ HTTP/1.1'),
        ('GET /api/users/password-reset/USER9/RESET10/ HTTP/1.1', 'GET /api/users/password-reset/<redacted>/<redacted>/ HTTP/1.1'),
        ('GET /photo/42/?k=GALLERYKEY HTTP/1.1', 'GET /photo/42/?k=<redacted> HTTP/1.1'),
        ('GET /api/v1/webhooks/kuma/KUMASECRET/ HTTP/1.1', 'GET /api/v1/webhooks/kuma/<redacted>/ HTTP/1.1'),
        ('{"RequestURI":"/photo/?a=1\\u0026k=JSONKEY\\u0026b=2"}', '{"RequestURI":"/photo/?a=1\\u0026k=<redacted>\\u0026b=2"}'),
        ('GET /reset/U1/T1/?k=K1 /invite/U2/T2/ HTTP/1.1', 'GET /reset/<redacted>/<redacted>/?k=<redacted> /invite/<redacted>/<redacted>/ HTTP/1.1'),
        ('GET /api/users/me/ /api/healthz /reset-password-help/?page=2&sort=k_name HTTP/1.1', 'GET /api/users/me/ /api/healthz /reset-password-help/?page=2&sort=k_name HTTP/1.1'),
        ('application log mentions invite but has no credential path', 'application log mentions invite but has no credential path'),
        ('GET /photo/?mk=KEEP&kk=ALSOKEEP HTTP/1.1', 'GET /photo/?mk=KEEP&kk=ALSOKEEP HTTP/1.1'),
        ('GET /photo/?page=2&k=GALLERYKEY HTTP/1.1', 'GET /photo/?page=2&k=<redacted> HTTP/1.1'),
        ('GET /photo/?k=FIRST&page=2&k=SECOND HTTP/1.1', 'GET /photo/?k=<redacted>&page=2&k=<redacted> HTTP/1.1'),
    ],
)
def test_docker_redaction_replaces_only_secret_values(rendered_config, line, expected):
    _, config = rendered_config
    stages = _docker_stages(config)
    assert _apply_replace_stages(line, stages) == expected


def test_redaction_precedes_optional_debug_drop_and_auth_job_is_untouched(rendered_config):
    drop_debug, config = rendered_config
    stages = _docker_stages(config)
    replace_indexes = [i for i, stage in enumerate(stages) if "replace" in stage]
    assert len(replace_indexes) == 4
    assert replace_indexes == list(range(4))
    drop_indexes = [i for i, stage in enumerate(stages) if "drop" in stage]
    assert bool(drop_indexes) is drop_debug
    if drop_indexes:
        assert drop_indexes[0] > replace_indexes[-1]
        assert stages[drop_indexes[0]]["drop"]["expression"] == r"(?i)level=debug|\blevel\s*:\s*debug\b"

    auth = next(job for job in config["scrape_configs"] if job["job_name"] == "auth_log")
    assert "pipeline_stages" not in auth


@pytest.mark.parametrize("drop_debug", [False, True], ids=["keep-debug", "drop-debug"])
def test_redaction_toggle_can_disable_replace_stages(drop_debug):
    rendered = Environment().from_string(TEMPLATE.read_text(encoding="utf-8")).render(
        promtail_loki_url="http://127.0.0.1:3100/loki/api/v1/push",
        inventory_hostname="staging",
        promtail_drop_debug=drop_debug,
        promtail_redact_secrets=False,
    )
    config = yaml.safe_load(rendered)
    stages = _docker_stages(config)
    assert not any("replace" in stage for stage in stages)
    # The pre-existing debug drop is independent of the redaction toggle.
    assert any("drop" in stage for stage in stages) is drop_debug


def test_changed_config_restarts_a_running_promtail():
    """The config is a bind mount; `compose up -d` alone leaves a running container on the old pipeline."""
    tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text(encoding="utf-8"))[0]["block"]
    by_name = {task["name"]: task for task in tasks}
    render = by_name["Render Promtail config"]
    assert render["register"] == "_promtail_config"
    restart = by_name["Restart Promtail to load a changed config"]
    assert restart["ansible.builtin.command"]["cmd"] == "docker compose restart promtail"
    assert restart["when"] == ["_promtail_config is changed", "_promtail_up is not changed"]
    names = [task["name"] for task in tasks]
    assert names.index("Restart Promtail to load a changed config") > names.index("Start / update Promtail stack")
