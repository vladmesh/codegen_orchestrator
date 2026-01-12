import pytest
import uuid
from unittest.mock import MagicMock

# We assume the service listens on Redis for commands.
# P1.4 Spec says:
# - Subscribe: worker:commands (deprecated?) OR standard pattern?
# The spec says:
# - Loop: XREAD -> Subprocess -> XADD (This is Wrapper, P1.2)
# - Worker Manager (P1.4) manages containers.
# It should likely listen to a queue to spawn workers, OR exposes an API?
# MIGRATION_PLAN says: "Container lifecycle (create, delete, status)".
# Usually Orchestrator calls WorkerManager via API or Queue.
# Let's assume for now we use Redis Queue `worker:manager:commands` or similar,
# OR we verify the internal logic if we can import it.
# But for Service Test (black box), we need to trigger the service.
# Wait, MIGRATION_PLAN P1.1 Orchestrator CLI says "Orchestrator CLI -> Redis".
# But P1.4 says "Worker Manager" manages containers.
# Let's check how the system is supposed to work.
# Orchestrator CLI likely pushes a message to `worker:manager:queue` to spawn a worker?
# OR does it spawn it via API?
# The spec "Scenario A: Lifecycle" says "One Spawn: Send create command".
# Let's assume Redis Stream or PubSub.
# Let's use `worker:manager:commands` stream as a reasonable default for now.


@pytest.mark.asyncio
async def test_worker_lifecycle_happy_path(mock_docker_client):
    from src.manager import WorkerManager
    from redis.asyncio import Redis

    # Connect to Redis
    redis = Redis.from_url("redis://redis:6379/0", decode_responses=True)
    await redis.ping()

    # Mock DockerWrapper
    mock_wrapper = MagicMock()
    mock_container = MagicMock()
    mock_container.id = "test-container-id"
    mock_container.status = "running"

    # Configure async methods
    async def async_return(val):
        return val

    mock_wrapper.run_container.return_value = async_return(mock_container)
    mock_wrapper.remove_container.return_value = async_return(None)

    manager = WorkerManager(redis=redis, docker_client=mock_wrapper)

    # 2. Action: Create Worker
    worker_id = str(uuid.uuid4())
    await manager.create_worker(worker_id=worker_id, image="worker:latest")

    # 3. Assert: run_container called
    mock_wrapper.run_container.assert_called_once()

    # 4. Assert: Redis status updated
    status = await redis.get(f"worker:status:{worker_id}")
    assert status == "RUNNING"

    # 5. Action: Delete Worker
    await manager.delete_worker(worker_id)

    # 6. Assert: remove_container called
    mock_wrapper.remove_container.assert_called_once()

    # 7. Assert: Redis status updated
    status = await redis.get(f"worker:status:{worker_id}")
    assert status == "STOPPED"
