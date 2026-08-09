from unittest.mock import AsyncMock, MagicMock, patch

from fakeredis import FakeAsyncRedis
import pytest
from worker_wrapper.broker import BrokerMessage
from worker_wrapper.config import WorkerWrapperConfig
from worker_wrapper.wrapper import WorkerWrapper


@pytest.fixture(autouse=True)
def _no_workspace_check():
    """Skip workspace preflight — these tests run outside containers."""
    with patch("worker_wrapper.wrapper.WORKSPACE_DIR", "/nonexistent/workspace"):
        yield


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
    config = WorkerWrapperConfig(
        broker_url="http://worker-broker:8001",
        broker_token="x" * 43,
        worker_id="test-worker",
        agent_type="claude",
    )

    # Mock Redis client
    broker_client = AsyncMock()
    # Use the fixture instance
    broker_client.update_status = AsyncMock()
    broker_client.submit_output = AsyncMock()
    broker_client.get_session = AsyncMock(return_value=None)
    broker_client.set_session = AsyncMock()

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

    broker_client.lease_input = AsyncMock(
        side_effect=[BrokerMessage(mock_message.message_id, mock_message.data), None]
    )
    broker_client.exhausted = True

    # Initialize Wrapper
    wrapper = WorkerWrapper(config, broker_client=broker_client)

    # Mock execute_agent so we don't actually run anything
    wrapper.execute_agent = AsyncMock(return_value={"status": "success"})
    wrapper._git_pull = AsyncMock()

    # 2. Run
    await wrapper.run()

    broker_client.update_status.assert_awaited_once_with(
        {"task_id": "task-456", "request_id": "req-123"}
    )


@pytest.mark.asyncio
async def test_wrapper_publishes_error_to_output_stream_on_failure(fake_redis):
    """When execute_agent raises, wrapper must publish error to output stream
    so that the spawner (engineering-worker) doesn't hang forever."""
    config = WorkerWrapperConfig(
        broker_url="http://worker-broker:8001",
        broker_token="x" * 43,
        worker_id="test-worker",
        agent_type="claude",
    )

    broker_client = AsyncMock()
    broker_client.update_status = AsyncMock()
    broker_client.submit_output = AsyncMock()
    broker_client.get_session = AsyncMock(return_value=None)
    broker_client.set_session = AsyncMock()

    mock_message = MagicMock()
    mock_message.message_id = "1-0"
    mock_message.data = {"prompt": "Do something"}

    broker_client.lease_input = AsyncMock(
        side_effect=[BrokerMessage(mock_message.message_id, mock_message.data), None]
    )
    broker_client.exhausted = True

    wrapper = WorkerWrapper(config, broker_client=broker_client)
    wrapper.execute_agent = AsyncMock(
        side_effect=RuntimeError("Agent process timed out after 600 seconds")
    )
    wrapper._git_pull = AsyncMock()

    await wrapper.run()

    broker_client.submit_output.assert_awaited_once()
