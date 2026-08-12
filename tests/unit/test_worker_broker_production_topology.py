"""Regression guard for the production worker-network boundary."""

import json
from pathlib import Path
import re
import subprocess

REPO_ROOT = Path(__file__).parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"


def _resolved_production_compose(tmp_path: Path) -> dict:
    """Resolve the deployment compose source without reading a developer .env file."""
    source = COMPOSE_FILE.read_text()
    variables = set(re.findall(r"\$\{([A-Z0-9_]+)(?::[^}]*)?\}", source))
    host_paths = {
        "GITHUB_APP_PEM_PATH": str(tmp_path / "github-app.pem"),
        "HOST_CLAUDE_DIR": str(tmp_path / "claude"),
        "HOST_CODEX_HOME": str(tmp_path / "codex"),
        "WORKER_TRANSCRIPT_HOST_PATH": str(tmp_path / "worker-transcripts"),
        "WORKSPACE_HOST_PATH": str(tmp_path / "workspaces"),
    }
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            f"{name}={host_paths.get(name, f'test-{name.lower()}')}" for name in sorted(variables)
        )
        + "\n"
    )
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--project-directory",
            str(tmp_path),
            "--env-file",
            str(env_file),
            "-f",
            str(COMPOSE_FILE),
            "config",
            "--format",
            "json",
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_resolved_production_worker_network_is_broker_only(tmp_path):
    compose = _resolved_production_compose(tmp_path)
    services = compose["services"]

    assert set(services["worker-broker"]["networks"]) == {"internal", "worker", "qa_egress"}
    assert "worker" not in services["redis"]["networks"]
    assert "worker" not in services["api"]["networks"]
    assert "worker" not in services["worker-manager"]["networks"]


def test_the_qa_executor_network_has_no_route_off_itself(tmp_path):
    """The whole QA write guarantee is this flag.

    A QA executor container is attached to this network and to nothing else, so
    if it were an ordinary bridge the container would have the deployment's
    public URL, the fleet and the internet — and "QA does not write to the
    application" would again be a sentence in a prompt.
    """
    compose = _resolved_production_compose(tmp_path)

    qa_egress = compose["networks"]["qa_egress"]
    assert qa_egress["internal"] is True
    assert qa_egress["name"] == "codegen_qa_egress"


def test_only_the_qa_runtime_and_the_broker_are_reachable_from_it(tmp_path):
    """Everything else on the platform stays off the executor's network."""
    compose = _resolved_production_compose(tmp_path)
    services = compose["services"]

    on_qa_network = {
        name for name, service in services.items() if "qa_egress" in (service.get("networks") or {})
    }
    assert on_qa_network == {"qa-worker", "worker-broker"}
    # qa-worker serves the run's capability endpoint; it must not be reachable
    # from a QA executor by any other route than that network.
    assert set(services["qa-worker"]["networks"]) == {"internal", "qa_egress"}
