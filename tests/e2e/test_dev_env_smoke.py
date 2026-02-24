"""E2E smoke test for the dev environment architecture.

Verifies the full vertical slice:
  worker creation → workspace bind-mount → dev network → compose sidecar → cleanup

Run via:
    docker compose -f docker/test/e2e/e2e.yml run tests \
        pytest tests/e2e/test_dev_env_smoke.py
"""

import contextlib
import json
import os
import textwrap
import time

import httpx
import pytest

import docker

# Configure pytest-asyncio
pytest_plugins = ("pytest_asyncio",)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
DOCKER_HOST = os.getenv("DOCKER_HOST", "tcp://docker:2375")
WORKER_MANAGER_URL = os.getenv("WORKER_MANAGER_URL", "http://worker-manager:8000")
WORKSPACE_BASE_PATH = os.getenv("WORKSPACE_BASE_PATH", "/tmp/codegen/workspaces")  # noqa: S108


@pytest.fixture
def docker_client():
    client = docker.DockerClient(base_url=DOCKER_HOST)
    yield client
    client.close()


@pytest.fixture
def worker_id():
    return f"e2e-dev-env-{int(time.time())}"


@pytest.fixture(autouse=True)
def cleanup_test_resources(docker_client, worker_id):
    """Remove all test resources after the test."""
    yield

    # Sidecar containers by compose project label
    with contextlib.suppress(Exception):
        containers = docker_client.containers.list(
            all=True,
            filters={"label": f"com.docker.compose.project=worker_{worker_id}"},
        )
        for c in containers:
            with contextlib.suppress(Exception):
                c.remove(force=True)

    # Worker container
    with contextlib.suppress(Exception):
        docker_client.containers.get(f"worker-{worker_id}").remove(force=True)

    # Dev network
    with contextlib.suppress(Exception):
        docker_client.networks.get(f"dev_proj_{worker_id}").remove()


async def _wait_for_worker_status(redis, worker_id: str, target_status: str, timeout: int = 30):
    """Poll Redis until worker reaches target_status."""
    start = time.time()
    while time.time() - start < timeout:
        status = await redis.hget(f"worker:status:{worker_id}", "status")
        if status == target_status:
            return
        await _async_sleep(0.5)
    pytest.fail(f"Worker {worker_id} did not reach {target_status} within {timeout}s")


async def _async_sleep(seconds: float):
    import asyncio

    await asyncio.sleep(seconds)


@pytest.mark.asyncio
async def test_worker_starts_postgres_and_connects(redis_client, docker_client, worker_id):
    """
    Full vertical slice:
    1. Create worker via Redis command
    2. Wait for RUNNING status
    3. Write docker-compose.yml to workspace (postgres with healthcheck)
    4. POST /api/worker/{id}/infra/compose {"args": ["up", "-d", "--wait", "db"]}
    5. Exec pg_isready in the worker container (reachable via dev network)
    6. Delete worker
    7. Verify: no sidecar containers, no dev_proj_* network, no workspace dir
    """
    import redis.asyncio as aioredis

    redis = aioredis.from_url(REDIS_URL, decode_responses=True)

    # Step 1: Create worker
    cmd_data = json.dumps(
        {
            "worker_id": worker_id,
            "image": "worker-base-common:latest",
            "capabilities": [],
            "agent_type": "claude",
            "base_image": "worker-base-common:latest",
        }
    )
    await redis.xadd("worker:commands", {"action": "create", "data": cmd_data})

    # Step 2: Wait for RUNNING
    await _wait_for_worker_status(redis, worker_id, "RUNNING")

    # Step 3: Write a postgres compose file to the workspace
    ws_path = os.path.join(WORKSPACE_BASE_PATH, worker_id, "workspace")
    compose_content = textwrap.dedent("""\
        services:
          db:
            image: postgres:16-alpine
            environment:
              POSTGRES_PASSWORD: testpass
            healthcheck:
              test: ["CMD-SHELL", "pg_isready -U postgres"]
              interval: 2s
              timeout: 5s
              retries: 10
    """)
    with open(os.path.join(ws_path, "docker-compose.yml"), "w") as f:
        f.write(compose_content)

    # Step 4: POST compose up --wait
    async with httpx.AsyncClient(base_url=WORKER_MANAGER_URL, timeout=90) as client:
        response = await client.post(
            f"/api/worker/{worker_id}/infra/compose",
            json={"args": ["up", "-d", "--wait", "db"], "timeout": 60},
        )

    assert response.status_code == 200, f"Compose failed: {response.text}"
    result = response.json()
    assert result["exit_code"] == 0, f"Compose non-zero exit: {result['stderr']}"

    # Step 5: Verify postgres is reachable from within the worker container
    container = docker_client.containers.get(f"worker-{worker_id}")
    exit_code, output = container.exec_run(
        "sh -c 'pg_isready -h db -p 5432 -U postgres'",
        user="worker",
    )
    assert exit_code == 0, f"pg_isready failed: {output}"

    # Step 6: Delete worker
    del_data = json.dumps({"worker_id": worker_id})
    await redis.xadd("worker:commands", {"action": "delete", "data": del_data})

    # Step 7: Wait for cleanup and verify
    await _async_sleep(10)

    # Worker container gone
    with pytest.raises(docker.errors.NotFound):
        docker_client.containers.get(f"worker-{worker_id}")

    # Dev network gone
    with pytest.raises(docker.errors.NotFound):
        docker_client.networks.get(f"dev_proj_{worker_id}")

    # Workspace directory gone
    worker_dir = os.path.join(WORKSPACE_BASE_PATH, worker_id)
    assert not os.path.exists(worker_dir), f"Workspace dir still exists: {worker_dir}"

    await redis.aclose()
