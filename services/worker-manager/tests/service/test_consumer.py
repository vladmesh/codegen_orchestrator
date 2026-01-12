import pytest
from unittest.mock import MagicMock, AsyncMock
from fakeredis import aioredis

from shared.contracts.queues.worker import (
    CreateWorkerCommand,
    DeleteWorkerCommand,
    StatusWorkerCommand,
    WorkerConfig,
    AgentType,
    WorkerCapability,
    CreateWorkerResponse,
)

# We anticipate this module will exist
from src.consumer import WorkerCommandConsumer
from src.manager import WorkerManager


@pytest.fixture
def mock_worker_manager():
    manager = MagicMock(spec=WorkerManager)
    manager.create_worker = AsyncMock(return_value="test-worker-id")
    manager.delete_worker = AsyncMock(return_value=None)
    manager.get_worker_status = AsyncMock(return_value="RUNNING")
    return manager


@pytest.fixture
async def redis_client():
    redis = aioredis.FakeRedis(decode_responses=True)
    yield redis
    await redis.close()


@pytest.mark.asyncio
async def test_consume_create_worker_command(redis_client, mock_worker_manager):
    """
    Test that CreateWorkerCommand is consumed, manager called, and response published.
    """
    # Setup Consumer
    consumer = WorkerCommandConsumer(redis=redis_client, manager=mock_worker_manager)

    # Prepare Command
    command = CreateWorkerCommand(
        request_id="req-123",
        config=WorkerConfig(
            name="test-worker",
            worker_type="po",
            agent_type=AgentType.CLAUDE,
            instructions="Do work",
            allowed_commands=[],
            capabilities=[WorkerCapability.DOCKER],
        ),
    )

    # 1. Push Command to Stream
    # We use xadd directly same as a strict producer would
    await redis_client.xadd("worker:commands", {"data": command.model_dump_json()})

    # 2. Run consumer for one message (we need to expose a way to run one step or mock run logic)
    # Ideally process_message(msg_id, data) is testable, or run_loop has a stop condition.
    # Let's assume we can call `process_batch` or similar, or just pull manually and pass to `process_message`.

    # Simulating the loop logic manually to test processing isolation:
    resp = await redis_client.xread_group(
        groupname="worker_manager", consumername="worker_manager_1", streams={"worker:commands": ">"}, count=1
    )

    assert resp, "Should have read the message we just pushed"
    stream, messages = resp[0]
    message_id, data = messages[0]

    # 3. Process
    await consumer.process_message(message_id, data)

    # 4. Verify Manager Call
    mock_worker_manager.create_worker.assert_called_once()
    call_args = mock_worker_manager.create_worker.call_args
    # create_worker signature: item_id, image, env
    # Logic inside consumer must derive these from config
    assert call_args, "Manager should be called"

    # 5. Verify Response
    # Expect response in worker:responses:po
    response_messages = await redis_client.xread(streams={"worker:responses:po": "0-0"}, count=1)
    assert response_messages, "Should have published response"
    _, msgs = response_messages[0]
    msg_id, msg_data = msgs[0]

    response = CreateWorkerResponse.model_validate_json(msg_data["data"])
    assert response.request_id == "req-123"
    assert response.success is True
    assert response.worker_id == "test-worker-id"


@pytest.mark.asyncio
async def test_consume_delete_worker_command(redis_client, mock_worker_manager):
    """
    Test DeleteWorkerCommand consumption.
    """
    consumer = WorkerCommandConsumer(redis=redis_client, manager=mock_worker_manager)

    command = DeleteWorkerCommand(request_id="del-123", worker_id="worker-to-del")

    await redis_client.xadd("worker:commands", {"data": command.model_dump_json()})

    # Simulate read
    resp = await redis_client.xread_group(
        groupname="worker_manager", consumername="worker_manager_1", streams={"worker:commands": ">"}, count=1
    )
    message_id, data = resp[0][1][0]

    await consumer.process_message(message_id, data)

    mock_worker_manager.delete_worker.assert_called_with("worker-to-del")

    # Verify Response in generic channel or specific?
    # Spec: "worker:responses:po" or "developer".
    # Delete command doesn't have worker_type effectively, but we probably want it in specific channel.
    # NOTE: DeleteWorkerCommand in CONTRACTS doesn't seem to have worker_type.
    # WorkerManager needs to know where to send response? Or does it send to both or global?
    # Contracts table says:
    # worker:responses:po - Initiator: worker-manager.
    # If the initiator of command was PO (or bot acting as PO user), it expects response there.
    # But Delete might come from LangGraph (Dev flow).
    # Let's assume for now consumer tries to infer or defaults.
    # Actually, contracts say "worker:commands" -> "worker-manager".
    # The response queue depends on who asked.
    # Maybe we should check if command has metadata or we broadcast?
    # For now let's assume it publishes to both or we check one.
    # Let's check 'worker:responses:po' for simplicity or check code implementation plan.
    # I will implement it sending to fixed or broadcast. Best effort: check both in test.

    r1 = await redis_client.xlen("worker:responses:po")
    r2 = await redis_client.xlen("worker:responses:developer")
    assert r1 + r2 > 0, "Should publish response to one of the channels"


@pytest.mark.asyncio
async def test_consume_status_worker_command(redis_client, mock_worker_manager):
    consumer = WorkerCommandConsumer(redis=redis_client, manager=mock_worker_manager)

    command = StatusWorkerCommand(request_id="stat-123", worker_id="some-worker")

    await redis_client.xadd("worker:commands", {"data": command.model_dump_json()})

    resp = await redis_client.xread_group("worker_manager", "c1", {"worker:commands": ">"}, count=1)
    msg_id, data = resp[0][1][0]

    await consumer.process_message(msg_id, data)

    mock_worker_manager.get_worker_status.assert_called_with("some-worker")
