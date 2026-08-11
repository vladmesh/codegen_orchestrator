"""Service tests for the compose HTTP API endpoint."""

import asyncio
import hashlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from shared.contracts.vocab import WorkerType
from src.routers.compose import router as compose_router
from src.compose_runner import ComposeRunner
from src.compose_validator import RESOURCE_IDENTITY_POLICY

BROKER_TOKEN = "broker-test-token"


def server_records(
    worker_id: str = "worker-123",
    *,
    worker_type: WorkerType | str = WorkerType.DEVELOPER,
    workspace_path: str | None = None,
    token: str = BROKER_TOKEN,
):
    """The Redis records worker-manager itself writes when it creates a worker.

    The route authorizes on these, so the tests state them instead of stubbing
    the reads: a worker with no recorded type is a real case (it is what a
    forged worker id looks like) and it must be refused, not waved through.
    """
    redis = FakeAsyncRedis(decode_responses=True)

    async def seed():
        await redis.hset(
            f"worker:broker:{worker_id}",
            mapping={"token_digest": hashlib.sha256(token.encode()).hexdigest()},
        )
        meta = {}
        if worker_type is not None:
            meta["worker_type"] = getattr(worker_type, "value", worker_type)
        if workspace_path:
            meta["workspace_path"] = workspace_path
        if meta:
            await redis.hset(f"worker:meta:{worker_id}", mapping=meta)

    asyncio.run(seed())
    return redis


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

    redis = server_records()

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
        app.state.redis = server_records(workspace_path=str(workspace))
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

    def test_broker_api_replaces_worker_build_image_with_manager_identity(self, tmp_path):
        workspace = tmp_path / "project" / "workspace"
        infra = workspace / "infra"
        infra.mkdir(parents=True)
        (workspace / "Dockerfile").write_text("FROM scratch\n")
        (infra / "compose.base.yml").write_text(
            "services:\n  app:\n    image: codegen-orchestrator/victim:latest\n    build:\n      context: ..\n"
        )
        (infra / "compose.dev.yml").write_text("services: {}\n")
        app = FastAPI(title="Test Worker Manager")
        app.include_router(compose_router)
        app.state.compose_runner = ComposeRunner(str(tmp_path))
        app.state.redis = server_records(workspace_path=str(workspace))
        config = json.dumps(
            {
                "services": {
                    "app": {
                        "image": "codegen-orchestrator/victim:latest",
                        "build": {"context": str(workspace)},
                        "networks": {"default": None},
                        "deploy": {"resources": {"limits": {"cpus": "1.0", "memory": "512M"}}},
                    }
                },
                "networks": {"default": {"name": "dev_proj_worker-123", "external": True}},
            }
        )
        config_result = MagicMock(returncode=0, stdout=config, stderr="")
        execution_result = MagicMock(returncode=0, stdout="built\n", stderr="")

        with (
            TestClient(app, raise_server_exceptions=True) as c,
            patch("src.compose_runner.subprocess.run", side_effect=[config_result, execution_result]),
        ):
            response = c.post(
                "/api/worker/worker-123/infra/compose",
                json={"args": ["build"]},
                headers={"X-Worker-Broker-Token": "broker-test-token"},
            )

        assert response.status_code == 200
        snapshot = tmp_path / ".compose-plans" / "worker-123" / "compose.resolved.yml"
        assert RESOURCE_IDENTITY_POLICY.build_image("worker-123", "app") in snapshot.read_text()
        assert "codegen-orchestrator/victim:latest" not in snapshot.read_text()

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
        c, runner, redis = client
        asyncio.run(redis.hset("worker:meta:worker-123", "workspace_path", "/tmp/workspaces/project-uuid/workspace"))
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

    @pytest.mark.parametrize(
        ("env_file", "project_env"),
        [("${EVIL}", "EVIL=../../HOSTSECRET.env\n"), ("${HOME}/HOSTSECRET.env", None)],
    )
    def test_broker_api_rejects_interpolated_env_file_before_compose_config(self, tmp_path, env_file, project_env):
        workspace = tmp_path / "workspace"
        infra = workspace / "infra"
        infra.mkdir(parents=True)
        if project_env:
            (workspace / ".env").write_text(project_env)
        (infra / "compose.base.yml").write_text(f"services:\n  db:\n    image: postgres:16\n    env_file: {env_file}\n")
        app = FastAPI(title="Test Worker Manager")
        app.include_router(compose_router)
        app.state.compose_runner = ComposeRunner(str(tmp_path))
        app.state.redis = server_records(workspace_path=str(workspace))

        with (
            TestClient(app, raise_server_exceptions=True) as c,
            patch("src.compose_runner.subprocess.run") as mock_run,
        ):
            response = c.post(
                "/api/worker/worker-123/infra/compose",
                json={"args": ["-f", "infra/compose.base.yml", "up", "-d"]},
                headers={"X-Worker-Broker-Token": "broker-test-token"},
            )

        assert response.status_code == 400
        assert "interpolation" in response.json()["detail"]
        mock_run.assert_not_called()

    @pytest.mark.parametrize("label_file", ["/etc/passwd", "../../HOSTSECRET.env", "${HOME}/HOSTSECRET.env"])
    def test_broker_api_rejects_label_file_before_compose_or_error_reflection(self, tmp_path, label_file):
        workspace = tmp_path / "workspace"
        infra = workspace / "infra"
        infra.mkdir(parents=True)
        (infra / "compose.base.yml").write_text(
            f"services:\n  db:\n    image: postgres:16\n    label_file: {label_file}\n"
        )
        app = FastAPI(title="Test Worker Manager")
        app.include_router(compose_router)
        app.state.compose_runner = ComposeRunner(str(tmp_path))
        app.state.redis = server_records(workspace_path=str(workspace))

        with (
            TestClient(app, raise_server_exceptions=True) as c,
            patch("src.compose_runner.subprocess.run") as mock_run,
        ):
            response = c.post(
                "/api/worker/worker-123/infra/compose",
                json={"args": ["-f", "infra/compose.base.yml", "up", "-d"]},
                headers={"X-Worker-Broker-Token": "broker-test-token"},
            )

        assert response.status_code == 400
        assert response.json()["detail"] == "Service 'db': label_file is not supported"
        mock_run.assert_not_called()
        assert not (tmp_path / ".compose-plans" / "worker-123" / "compose.resolved.yml").exists()

    @pytest.mark.parametrize(
        ("build_key", "cache_value"),
        [
            ("cache_from", "type=local,src=/etc"),
            ("cache_to", "type=local,dest=/manager-owned-path"),
        ],
    )
    def test_broker_api_rejects_build_cache_before_compose_config(self, tmp_path, build_key, cache_value):
        workspace = tmp_path / "workspace"
        infra = workspace / "infra"
        infra.mkdir(parents=True)
        (infra / "compose.base.yml").write_text(
            f"services:\n  db:\n    build:\n      context: ..\n      {build_key}:\n        - {cache_value}\n"
        )
        app = FastAPI(title="Test Worker Manager")
        app.include_router(compose_router)
        app.state.compose_runner = ComposeRunner(str(tmp_path))
        app.state.redis = server_records(workspace_path=str(workspace))

        with (
            TestClient(app, raise_server_exceptions=True) as c,
            patch("src.compose_runner.subprocess.run") as mock_run,
        ):
            response = c.post(
                "/api/worker/worker-123/infra/compose",
                json={"args": ["-f", "infra/compose.base.yml", "up", "-d"]},
                headers={"X-Worker-Broker-Token": "broker-test-token"},
            )

        assert response.status_code == 400
        assert response.json()["detail"] == f"Service 'db': build {build_key} is not supported"
        mock_run.assert_not_called()
        assert not (tmp_path / ".compose-plans" / "worker-123" / "compose.resolved.yml").exists()

    def test_broker_api_rejects_daemon_global_resource_identity_before_compose_config(self, tmp_path):
        workspace = tmp_path / "workspace"
        infra = workspace / "infra"
        infra.mkdir(parents=True)
        (infra / "compose.base.yml").write_text(
            "services:\n  db:\n    image: postgres:16\n    container_name: worker-manager\n"
        )
        app = FastAPI(title="Test Worker Manager")
        app.include_router(compose_router)
        app.state.compose_runner = ComposeRunner(str(tmp_path))
        app.state.redis = server_records(workspace_path=str(workspace))

        with (
            TestClient(app, raise_server_exceptions=True) as c,
            patch("src.compose_runner.subprocess.run") as mock_run,
        ):
            response = c.post(
                "/api/worker/worker-123/infra/compose",
                json={"args": ["-f", "infra/compose.base.yml", "up", "-d"]},
                headers={"X-Worker-Broker-Token": "broker-test-token"},
            )

        assert response.status_code == 400
        assert "container_name" in response.json()["detail"]
        mock_run.assert_not_called()


