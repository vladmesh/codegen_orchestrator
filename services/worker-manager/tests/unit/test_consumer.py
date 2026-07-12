import pytest
import pytest_asyncio
from unittest.mock import MagicMock, AsyncMock
from fakeredis import aioredis

from shared.contracts.dto.worker import WorkerStatus
from shared.contracts.queues.worker import (
    CreateWorkerCommand,
    DeleteWorkerCommand,
    StatusWorkerCommand,
    WorkerConfig,
    AgentType,
    WorkerCapability,
    CreateWorkerResponse,
    WorkerCommand,
)
from shared.queues import WORKER_COMMANDS, WORKER_MANAGER_GROUP, WORKER_RESPONSES
from shared.redis_client import RedisStreamClient

from src.consumer import WorkerCommandConsumer
from src.manager import WorkerManager


@pytest.fixture
def mock_worker_manager():
    manager = MagicMock(spec=WorkerManager)
    manager.create_worker = AsyncMock(return_value="test-worker-id")
    manager.create_worker_with_capabilities = AsyncMock(return_value="test-worker-id")
    manager.delete_worker = AsyncMock(return_value=None)
    manager.get_worker_status = AsyncMock(return_value=WorkerStatus.RUNNING)
    return manager


@pytest_asyncio.fixture
async def redis_client():
    redis = aioredis.FakeRedis(decode_responses=True)
    yield redis
    await redis.aclose()


@pytest_asyncio.fixture
async def stream_client(redis_client):
    client = RedisStreamClient(redis_url="redis://fake:6379")
    client._redis = redis_client
    return client


async def _drain_once(consumer):
    """Drive the run() envelope until the command stream goes idle (first None).

    Mirrors WorkerCommandConsumer.run(): validation is terminal inside the
    client, and a transient processing error is logged and left unacked.
    """
    async for msg in consumer.client.consume_typed(
        consumer.stream_name,
        consumer.group_name,
        consumer.consumer_name,
        WorkerCommand,
        block_ms=100,
        count=10,
        claim_pending=True,
    ):
        if msg is None:
            break
        try:
            await consumer.process_entry(msg)
        except Exception:
            pass  # run() logs and leaves the entry unacked for reclaim


def _create_command() -> CreateWorkerCommand:
    return CreateWorkerCommand(
        request_id="req-123",
        config=WorkerConfig(
            name="test-worker",
            worker_type="developer",
            agent_type=AgentType.CLAUDE,
            instructions="Do work",
            allowed_commands=[],
            capabilities=[WorkerCapability.GIT],
        ),
    )


@pytest.mark.asyncio
async def test_consume_create_worker_command(redis_client, stream_client, mock_worker_manager):
    """A valid create command is validated, dispatched, answered, and ACKed."""
    consumer = WorkerCommandConsumer(client=stream_client, manager=mock_worker_manager)

    await stream_client.publish(WORKER_COMMANDS, _create_command().model_dump(mode="json"))

    await _drain_once(consumer)

    mock_worker_manager.create_worker_with_capabilities.assert_called_once()

    response_messages = await redis_client.xread(streams={WORKER_RESPONSES: "0-0"}, count=1)
    assert response_messages, "Should have published response"
    _, msgs = response_messages[0]
    _msg_id, msg_data = msgs[0]
    response = CreateWorkerResponse.model_validate(RedisStreamClient._parse_fields(msg_data))
    assert response.request_id == "req-123"
    assert response.success is True
    assert response.worker_id == "test-worker"

    pending = await redis_client.xpending(WORKER_COMMANDS, WORKER_MANAGER_GROUP)
    assert pending["pending"] == 0  # acked after successful processing


@pytest.mark.asyncio
async def test_consume_delete_worker_command(redis_client, stream_client, mock_worker_manager):
    """A valid delete command is dispatched and answered without wire changes."""
    consumer = WorkerCommandConsumer(client=stream_client, manager=mock_worker_manager)

    command = DeleteWorkerCommand(request_id="del-123", worker_id="worker-to-del")
    await stream_client.publish(WORKER_COMMANDS, command.model_dump(mode="json"))

    await _drain_once(consumer)

    mock_worker_manager.delete_worker.assert_called_with("worker-to-del", reason=None)
    assert await redis_client.xlen(WORKER_RESPONSES) > 0
    pending = await redis_client.xpending(WORKER_COMMANDS, WORKER_MANAGER_GROUP)
    assert pending["pending"] == 0


@pytest.mark.asyncio
async def test_consume_status_worker_command(redis_client, stream_client, mock_worker_manager):
    consumer = WorkerCommandConsumer(client=stream_client, manager=mock_worker_manager)

    command = StatusWorkerCommand(request_id="stat-123", worker_id="some-worker")
    await stream_client.publish(WORKER_COMMANDS, command.model_dump(mode="json"))

    await _drain_once(consumer)

    mock_worker_manager.get_worker_status.assert_called_with("some-worker")


@pytest.mark.asyncio
async def test_broken_json_is_discarded_terminally(redis_client, stream_client, mock_worker_manager):
    """A malformed payload never reaches the manager and is ACKed away."""
    consumer = WorkerCommandConsumer(client=stream_client, manager=mock_worker_manager)

    await redis_client.xadd(WORKER_COMMANDS, {"data": "{not valid json"})

    await _drain_once(consumer)

    mock_worker_manager.create_worker_with_capabilities.assert_not_called()
    mock_worker_manager.delete_worker.assert_not_called()
    pending = await redis_client.xpending(WORKER_COMMANDS, WORKER_MANAGER_GROUP)
    assert pending["pending"] == 0  # terminal, no poison loop


@pytest.mark.asyncio
async def test_schema_invalid_payload_is_discarded_terminally(redis_client, stream_client, mock_worker_manager):
    """Valid JSON that matches no command type is discarded, not dispatched."""
    consumer = WorkerCommandConsumer(client=stream_client, manager=mock_worker_manager)

    await stream_client.publish(WORKER_COMMANDS, {"command": "nonsense", "request_id": "x"})

    await _drain_once(consumer)

    mock_worker_manager.create_worker_with_capabilities.assert_not_called()
    mock_worker_manager.delete_worker.assert_not_called()
    mock_worker_manager.get_worker_status.assert_not_called()
    pending = await redis_client.xpending(WORKER_COMMANDS, WORKER_MANAGER_GROUP)
    assert pending["pending"] == 0


@pytest.mark.asyncio
async def test_transient_processing_error_leaves_message_unacked(
    redis_client, stream_client, mock_worker_manager, monkeypatch
):
    """A transient handler failure propagates and the entry stays in the PEL."""
    consumer = WorkerCommandConsumer(client=stream_client, manager=mock_worker_manager)

    async def _boom(command):
        raise RuntimeError("downstream unavailable")

    monkeypatch.setattr(consumer, "handle_command", _boom)

    await stream_client.publish(WORKER_COMMANDS, _create_command().model_dump(mode="json"))

    await _drain_once(consumer)

    pending = await redis_client.xpending(WORKER_COMMANDS, WORKER_MANAGER_GROUP)
    assert pending["pending"] == 1  # unacked → will be reclaimed and retried
