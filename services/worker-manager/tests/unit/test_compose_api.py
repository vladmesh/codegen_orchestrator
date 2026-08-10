"""Service tests for the compose HTTP API endpoint."""

import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
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
    runner.inspect = AsyncMock(
        return_value=(
            {
                "services": {
                    "db": {
                        "image": "postgres:16",
                        "networks": {"default": None},
                        "deploy": {"resources": {"limits": {"cpus": "1.0", "memory": "512M"}}},
                    }
                },
                "networks": {"default": {"name": "dev_proj_worker-123", "external": True}},
            },
            None,
        )
    )

    docker = MagicMock()
    docker.exec_in_container = AsyncMock(return_value=(0, b"services:\n  db:\n    image: postgres:16\n"))

    redis = AsyncMock()
    redis.hget = AsyncMock(return_value=None)
    redis.hgetall = AsyncMock(return_value={"token_digest": hashlib.sha256(b"broker-test-token").hexdigest()})

    app.state.compose_runner = runner
    app.state.docker = docker
    app.state.redis = redis
    with TestClient(app, raise_server_exceptions=True) as c:
        c.headers.update({"X-Worker-Broker-Token": "broker-test-token"})
        yield c, runner, redis


class TestComposeApi:
    def test_broker_authenticated_creation_uses_the_real_runner_plan(self, tmp_path):
        workspace = tmp_path / "project" / "workspace"
        infra = workspace / "infra"
        infra.mkdir(parents=True)
        (infra / "compose.base.yml").write_text("services:\n  db:\n    image: postgres:16\n")
        (infra / "compose.dev.yml").write_text("services:\n  db:\n    ports: ['5432:5432']\n")
        app = FastAPI(title="Test Worker Manager")
        app.include_router(compose_router)
        app.state.compose_runner = ComposeRunner(str(tmp_path))
        app.state.redis = AsyncMock()
        app.state.redis.hgetall = AsyncMock(
            return_value={"token_digest": hashlib.sha256(b"broker-test-token").hexdigest()}
        )
        app.state.redis.hget = AsyncMock(return_value=str(workspace))
        config = (
            '{"services":{"db":{"image":"postgres:16","networks":{"default":null},"ports":["5432:5432"]}},'
            '"networks":{"default":{"name":"dev_proj_worker-123","external":true}}}'
        )
        config_result = MagicMock(returncode=0, stdout=config, stderr="")
        execution_result = MagicMock(returncode=0, stdout="started\n", stderr="")

        with (
            TestClient(app, raise_server_exceptions=True) as c,
            patch("src.compose_runner.subprocess.run", side_effect=[config_result, execution_result]) as mock_run,
        ):
            response = c.post(
                "/api/worker/worker-123/infra/compose",
                json={"args": ["up", "-d"]},
                headers={"X-Worker-Broker-Token": "broker-test-token"},
            )

        assert response.status_code == 200
        assert mock_run.call_count == 2
        config_command = mock_run.call_args_list[0].args[0]
        assert "config" in config_command
        project_directory_index = config_command.index("--project-directory")
        assert config_command[project_directory_index + 1] == str(infra)
        assert "compose.resolved.yml" in " ".join(mock_run.call_args_list[1].args[0])

    def test_direct_request_without_broker_credential_is_rejected(self, client):
        c, _, _ = client
        response = c.post(
            "/api/worker/worker-123/infra/compose",
            json={"args": ["ps"]},
            headers={"X-Worker-Broker-Token": "wrong-token"},
        )
        assert response.status_code == 403

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
        """Runner policy failures are surfaced through the authenticated route."""
        c, runner, _redis = client
        runner.run = AsyncMock(side_effect=ValueError("Command 'exec' is not allowed"))
        response = c.post(
            "/api/worker/worker-123/infra/compose",
            json={"args": ["exec", "db", "bash"]},
        )
        assert response.status_code == 400
        assert "exec" in response.json()["detail"].lower()

    def test_interactive_flag_returns_400(self, client):
        """The router does not duplicate the runner argument policy."""
        c, runner, _redis = client
        runner.run = AsyncMock(side_effect=ValueError("Flag '-it' is not allowed"))
        response = c.post(
            "/api/worker/worker-123/infra/compose",
            json={"args": ["run", "-it", "db"]},
        )
        assert response.status_code == 400

    def test_run_scope_flag_never_reaches_runner(self, client):
        c, runner, _redis = client
        runner.run = AsyncMock(side_effect=ValueError("Flag '--volume=/:/host' is not allowed"))

        response = c.post(
            "/api/worker/worker-123/infra/compose",
            json={"args": ["run", "--volume=/:/host", "db"]},
        )

        assert response.status_code == 400
        runner.run.assert_awaited_once()

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

    def test_router_delegates_selected_source_and_cwd_to_runner(self, client):
        c, runner, _redis = client

        response = c.post(
            "/api/worker/worker-123/infra/compose",
            json={"args": ["-f", "compose.yml", "up", "-d"], "cwd": "infra"},
        )

        assert response.status_code == 200
        assert runner.run.call_args.kwargs["cwd"] == "infra"

    def test_source_failure_from_runner_returns_400(self, client):
        c, runner, _redis = client
        runner.run = AsyncMock(side_effect=ValueError("Compose source cannot be resolved"))

        response = c.post(
            "/api/worker/worker-123/infra/compose",
            json={"args": ["ps"]},
        )

        assert response.status_code == 400
        runner.run.assert_awaited_once()

    def test_effective_policy_failure_from_runner_returns_400(self, client):
        c, runner, _redis = client
        runner.run = AsyncMock(side_effect=ValueError("Service 'db': privileged is not allowed"))

        response = c.post(
            "/api/worker/worker-123/infra/compose",
            json={"args": ["up", "-d"]},
        )

        assert response.status_code == 400
        assert "privileged" in response.json()["detail"]
        runner.run.assert_awaited_once()