class TestQaWorkerHasNoComposeAuthority:
    """The second of the two boundaries that refuse Compose to a QA executor.

    The broker refuses it first. This one exists because the token is readable
    by the agent (`/proc/<ppid>/environ` of its wrapper), so the caller here may
    have skipped the broker entirely — a boundary that only the well-behaved
    path passes through is not a boundary.
    """

    def _app(self, redis):
        app = FastAPI(title="Test Worker Manager")
        app.include_router(compose_router)
        runner = MagicMock(spec=ComposeRunner)
        runner.run = AsyncMock(return_value=(0, "should never run\n", ""))
        app.state.compose_runner = runner
        app.state.redis = redis
        return app, runner

    @pytest.mark.parametrize("args", [["build"], ["up", "-d"], ["ps"]])
    def test_a_qa_worker_is_refused_with_its_own_valid_token(self, args):
        """Authenticated, and still refused — including for a read-only `ps`.

        The command does not matter: what is denied is the operation, so no
        future argument-level judgement can reopen it.
        """
        app, runner = self._app(server_records(worker_type=WorkerType.QA, workspace_path="/workspace"))

        with TestClient(app, raise_server_exceptions=True) as c:
            response = c.post(
                "/api/worker/worker-123/infra/compose",
                json={"args": args},
                headers={"X-Worker-Broker-Token": BROKER_TOKEN},
            )

        assert response.status_code == 403
        assert response.json()["detail"] == "a qa worker may not call infra.compose"
        runner.run.assert_not_awaited()

    def test_a_worker_whose_type_was_never_recorded_is_refused(self):
        """Fail closed. The record is written before the credential exists."""
        app, runner = self._app(server_records(worker_type=None))

        with TestClient(app, raise_server_exceptions=True) as c:
            response = c.post(
                "/api/worker/worker-123/infra/compose",
                json={"args": ["build"]},
                headers={"X-Worker-Broker-Token": BROKER_TOKEN},
            )

        assert response.status_code == 403
        assert response.json()["detail"] == "worker type is not recorded for this worker"
        runner.run.assert_not_awaited()

    def test_the_request_cannot_talk_its_way_into_being_a_developer(self):
        """The type is read from the server's record, never from the request."""
        app, runner = self._app(server_records(worker_type=WorkerType.QA, workspace_path="/workspace"))

        with TestClient(app, raise_server_exceptions=True) as c:
            response = c.post(
                "/api/worker/worker-123/infra/compose",
                json={"args": ["build"], "worker_type": "developer"},
                headers={"X-Worker-Broker-Token": BROKER_TOKEN},
            )

        assert response.status_code == 403
        runner.run.assert_not_awaited()

    def test_a_developer_worker_still_reaches_the_runner(self):
        """The control: the boundary must not break the ordinary pipeline."""
        app, runner = self._app(server_records(worker_type=WorkerType.DEVELOPER, workspace_path="/workspace"))

        with TestClient(app, raise_server_exceptions=True) as c:
            response = c.post(
                "/api/worker/worker-123/infra/compose",
                json={"args": ["build"]},
                headers={"X-Worker-Broker-Token": BROKER_TOKEN},
            )

        assert response.status_code == 200
        runner.run.assert_awaited_once()
