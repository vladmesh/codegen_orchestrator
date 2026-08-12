"""Integration tests for the dev environment architecture.

Tests workspace bind-mount, compose proxy, and cleanup via worker-manager.
Runs in DinD environment (DOCKER_HOST pointing to a DinD daemon).
"""

import os
import time
from uuid import uuid4

import httpx
import pytest

from shared.contracts.queues.worker import (
    AgentType,
    CreateWorkerCommand,
    DeleteWorkerCommand,
    WorkerConfig,
)

from .conftest import wait_for_create_response

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
DOCKER_HOST = os.getenv("DOCKER_HOST", "tcp://docker:2375")
WORKER_MANAGER_URL = os.getenv("WORKER_MANAGER_URL", "http://worker-manager:8000")

REDIS_STREAM_COMMANDS = "worker:commands"
REDIS_STREAM_DEV_RESPONSES = "worker:responses:developer"


@pytest.mark.integration
@pytest.mark.asyncio
class TestDevEnvIntegration:
    async def test_workspace_bind_mount(self, redis_client, docker_client, scaffolded_workspace):
        """Create worker -> touch file in /workspace -> verify via docker exec."""
        req_id = f"dev-env-{uuid4().hex[:6]}"
        worker_name = f"test-ws-mount-{req_id}"

        cmd = CreateWorkerCommand(
            request_id=req_id,
            config=WorkerConfig(
                name=worker_name,
                worker_type="developer",
                agent_type=AgentType.CLAUDE,
                instructions="Test workspace",
                allowed_commands=[],
                capabilities=[],
                repo_id=scaffolded_workspace,
            ),
        )
        await redis_client.xadd(REDIS_STREAM_COMMANDS, {"data": cmd.model_dump_json()})

        result = await wait_for_create_response(redis_client, REDIS_STREAM_DEV_RESPONSES, req_id)
        assert result.success, f"Worker creation failed: {result.error}"

        container = docker_client.containers.get(f"worker-{worker_name}")

        container.reload()
        host_config = container.attrs["HostConfig"]
        assert host_config["CapDrop"] == ["ALL"]
        assert host_config["SecurityOpt"] == ["no-new-privileges:true"]

        # The hardened worker user must write both paths prepared before launch.
        exit_code, output = container.exec_run(
            "touch /workspace/test.txt /artifacts/worker-transcripts/test.jsonl"
        )
        assert exit_code == 0, f"touch failed: {output.decode()}"

        # Verify file exists (proves workspace is writable and mounted)
        exit_code, output = container.exec_run("ls /workspace/test.txt")
        assert exit_code == 0, f"File not found: {output.decode()}"

        exit_code, output = container.exec_run("ls /artifacts/worker-transcripts/test.jsonl")
        assert exit_code == 0, f"Transcript not found: {output.decode()}"

    async def test_compose_rejects_absolute_volumes(
        self, redis_client, docker_client, scaffolded_workspace
    ):
        """POST compose with absolute volume mounts should return 400."""
        req_id = f"dev-env-{uuid4().hex[:6]}"
        worker_name = f"test-vols-{req_id}"

        cmd = CreateWorkerCommand(
            request_id=req_id,
            config=WorkerConfig(
                name=worker_name,
                worker_type="developer",
                agent_type=AgentType.CLAUDE,
                instructions="Test compose",
                allowed_commands=[],
                capabilities=[],
                repo_id=scaffolded_workspace,
            ),
        )
        await redis_client.xadd(REDIS_STREAM_COMMANDS, {"data": cmd.model_dump_json()})

        result = await wait_for_create_response(redis_client, REDIS_STREAM_DEV_RESPONSES, req_id)
        assert result.success, f"Worker creation failed: {result.error}"

        # Write compose file with absolute volume mount inside the container
        container = docker_client.containers.get(f"worker-{worker_name}")
        compose_yml = (
            "services:\n"
            "  db:\n"
            "    image: postgres:16\n"
            "    volumes:\n"
            "      - /etc/passwd:/etc/passwd\n"
        )
        exit_code, _ = container.exec_run(
            [
                "sh",
                "-c",
                f"cat > /workspace/docker-compose.yml << 'EOFCOMPOSE'\n{compose_yml}EOFCOMPOSE",
            ]
        )
        assert exit_code == 0

        # Call through the authenticated broker, exactly as the localhost
        # worker-wrapper proxy does. The worker token is read from the real
        # DinD-launched container, never invented by the test.
        container.reload()
        worker_env = dict(
            item.split("=", 1) for item in container.attrs["Config"]["Env"] if "=" in item
        )
        broker_url = worker_env["WORKER_BROKER_URL"]
        broker_token = worker_env["WORKER_BROKER_TOKEN"]
        async with httpx.AsyncClient(
            base_url=broker_url,
            headers={"X-Worker-Broker-Token": broker_token},
        ) as client:
            response = await client.post(
                f"/v1/workers/{worker_name}/infra/compose",
                json={"args": ["-f", "docker-compose.yml", "up", "-d"]},
            )

        assert response.status_code == 400
        assert "absolute" in response.json()["detail"].lower()

    async def test_delete_cleans_everything(
        self, redis_client, docker_client, scaffolded_workspace
    ):
        """Create worker -> delete -> verify container gone."""
        req_id = f"dev-env-{uuid4().hex[:6]}"
        worker_name = f"test-del-{req_id}"

        cmd = CreateWorkerCommand(
            request_id=req_id,
            config=WorkerConfig(
                name=worker_name,
                worker_type="developer",
                agent_type=AgentType.CLAUDE,
                instructions="Test delete",
                allowed_commands=[],
                capabilities=[],
                repo_id=scaffolded_workspace,
            ),
        )
        await redis_client.xadd(REDIS_STREAM_COMMANDS, {"data": cmd.model_dump_json()})

        result = await wait_for_create_response(redis_client, REDIS_STREAM_DEV_RESPONSES, req_id)
        assert result.success, f"Worker creation failed: {result.error}"

        # Verify container exists
        container = docker_client.containers.get(f"worker-{worker_name}")
        assert container is not None

        # Delete worker
        del_req_id = f"del-{uuid4().hex[:6]}"
        del_cmd = DeleteWorkerCommand(request_id=del_req_id, worker_id=worker_name)
        await redis_client.xadd(REDIS_STREAM_COMMANDS, {"data": del_cmd.model_dump_json()})

        # Wait for deletion
        import docker as docker_lib

        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                docker_client.containers.get(f"worker-{worker_name}")
                time.sleep(1)
            except docker_lib.errors.NotFound:
                break
        else:
            pytest.fail("Container still exists after delete")
