from unittest.mock import AsyncMock, MagicMock

from fakeredis import FakeAsyncRedis
import pytest
from worker_wrapper.config import WorkerWrapperConfig
from worker_wrapper.wrapper import WorkerWrapper


@pytest.fixture
def fake_redis():
    return FakeAsyncRedis()


@pytest.mark.asyncio
async def test_wrapper_saves_task_context_to_redis(fake_redis):
    """
    Test that WorkerWrapper extracts task_id and request_id from input message
    and saves them to the worker:status:{id} hash in Redis.
    This enables crash recovery to identify which task was running.
    """
    # 1. Setup
    worker_id = "test-worker-1"
    config = WorkerWrapperConfig(
        redis_url="redis://localhost:6379",
        agent_type="claude",
        input_stream="worker:developer:input",
        output_stream="worker:developer:output",
        consumer_group="workers",
        consumer_name=worker_id,
    )

    # Mock Redis client
    mock_redis_client = MagicMock()
    # Use the fixture instance
    mock_redis_client.redis = fake_redis
    # We need to mock connect/close/etc since they are async
    mock_redis_client.connect = AsyncMock()
    mock_redis_client.close = AsyncMock()
    mock_redis_client.ensure_consumer_group = AsyncMock()
    mock_redis_client.publish = AsyncMock()
    mock_redis_client.publish_message = AsyncMock()

    # Mock consume to yield one message then stop
    message_data = {
        "request_id": "req-123",
        "task_id": "task-456",
        "project_id": "proj-789",
        "prompt": "Fix something",
        "timeout": 1800,
    }

    mock_message = MagicMock()
    mock_message.message_id = "1-0"
    mock_message.data = message_data

    # We use a trick to make consume yield once then cancel
    async def mock_consume(**kwargs):
        yield mock_message
        # Stop loop after one message
        wrapper._running = False

    mock_redis_client.consume = mock_consume

    # Initialize Wrapper
    wrapper = WorkerWrapper(config, redis_client=mock_redis_client)

    # Mock execute_agent so we don't actually run anything
    wrapper.execute_agent = AsyncMock(return_value={"status": "success"})
    wrapper.publish_lifecycle = AsyncMock()

    # 2. Run
    await wrapper.run()

    # 3. Verify Redis State
    # Check if task context was saved to worker:status:{id}
    status_hash = await fake_redis.hgetall(f"worker:status:{worker_id}")

    # Decode bytes from redis
    status_hash = {k.decode(): v.decode() for k, v in status_hash.items()}

    assert "task_id" in status_hash, "task_id not saved to Redis status"
    assert status_hash["task_id"] == "task-456"
    assert "request_id" in status_hash, "request_id not saved to Redis status"
    assert status_hash["request_id"] == "req-123"
