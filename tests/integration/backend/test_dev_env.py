"""Integration tests for the dev environment architecture.

Tests workspace bind-mount, compose proxy, and cleanup via worker-manager.
Runs in DinD environment (DOCKER_HOST pointing to a DinD daemon).
"""

import json
import os
import time
from uuid import uuid4

import httpx
import pytest

from shared.contracts.queues.worker import (
    AgentType,
    CreateWorkerCommand,
    CreateWorkerResponse,
    DeleteWorkerCommand,
    WorkerConfig,
)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
DOCKER_HOST = os.getenv("DOCKER_HOST", "tcp://docker:2375")
WORKER_MANAGER_URL = os.getenv("WORKER_MANAGER_URL", "http://worker-manager:8000")

REDIS_STREAM_COMMANDS = "worker:commands"
REDIS_STREAM_DEV_RESPONSES = "worker:responses:developer"


async def wait_for_create_response(redis, request_id: str, timeout: int = 120):
    """Wait for CreateWorkerResponse matching request_id."""
    start = time.time()
    current_id = "0"
    while time.time() - start < timeout:
        messages = await redis.xread({REDIS_STREAM_DEV_RESPONSES: current_id}, count=1, block=1000)
        if not messages:
            continue
        msg_id = messages[0][1][0][0]
        fields = messages[0][1][0][1]
        current_id = msg_id
        data_str = fields.get("data")
        if not data_str:
            continue
        parsed = json.loads(data_str)
        if parsed.get("request_id") != request_id:
            continue
        return CreateWorkerResponse.model_validate(parsed)
    raise TimeoutError(f"No response for {request_id} within {timeout}s")


@pytest.mark.integration
@pytest.mark.asyncio
class TestDevEnvIntegration:
    async def test_workspace_bind_mount(self, redis_client, docker_client):
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
            ),
        )
        await redis_client.xadd(REDIS_STREAM_COMMANDS, {"data": cmd.model_dump_json()})

        result = await wait_for_create_response(redis_client, req_id)
        assert result.success, f"Worker creation failed: {result.error}"

        container = docker_client.containers.get(f"worker-{worker_name}")

        # Touch a file in /workspace inside the container
        exit_code, output = container.exec_run("touch /workspace/test.txt")
        assert exit_code == 0, f"touch failed: {output.decode()}"

        # Verify file exists (proves workspace is writable and mounted)
        exit_code, output = container.exec_run("ls /workspace/test.txt")
        assert exit_code == 0, f"File not found: {output.decode()}"

    async def test_compose_rejects_absolute_volumes(self, redis_client, docker_client):
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
            ),
        )
        await redis_client.xadd(REDIS_STREAM_COMMANDS, {"data": cmd.model_dump_json()})

        result = await wait_for_create_response(redis_client, req_id)
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

        # POST to compose proxy should reject absolute volume mounts
        async with httpx.AsyncClient(base_url=WORKER_MANAGER_URL) as client:
            response = await client.post(
                f"/api/worker/{worker_name}/infra/compose",
                json={"args": ["up", "-d"]},
            )

        assert response.status_code == 400
        assert "absolute" in response.json()["detail"].lower()

    async def test_delete_cleans_everything(self, redis_client, docker_client):
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
            ),
        )
        await redis_client.xadd(REDIS_STREAM_COMMANDS, {"data": cmd.model_dump_json()})

        result = await wait_for_create_response(redis_client, req_id)
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
