"""INF-8 — pins the `sync-kuma-notifications` composite action's SMTP inputs.

Mirrors the pre-existing `discord_webhook_url` -> `${DISCORD_WEBHOOK_URL}` contract:
seven new optional inputs, each exposed to the sync script as the matching
`SMTP_*` env var, all with an empty default so an existing caller (this repo has
none yet — that is chunk 2, in webapp-management) sees no behaviour change.

Run: pytest scripts/tests/test_sync_kuma_notifications_action.py
"""

from pathlib import Path

import pytest
import yaml

ACTION = (
    Path(__file__).resolve().parents[2]
    / ".github" / "actions" / "sync-kuma-notifications" / "action.yml"
)

# input name -> the env var it must become
SMTP_INPUTS = {
    "smtp_host": "SMTP_HOST",
    "smtp_port": "SMTP_PORT",
    "smtp_secure": "SMTP_SECURE",
    "smtp_username": "SMTP_USERNAME",
    "smtp_password": "SMTP_PASSWORD",
    "smtp_from": "SMTP_FROM",
    "smtp_to": "SMTP_TO",
}

# INF-59: same optional/empty-default/self-documented-description contract,
# for the one new input this WO adds.
COCKPIT_INPUTS = {
    "cockpit_webhook_secret": "COCKPIT_KUMA_WEBHOOK_SECRET",
}

ALL_OPTIONAL_INPUTS = {**SMTP_INPUTS, **COCKPIT_INPUTS}


@pytest.fixture(scope="module")
def action():
    return yaml.safe_load(ACTION.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def sync_step(action):
    return next(s for s in action["runs"]["steps"] if s.get("name") == "Sync notifications")


@pytest.mark.parametrize("input_name", sorted(ALL_OPTIONAL_INPUTS))
def test_input_is_declared_optional_with_empty_default(action, input_name):
    spec = action["inputs"][input_name]
    assert spec.get("required", False) is False
    assert spec.get("default") == ""


@pytest.mark.parametrize("input_name", sorted(ALL_OPTIONAL_INPUTS))
def test_input_description_names_its_env_var(action, input_name):
    """Same self-documenting convention as discord_webhook_url — the description
    states which ${VAR} the config can reference."""
    assert f"${{{ALL_OPTIONAL_INPUTS[input_name]}}}" in action["inputs"][input_name]["description"]


@pytest.mark.parametrize("input_name,env_name", sorted(ALL_OPTIONAL_INPUTS.items()))
def test_sync_step_maps_input_to_its_env_var(sync_step, input_name, env_name):
    assert sync_step["env"][env_name] == f"${{{{ inputs.{input_name} }}}}"


def test_discord_mapping_is_unchanged(action, sync_step):
    """Guards the pre-existing contract this WO extends, not replaces."""
    assert action["inputs"]["discord_webhook_url"]["default"] == ""
    assert sync_step["env"]["DISCORD_WEBHOOK_URL"] == "${{ inputs.discord_webhook_url }}"


def test_prune_and_config_path_handling_untouched(sync_step):
    run = sync_step["run"]
    assert "INPUT_PRUNE" in sync_step["env"]
    assert "INPUT_CONFIG_PATH" in sync_step["env"]
    assert '--config "$INPUT_CONFIG_PATH"' in run


def test_no_new_input_is_required():
    """An optional-with-empty-default input that somehow became required would
    break every existing caller (register-kuma-monitors and any future one) the
    moment this pin is consumed."""
    action_yaml = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
    for name in ALL_OPTIONAL_INPUTS:
        assert action_yaml["inputs"][name].get("required", False) is False, name
