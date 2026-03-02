"""Service tests for the compose HTTP API endpoint."""

import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.routers.compose import router as compose_router
from src.compose_runner import ComposeRunner


@pytest.fixture
def client(tmp_path):
    """Test client with a mocked compose runner and docker client in app state."""
    # Create an isolated test app without lifespan manager from main.py
    app = FastAPI(title="Test Worker Manager")
    app.include_router(compose_router)

    runner = MagicMock(spec=ComposeRunner)
    runner.run = AsyncMock(return_value=(0, "output\n", ""))

    docker = MagicMock()
    docker.exec_in_container = AsyncMock(side_effect=Exception("no container"))

    redis = AsyncMock()
    redis.hget = AsyncMock(return_value=None)

    app.state.compose_runner = runner
    app.state.docker = docker
    app.state.redis = redis
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c, runner, redis


class TestComposeApi:
    def test_valid_ps_returns_output(self, client, tmp_path):
        """A valid 'ps' command should return 200 with stdout/stderr."""
        c, runner, _redis = client
        runner.run = AsyncMock(return_value=(0, "container_list\n", ""))

        response = c.post(
            "/api/worker/worker-123/infra/compose",
            json={"args": ["ps"], "cwd": ".", "timeout": 30},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["exit_code"] == 0

    def test_blocked_command_returns_400(self, client):
        """Commands not in the whitelist should return 400."""
        c, _, _redis = client
        response = c.post(
            "/api/worker/worker-123/infra/compose",
            json={"args": ["exec", "db", "bash"]},
        )
        assert response.status_code == 400
        assert "exec" in response.json()["detail"].lower()

    def test_interactive_flag_returns_400(self, client):
        """Interactive flags should return 400."""
        c, _, _redis = client
        response = c.post(
            "/api/worker/worker-123/infra/compose",
            json={"args": ["run", "-it", "db"]},
        )
        assert response.status_code == 400

    def test_nonzero_exit_code_still_returns_200(self, client):
        """Non-zero exit codes from compose should still return 200 with the exit code."""
        c, runner, _redis = client
        runner.run = AsyncMock(return_value=(1, "", "error: db not found\n"))

        response = c.post(
            "/api/worker/worker-123/infra/compose",
            json={"args": ["ps"]},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["exit_code"] == 1
        assert "error" in data["stderr"]

    def test_path_traversal_returns_400(self, client, tmp_path):
        """Path traversal in cwd should return 400."""
        c, runner, _redis = client
        # Make run() raise ValueError (as ComposeRunner does for traversal)
        runner.run = AsyncMock(side_effect=ValueError("Path traversal detected"))

        response = c.post(
            "/api/worker/worker-123/infra/compose",
            json={"args": ["ps"], "cwd": "../../etc"},
        )
        assert response.status_code == 400

    def test_workspace_resolved_from_redis_meta(self, client):
        """When Redis has workspace_path for worker, it should be passed to runner.run()."""
        c, runner, mock_redis = client
        mock_redis.hget = AsyncMock(return_value="/tmp/workspaces/project-uuid/workspace")
        runner.run = AsyncMock(return_value=(0, "ok\n", ""))

        response = c.post(
            "/api/worker/worker-123/infra/compose",
            json={"args": ["ps"]},
        )

        assert response.status_code == 200
        # Verify runner.run was called with workspace_dir from Redis
        call_kwargs = runner.run.call_args
        assert call_kwargs.kwargs.get("workspace_dir") == "/tmp/workspaces/project-uuid/workspace"
