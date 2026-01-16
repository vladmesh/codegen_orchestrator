"""Service tests for POSessionManager.

These tests verify the NEW architecture where telegram-bot
communicates with worker-manager via Redis Streams.

Expected behavior:
1. New user → CreateWorkerCommand published to `worker:commands`
2. Existing session → Reuse worker_id from Redis
3. User message → Published to `worker:po:{id}:input`
"""

import json
from unittest.mock import AsyncMock

import pytest

from shared.contracts.queues.worker import AgentType, CreateWorkerCommand

# Import the class we're testing (will fail until implemented)
# This is the expected import path after refactoring
from src.session.po_session_manager import POSessionManager


@pytest.mark.asyncio
async def test_new_user_creates_po_worker_via_redis(redis_client):
    """
    Scenario: First message from user triggers PO worker creation.

    Expected:
    1. POSessionManager checks Redis for existing session → not found
    2. Publishes CreateWorkerCommand to `worker:commands` stream
    3. Waits for response in `worker:responses` (mocked here)
    4. Stores session mapping in Redis
    """
    user_id = 12345

    # Create manager with real Redis
    manager = POSessionManager(redis=redis_client)

    # Mock the response listener (we don't have worker-manager running)
    # In real scenario, worker-manager would respond
    async def mock_wait_for_response(request_id: str, timeout: float):
        return {"worker_id": f"po-{user_id}", "success": True}

    manager._wait_for_worker_response = AsyncMock(side_effect=mock_wait_for_response)

    # Act: Get or create worker
    worker_id = await manager.get_or_create_worker(user_id)

    # Assert: Worker ID returned
    assert worker_id == f"po-{user_id}"

    # Assert: CreateWorkerCommand was published to Redis
    messages = await redis_client.xrange("worker:commands", "-", "+")
    assert len(messages) == 1

    msg_id, msg_data = messages[0]
    payload = json.loads(msg_data["data"])
    command = CreateWorkerCommand.model_validate(payload)

    assert command.config.worker_type == "po"
    assert command.config.agent_type == AgentType.CLAUDE
    assert str(user_id) in command.config.name

    # Assert: Session stored in Redis
    stored_worker_id = await redis_client.get(f"session:po:{user_id}")
    assert stored_worker_id == worker_id


@pytest.mark.asyncio
async def test_existing_session_reuses_worker(redis_client):
    """
    Scenario: User has existing active session.

    Expected:
    1. POSessionManager finds session in Redis
    2. Checks worker status (mocked as running)
    3. Returns existing worker_id WITHOUT publishing new command
    """
    user_id = 67890
    existing_worker_id = "po-existing-worker"

    # Setup: Pre-existing session
    await redis_client.set(f"session:po:{user_id}", existing_worker_id)
    await redis_client.hset(f"worker:status:{existing_worker_id}", mapping={"status": "RUNNING"})

    manager = POSessionManager(redis=redis_client)

    # Act
    worker_id = await manager.get_or_create_worker(user_id)

    # Assert: Same worker returned
    assert worker_id == existing_worker_id

    # Assert: NO new command published
    messages = await redis_client.xrange("worker:commands", "-", "+")
    assert len(messages) == 0


@pytest.mark.asyncio
async def test_message_published_to_worker_input_stream(redis_client):
    """
    Scenario: User sends message to PO worker.

    Expected:
    1. Message is XADD'ed to `worker:po:{id}:input`
    2. Payload contains user text and metadata
    """
    user_id = 11111
    worker_id = "po-worker-for-msg-test"
    message_text = "Создай мне приложение для учёта задач"

    # Setup: Existing session
    await redis_client.set(f"session:po:{user_id}", worker_id)
    await redis_client.hset(f"worker:status:{worker_id}", mapping={"status": "RUNNING"})

    manager = POSessionManager(redis=redis_client)

    # Act
    await manager.send_message(user_id, message_text)

    # Assert: Message in worker input stream
    stream_key = f"worker:po:{worker_id}:input"
    messages = await redis_client.xrange(stream_key, "-", "+")
    assert len(messages) == 1

    msg_id, msg_data = messages[0]
    payload = json.loads(msg_data["data"])

    assert payload["content"] == message_text
    assert payload["user_id"] == user_id
