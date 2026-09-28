"""The backend fixture supplies the repository contract production requires."""

import subprocess

import pytest

from shared.contracts.queues.worker import AgentType, WorkerOwnership
from tests.integration.backend import conftest as backend


def test_scaffolded_repository_matches_worker_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "WORKSPACE_BASE_PATH", str(tmp_path))
    repo_id = backend._create_scaffolded_workspace()
    workspace = tmp_path / repo_id
    origin = subprocess.check_output(
        ["git", "-C", str(workspace), "remote", "get-url", "origin"], text=True
    ).strip()

    extra_env = {"FIXTURE_ASSERTION": "preserved"}
    config = backend.scaffolded_worker_config(
        repo_id,
        name="fixture-contract",
        worker_type="developer",
        agent_type=AgentType.CLAUDE,
        instructions="Keep these instructions.",
        allowed_commands=[],
        capabilities=[],
        ownership=WorkerOwnership(project_id="project", run_id="run", attempt_id="attempt"),
        env_vars=extra_env,
    )

    assert config.repo_id == repo_id
    assert origin == f"https://github.com/{config.env_vars['REPO_NAME']}.git"
    assert repo_id in config.env_vars["REPO_NAME"]
    assert repo_id in config.env_vars["GITHUB_TOKEN"]
    assert config.env_vars["FIXTURE_ASSERTION"] == "preserved"
    assert extra_env == {"FIXTURE_ASSERTION": "preserved"}
    assert config.env_vars["GITHUB_TOKEN"] not in (workspace / ".git/config").read_text()
    assert subprocess.check_output(["git", "-C", str(workspace), "rev-parse", "HEAD"])


@pytest.mark.parametrize("key", ["REPO_NAME", "GITHUB_TOKEN"])
def test_scaffolded_config_refuses_to_normalize_invalid_credentials(key):
    with pytest.raises(ValueError, match="Use WorkerConfig directly for invalid repository input"):
        backend.scaffolded_worker_config("repository", env_vars={key: ""})
