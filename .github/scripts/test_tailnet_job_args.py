from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_runner_args_file_is_optional_and_supplied_to_both_tailnet_connect_joins():
    action = yaml.safe_load((ROOT / ".github/actions/tailnet-connect/action.yml").read_text(encoding="utf-8"))
    steps = action["runs"]["steps"]
    read = next(i for i, step in enumerate(steps) if step.get("id") == "ts_args")
    joins = [step for step in steps if str(step.get("uses", "")).startswith("tailscale/github-action")]
    assert read == 0
    assert "if [ -f /etc/tailnet-runner/job-up-args ]" in steps[read]["run"]
    assert len(joins) == 2
    assert all(step["with"]["args"] == "${{ steps.ts_args.outputs.args }}" for step in joins)


def test_deploy_app_reads_optional_args_before_join_and_passes_them_to_action():
    action = yaml.safe_load((ROOT / ".github/actions/deploy-app/action.yml").read_text(encoding="utf-8"))
    steps = action["runs"]["steps"]
    assert steps[0]["id"] == "ts_args"
    join = next(step for step in steps if step.get("name") == "🔗 Join Tailnet")
    assert join["with"]["args"] == "${{ steps.ts_args.outputs.args }}"


def test_all_nested_consumers_pin_v2_15_and_old_pin_is_gone():
    expected = {"backup", "deploy-traefik", "janitor", "maintenance", "register-kuma-monitors", "restic-check-local", "restore-dest-import", "restore", "sync-kuma-notifications"}
    found = set()
    for path in (ROOT / ".github/actions").glob("*/action.yml"):
        text = path.read_text(encoding="utf-8")
        if "tailnet-connect@v2.15.0" in text:
            found.add(path.parent.name)
        assert "tailnet-connect@v2.5.2" not in text
    assert found == expected
