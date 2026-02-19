"""Integration tests for the dev environment architecture.

Requires a DinD environment (DOCKER_HOST pointing to a DinD daemon).
Run via:
    docker compose -f docker/test/integration/backend.yml run tests \
        pytest tests/integration/backend/test_dev_env.py
"""

import contextlib
import json
import os
import time

import httpx
import pytest

import docker

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
    return f"test-dev-env-{int(time.time())}"


@pytest.fixture(autouse=True)
def cleanup_test_resources(docker_client, worker_id):
    """Clean up containers and networks after each test."""
    yield
    # Remove worker container
    with contextlib.suppress(Exception):
        c = docker_client.containers.get(f"worker-{worker_id}")
        c.remove(force=True)

    # Remove dev network
    with contextlib.suppress(Exception):
        net = docker_client.networks.get(f"dev_proj_{worker_id}")
        net.remove()

    # Remove any sidecar containers with project label
    with contextlib.suppress(Exception):
        containers = docker_client.containers.list(
            all=True,
            filters={"label": f"com.docker.compose.project=worker_{worker_id}"},
        )
        for c in containers:
            c.remove(force=True)


@pytest.mark.integration
@pytest.mark.asyncio
class TestDevEnvIntegration:
    async def test_workspace_bind_mount(self, docker_client, worker_id):
        """Create worker → touch /workspace/test.txt → verify file on host path."""
        import redis.asyncio as aioredis

        redis = aioredis.from_url(REDIS_URL, decode_responses=True)

        cmd_data = json.dumps(
            {
                "worker_id": worker_id,
                "image": "alpine:latest",
                "capabilities": [],
                "agent_type": "claude",
                "base_image": "alpine:latest",
            }
        )
        await redis.xadd("worker:commands", {"action": "create", "data": cmd_data})

        # Wait for worker to start
        timeout = 30
        start = time.time()
        while time.time() - start < timeout:
            status = await redis.hget(f"worker:status:{worker_id}", "status")
            if status == "RUNNING":
                break
            time.sleep(0.5)
        else:
            pytest.fail(f"Worker {worker_id} did not start within {timeout}s")

        # Touch a file in /workspace inside the container
        container = docker_client.containers.get(f"worker-{worker_id}")
        exit_code, output = container.exec_run("touch /workspace/test.txt")
        assert exit_code == 0

        # Verify file on host path
        host_path = os.path.join(WORKSPACE_BASE_PATH, worker_id, "workspace", "test.txt")
        assert os.path.exists(host_path), f"File not found on host: {host_path}"

        await redis.aclose()

    async def test_dual_network_created(self, docker_client, worker_id):
        """After creating a worker, the container should be attached to 2 networks."""
        import redis.asyncio as aioredis

        redis = aioredis.from_url(REDIS_URL, decode_responses=True)

        cmd_data = json.dumps(
            {
                "worker_id": worker_id,
                "image": "alpine:latest",
                "capabilities": [],
                "agent_type": "claude",
                "base_image": "alpine:latest",
            }
        )
        await redis.xadd("worker:commands", {"action": "create", "data": cmd_data})

        # Wait for RUNNING
        timeout = 30
        start = time.time()
        while time.time() - start < timeout:
            status = await redis.hget(f"worker:status:{worker_id}", "status")
            if status == "RUNNING":
                break
            time.sleep(0.5)
        else:
            pytest.fail("Worker did not start")

        container = docker_client.containers.get(f"worker-{worker_id}")
        container.reload()
        networks = list(container.attrs["NetworkSettings"]["Networks"].keys())
        assert len(networks) >= 2, f"Expected 2 networks, got: {networks}"

        # One network should be the dev network
        dev_net = f"dev_proj_{worker_id}"
        assert dev_net in networks, f"dev network {dev_net} not found in {networks}"

        await redis.aclose()

    async def test_compose_rejects_ports(self, docker_client, worker_id):
        """POST compose with ports in compose.yml should return 400."""
        # Write a compose file with ports into the workspace
        ws_path = os.path.join(WORKSPACE_BASE_PATH, worker_id, "workspace")
        os.makedirs(ws_path, exist_ok=True)

        with open(os.path.join(ws_path, "docker-compose.yml"), "w") as f:
            f.write("services:\n  db:\n    image: postgres:16\n    ports:\n      - '5432:5432'\n")

        async with httpx.AsyncClient(base_url=WORKER_MANAGER_URL) as client:
            response = await client.post(
                f"/api/worker/{worker_id}/infra/compose",
                json={"args": ["up", "-d"]},
            )

        assert response.status_code == 400
        assert "ports" in response.json()["detail"].lower()

    async def test_delete_cleans_everything(self, docker_client, worker_id):
        """Create worker + sidecar → delete → verify: no containers, no network, no workspace."""
        import redis.asyncio as aioredis

        redis = aioredis.from_url(REDIS_URL, decode_responses=True)

        cmd_data = json.dumps(
            {
                "worker_id": worker_id,
                "image": "alpine:latest",
                "capabilities": [],
                "agent_type": "claude",
                "base_image": "alpine:latest",
            }
        )
        await redis.xadd("worker:commands", {"action": "create", "data": cmd_data})

        # Wait for RUNNING
        timeout = 30
        start = time.time()
        while time.time() - start < timeout:
            status = await redis.hget(f"worker:status:{worker_id}", "status")
            if status == "RUNNING":
                break
            time.sleep(0.5)
        else:
            pytest.fail("Worker did not start")

        # Delete worker
        del_data = json.dumps({"worker_id": worker_id})
        await redis.xadd("worker:commands", {"action": "delete", "data": del_data})

        # Wait for cleanup (max 30s)
        time.sleep(5)

        # Container should be gone
        with pytest.raises(docker.errors.NotFound):
            docker_client.containers.get(f"worker-{worker_id}")

        # Dev network should be gone
        with pytest.raises(docker.errors.NotFound):
            docker_client.networks.get(f"dev_proj_{worker_id}")

        # Workspace dir should be gone
        ws_dir = os.path.join(WORKSPACE_BASE_PATH, worker_id)
        assert not os.path.exists(ws_dir), f"Workspace dir still exists: {ws_dir}"

        await redis.aclose()
