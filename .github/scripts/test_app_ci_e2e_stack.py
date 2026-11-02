from pathlib import Path

import yaml


WORKFLOW = Path(__file__).parents[1] / "workflows" / "app-ci.yml"
LABELS = (
    "ci.e2e.run=${{ github.run_id }}",
    "ci.e2e.attempt=${{ github.run_attempt }}",
    "ci.e2e.runner=${{ runner.name }}",
)


def test_e2e_creations_are_labelled_and_stack_stops_before_uploads():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["e2e"]["steps"]
    by_name = {step.get("name"): (index, step) for index, step in enumerate(steps)}

    creations = []
    for name in (
        "Build full app image",
        "Create E2E network and dependencies",
        "Start app",
    ):
        lines = by_name[name][1]["run"].splitlines()
        index = 0
        while index < len(lines):
            line = lines[index].strip()
            if line.startswith(("DOCKER_BUILDKIT=1 docker build", "docker network create", "docker run -d")):
                parts = [line]
                while parts[-1].endswith("\\") and index + 1 < len(lines):
                    index += 1
                    parts.append(lines[index].strip())
                creations.append((name, " ".join(parts)))
            index += 1
    assert len(creations) == 5, creations
    for name, command in creations:
        for label in LABELS:
            assert label in command, f"{name} creation command is missing {label}: {command}"

    stop_index, stop = by_name["Stop E2E stack"]
    capture_index = by_name["Capture E2E screens and report"][0]
    failure_upload_index = by_name["Upload E2E failure artifacts"][0]
    capture_upload_index = by_name["Upload E2E capture artifact"][0]
    cleanup_index, cleanup = by_name["Cleanup E2E containers, network, and image"]
    assert stop.get("if") == "always()"
    assert capture_index < stop_index < failure_upload_index < cleanup_index
    assert stop_index < capture_upload_index < cleanup_index

    for step in steps[stop_index + 1 : cleanup_index]:
        command = step.get("run", "")
        assert "docker logs" not in command
        assert not any(name in command for name in ("$E2E_APP_CONTAINER", "$E2E_DB_CONTAINER", "$E2E_REDIS_CONTAINER", "$E2E_NETWORK", "$CI_IMAGE"))
    repeat = cleanup["run"]
    assert "docker rm -f" in repeat and "docker network rm" in repeat and "docker image rm -f" in repeat
    assert "E2E_MAIL_HOST_DIR" in repeat
