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

    assert set(services["worker-broker"]["networks"]) == {"internal", "worker"}
    assert "worker" not in services["redis"]["networks"]
    assert "worker" not in services["api"]["networks"]
    assert "worker" not in services["worker-manager"]["networks"]
