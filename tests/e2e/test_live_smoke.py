"""Live smoke test: Worker spawn lifecycle against running docker compose stack.

Verifies:
1. API, Redis, worker-manager health
2. Worker creation via Redis command stream
3. Container appears on Docker host
4. Redis status keys set correctly
5. Worker deletion and cleanup

Run: make test-smoke
"""

import asyncio
import contextlib
import json
import time
from uuid import uuid4

import httpx
import pytest
import redis.asyncio as aioredis

import docker
from shared.contracts.queues.worker import (
    AgentType,
    CreateWorkerCommand,
    CreateWorkerResponse,
    DeleteWorkerCommand,
    WorkerCapability,
    WorkerChannels,
    WorkerConfig,
)

# Service addresses inside codegen_internal network
API_URL = "http://api:8000"
WORKER_MANAGER_URL = "http://worker-manager:8000"
REDIS_URL = "redis://redis:6379/0"
DOCKER_SOCKET = "unix:///var/run/docker.sock"

WORKER_COMMANDS = WorkerChannels.COMMANDS.value
WORKER_RESPONSES = "worker:responses:developer"


# --- Helpers ---


async def wait_for_response(
    redis_client: aioredis.Redis,
    stream: str,
    request_id: str,
    timeout: int = 120,
) -> CreateWorkerResponse:
    """Wait for CreateWorkerResponse matching request_id."""
    start = time.time()
    current_id = "0"
    while time.time() - start < timeout:
        messages = await redis_client.xread({stream: current_id}, count=1, block=1000)
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

    raise TimeoutError(f"No response for {request_id} on {stream} within {timeout}s")


# --- Fixtures ---


@pytest.fixture
async def redis_client():
    client = aioredis.from_url(REDIS_URL, decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def docker_client():
    client = docker.DockerClient(base_url=DOCKER_SOCKET)
    yield client
    client.close()


@pytest.fixture(autouse=True)
async def cleanup_response_stream(redis_client):
    """Clean response stream before/after test to avoid stale messages."""
    with contextlib.suppress(Exception):
        await redis_client.delete(WORKER_RESPONSES)
    yield
    with contextlib.suppress(Exception):
        await redis_client.delete(WORKER_RESPONSES)


# --- Tests ---


class TestHealthChecks:
    """Phase 1: All services are alive."""

    @pytest.mark.asyncio
    async def test_api_health(self):
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{API_URL}/health", timeout=5)
            assert resp.status_code == 200
            assert resp.json()["status"] == "ok"

    @pytest.mark.asyncio
    async def test_redis_ping(self, redis_client):
        assert await redis_client.ping()

    @pytest.mark.asyncio
    async def test_worker_manager_health(self):
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{WORKER_MANAGER_URL}/health", timeout=5)
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_worker_manager_consumer_group_exists(self, redis_client):
        """worker-manager consumer group on worker:commands should exist."""
        groups = await redis_client.xinfo_groups(WORKER_COMMANDS)
        group_names = [g["name"] for g in groups]
        assert "worker_manager" in group_names


class TestWorkerLifecycle:
    """Phase 2: Full worker create → verify → delete → cleanup cycle."""

    @pytest.mark.asyncio
    async def test_worker_spawn_and_delete(self, redis_client, docker_client):
        worker_name = f"smoke-{uuid4().hex[:6]}"
        req_id = f"smoke-req-{uuid4().hex[:6]}"

        # 1. Send CreateWorkerCommand
        command = CreateWorkerCommand(
            request_id=req_id,
            config=WorkerConfig(
                name=worker_name,
                worker_type="developer",
                agent_type=AgentType.CLAUDE,
                instructions="Smoke test worker. Do nothing.",
                allowed_commands=[],
                capabilities=[WorkerCapability.GIT],
            ),
        )
        await redis_client.xadd(WORKER_COMMANDS, {"data": command.model_dump_json()})

        # 2. Wait for creation response
        result = await wait_for_response(redis_client, WORKER_RESPONSES, req_id, timeout=120)
        assert result.success, f"Worker creation failed: {result.error}"
        assert result.worker_id is not None
        worker_id = result.worker_id

        try:
            # 3. Verify container exists and is running
            container = docker_client.containers.get(f"worker-{worker_id}")
            assert container.status == "running", f"Container status: {container.status}"

            # 4. Verify git is installed
            exit_code, output = container.exec_run("git --version")
            assert exit_code == 0, f"git not available: {output}"

            # 5. Verify CLAUDE.md injected
            exit_code, output = container.exec_run("cat /workspace/CLAUDE.md")
            assert exit_code == 0, "CLAUDE.md not found"
            assert b"Smoke test worker" in output

            # 6. Verify Redis status key
            status = await redis_client.hget(f"worker:status:{worker_id}", "status")
            assert status == "RUNNING", f"Redis status: {status}"

            # 7. Verify worker metadata in Redis
            meta = await redis_client.hgetall(f"worker:meta:{worker_id}")
            assert "dev_network" in meta, f"Missing dev_network in meta: {meta}"

        finally:
            # 8. Delete worker
            del_req_id = f"smoke-del-{uuid4().hex[:6]}"
            del_command = DeleteWorkerCommand(
                request_id=del_req_id,
                worker_id=worker_id,
            )
            await redis_client.xadd(WORKER_COMMANDS, {"data": del_command.model_dump_json()})

            # Wait for container to disappear (compose down + remove can take ~60s)
            container_gone = False
            for _ in range(90):
                try:
                    docker_client.containers.get(f"worker-{worker_id}")
                except docker.errors.NotFound:
                    container_gone = True
                    break
                await asyncio.sleep(1)

        # 9. Verify cleanup
        assert container_gone, "Container still exists after 90s delete timeout"

        # Redis status should be cleaned up
        status_after = await redis_client.hget(f"worker:status:{worker_id}", "status")
        assert status_after in (None, "STOPPED"), f"Status after delete: {status_after}"
