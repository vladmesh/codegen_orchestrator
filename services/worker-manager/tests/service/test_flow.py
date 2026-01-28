import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch, AsyncMock
from fakeredis import aioredis

from src.manager import WorkerManager


@pytest.fixture(autouse=True)
def patch_settings(worker_settings):
    """Patch settings for all tests in this module."""
    with patch("src.manager.settings", worker_settings):
        yield


@pytest.mark.asyncio
async def test_worker_lifecycle_flow(mock_docker_client, worker_settings):
    """
    Test the full lifecycle: Create -> Status -> Pause -> Resume -> Delete
    """
    # Setup
    redis = aioredis.FakeRedis(decode_responses=True)
    manager = WorkerManager(redis=redis, docker_client=mock_docker_client)

    worker_id = "test-life-1"
    image = "python:3.12-slim"

    # Mock run_container to return an object with an ID
    mock_container = MagicMock()
    mock_container.id = "container-123"

    # Configure AsyncMocks
    mock_docker_client.run_container = AsyncMock(return_value=mock_container)
    mock_docker_client.pause_container = AsyncMock()
    mock_docker_client.unpause_container = AsyncMock()
    mock_docker_client.remove_container = AsyncMock()
    mock_docker_client.image_exists = AsyncMock(return_value=True)  # Default to exists for this test
    mock_docker_client.pull_image = AsyncMock()

    # We expect ensure_image to be called
    # For this test, image exists
    container_id = await manager.create_worker(worker_id, image)

    assert container_id == "container-123"
    status = await manager.get_worker_status(worker_id)
    assert status == "RUNNING"
    mock_docker_client.run_container.assert_called_once()

    # 2. Pause (Simulating auto-pause or manual pause)
    # We verify that pause_container is called
    await manager.pause_worker(worker_id)  # API to be implemented

    mock_docker_client.pause_container.assert_called_with("worker-test-" + worker_id)
    status = await manager.get_worker_status(worker_id)
    assert status == "PAUSED"

    # 3. Resume
    await manager.resume_worker(worker_id)  # API to be implemented

    mock_docker_client.unpause_container.assert_called_with("worker-test-" + worker_id)
    status = await manager.get_worker_status(worker_id)
    assert status == "RUNNING"

    # 4. Delete
    await manager.delete_worker(worker_id)

    mock_docker_client.remove_container.assert_called_with("worker-test-" + worker_id, force=True)
    status = await manager.get_worker_status(worker_id)
    assert status == "STOPPED"


@pytest.mark.asyncio
async def test_image_caching_strategy(mock_docker_client, worker_settings):
    """
    Test that ensure_image adheres to caching strategy:
    - If image exists, do NOT pull/build.
    - If image missing, Pull/Build.
    - Always update last_used_at.
    """
    redis = aioredis.FakeRedis(decode_responses=True)
    manager = WorkerManager(redis=redis, docker_client=mock_docker_client)

    test_image = "custom-worker:latest"

    # Scenario A: Image Missing
    mock_docker_client.image_exists = AsyncMock(return_value=False)
    mock_docker_client.pull_image = AsyncMock()

    await manager.ensure_image(test_image)

    # Expect pull or build (assuming pull for simple case)
    mock_docker_client.pull_image.assert_called_with(test_image)

    # Verify Redis access timestamp
    last_used = await redis.get(f"worker:image:last_used:{test_image}")
    assert last_used is not None

    # Scenario B: Image Exists
    mock_docker_client.image_exists = AsyncMock(return_value=True)
    mock_docker_client.pull_image = AsyncMock()

    await manager.ensure_image(test_image)

    mock_docker_client.pull_image.assert_not_called()
    # Timestamp should be updated
    new_last_used = await redis.get(f"worker:image:last_used:{test_image}")
    assert new_last_used >= last_used


@pytest.mark.asyncio
async def test_garbage_collection_real_logic(mock_docker_client, worker_settings):
    """
    Test GC logic deleting old images.
    """
    redis = aioredis.FakeRedis(decode_responses=True)
    manager = WorkerManager(redis=redis, docker_client=mock_docker_client)

    # Setup: 2 images. One old, one new.
    old_image = "worker:old"
    new_image = "worker:new"

    # Make retention very short for test

    now = datetime.now()
    old_time = (now - timedelta(seconds=10)).isoformat()
    new_time = (now - timedelta(seconds=0)).isoformat()

    # Key: worker:image:last_used:{image_name}
    await redis.set(f"worker:image:last_used:{old_image}", old_time)
    await redis.set(f"worker:image:last_used:{new_image}", new_time)

    # We need to mock list_images to return these
    # In reality docker returns objects with tags
    img1 = MagicMock()
    img1.tags = [old_image]
    img2 = MagicMock()
    img2.tags = [new_image]

    mock_docker_client.list_images = AsyncMock(return_value=[img1, img2])
    mock_docker_client.remove_image = AsyncMock()

    # Run GC
    await manager.garbage_collect_images(retention_seconds=2)

    # Verify old image removed
    mock_docker_client.remove_image.assert_called_with(old_image, force=True)

    # Verify new image kept
    # We can't easily assert NOT called with specific arg effectively if we don't know call order perfectly,
    # but we can check call_args_list
    removed_images = [call.args[0] for call in mock_docker_client.remove_image.call_args_list]
    assert old_image in removed_images
    assert new_image not in removed_images


@pytest.mark.asyncio
async def test_auto_pause_workers(mock_docker_client, worker_settings):
    """
    Test background task pausing inactive workers.
    """
    redis = aioredis.FakeRedis(decode_responses=True)
    manager = WorkerManager(redis=redis, docker_client=mock_docker_client)

    active_worker = "w-active"
    idle_worker = "w-idle"

    # Configure AsyncMocks
    mock_docker_client.pause_container = AsyncMock()

    # Setup status
    await redis.hset(f"worker:status:{active_worker}", mapping={"status": "RUNNING"})
    await redis.hset(f"worker:status:{idle_worker}", mapping={"status": "RUNNING"})

    # Setup activity timestamps
    # Auto-pause threshold e.g. 600s
    now = datetime.now().timestamp()

    # Active: 10s ago
    await redis.set(f"worker:last_activity:{active_worker}", str(now - 10))
    # Idle: 1000s ago
    await redis.set(f"worker:last_activity:{idle_worker}", str(now - 1000))

    # Run check
    await manager.check_and_pause_workers(idle_timeout=600)

    # Verify calls
    mock_docker_client.pause_container.assert_called_with(f"worker-test-{idle_worker}")

    # Verify status update
    assert await manager.get_worker_status(idle_worker) == "PAUSED"
    assert await manager.get_worker_status(active_worker) == "RUNNING"
