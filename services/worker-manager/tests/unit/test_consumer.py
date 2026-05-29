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
)
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


@pytest.mark.asyncio
async def test_consume_create_worker_command(redis_client, stream_client, mock_worker_manager):
    """Test that CreateWorkerCommand is consumed, manager called, and response published."""
    consumer = WorkerCommandConsumer(client=stream_client, manager=mock_worker_manager)

    command = CreateWorkerCommand(
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

    # Push command via publish (JSON "data" wrapper)
    await stream_client.publish("worker:commands", command.model_dump(mode="json"))

    # Ensure consumer group exists
    await stream_client.ensure_consumer_group("worker:commands", "worker_manager")

    # Read and parse (simulating what consume() does internally)
    resp = await redis_client.xreadgroup(
        groupname="worker_manager",
        consumername="worker_manager_1",
        streams={"worker:commands": ">"},
        count=1,
    )
    assert resp, "Should have read the message we just pushed"
    _stream, messages = resp[0]
    message_id, raw_data = messages[0]

    # Parse via the real client helper (decodes bytes + unwraps JSON)
    data = RedisStreamClient._parse_fields(raw_data)

    # Process
    await consumer.process_message(message_id, data)

    # Verify Manager Call
    mock_worker_manager.create_worker_with_capabilities.assert_called_once()

    # Verify Response
    response_messages = await redis_client.xread(streams={"worker:responses:developer": "0-0"}, count=1)
    assert response_messages, "Should have published response"
    _, msgs = response_messages[0]
    _msg_id, msg_data = msgs[0]

    response = CreateWorkerResponse.model_validate(RedisStreamClient._parse_fields(msg_data))
    assert response.request_id == "req-123"
    assert response.success is True
    assert response.worker_id == "test-worker"


@pytest.mark.asyncio
async def test_consume_delete_worker_command(redis_client, stream_client, mock_worker_manager):
    """Test DeleteWorkerCommand consumption."""
    consumer = WorkerCommandConsumer(client=stream_client, manager=mock_worker_manager)

    command = DeleteWorkerCommand(request_id="del-123", worker_id="worker-to-del")

    await stream_client.publish("worker:commands", command.model_dump(mode="json"))
    await stream_client.ensure_consumer_group("worker:commands", "worker_manager")

    resp = await redis_client.xreadgroup(
        groupname="worker_manager",
        consumername="worker_manager_1",
        streams={"worker:commands": ">"},
        count=1,
    )
    message_id, raw_data = resp[0][1][0]
    data = RedisStreamClient._parse_fields(raw_data)

    await consumer.process_message(message_id, data)

    mock_worker_manager.delete_worker.assert_called_with("worker-to-del", reason=None)

    r1 = await redis_client.xlen("worker:responses:developer")
    assert r1 > 0, "Should publish response"


@pytest.mark.asyncio
async def test_consume_status_worker_command(redis_client, stream_client, mock_worker_manager):
    consumer = WorkerCommandConsumer(client=stream_client, manager=mock_worker_manager)

    command = StatusWorkerCommand(request_id="stat-123", worker_id="some-worker")

    await stream_client.publish("worker:commands", command.model_dump(mode="json"))
    await stream_client.ensure_consumer_group("worker:commands", "worker_manager")

    resp = await redis_client.xreadgroup(
        groupname="worker_manager",
        consumername="c1",
        streams={"worker:commands": ">"},
        count=1,
    )
    msg_id, raw_data = resp[0][1][0]
    data = RedisStreamClient._parse_fields(raw_data)

    await consumer.process_message(msg_id, data)

    mock_worker_manager.get_worker_status.assert_called_with("some-worker")
