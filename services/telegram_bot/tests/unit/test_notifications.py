"""Service tests for ProvisionerNotifier.

Tests verify that telegram-bot listens to provisioner:results
and notifies admin users about server provisioning status.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from fakeredis.aioredis import FakeRedis
import pytest

from shared.contracts.queues.provisioner import ProvisionerResult
from shared.redis_client import RedisStreamClient
from src.notifications import ProvisionerNotifier


@pytest.fixture
def mock_bot():
    """Create mock Telegram Bot."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return bot


@pytest.fixture
def admin_ids():
    """Admin Telegram IDs for testing."""
    return {111111, 222222}


@pytest.fixture
async def raw_redis():
    """Provides a clean FakeRedis client."""
    client = FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
async def stream_client(raw_redis):
    """RedisStreamClient backed by the service test Redis."""
    client = RedisStreamClient(redis_url="redis://fake:6379")
    client._redis = raw_redis
    return client


@pytest.mark.asyncio
async def test_provisioner_success_notifies_admins(raw_redis, stream_client, mock_bot, admin_ids):
    """
    Scenario: Provisioning completes successfully.

    Expected:
    1. ProvisionerNotifier picks up message from provisioner:results
    2. Formats success message with server details
    3. Sends to ALL admin IDs
    """
    notifier = ProvisionerNotifier(client=stream_client, admin_ids=admin_ids)

    result = ProvisionerResult(
        request_id="test-req-1",
        status="success",
        server_handle="srv-production",
        server_ip="192.168.1.100",
        services_redeployed=3,
    )

    # Start listener in background
    task = await notifier.start(mock_bot)

    # Give listener time to set up consumer group and start blocking
    await asyncio.sleep(0.2)

    # Now publish the message (using data wrapper — like infra-service publishes)
    await stream_client.publish("provisioner:results", result.model_dump(mode="json"))

    # Wait for processing
    await asyncio.sleep(0.5)

    # Stop the notifier
    await notifier.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Assert: Both admins received message
    assert mock_bot.send_message.call_count == len(admin_ids)

    # Check message content
    call_args_list = mock_bot.send_message.call_args_list
    chat_ids = {call.kwargs["chat_id"] for call in call_args_list}
    assert chat_ids == admin_ids

    # Check message text contains server info
    text = call_args_list[0].kwargs["text"]
    assert "srv-production" in text
    assert "192.168.1.100" in text


@pytest.mark.asyncio
async def test_provisioner_failure_notifies_admins(raw_redis, stream_client, mock_bot, admin_ids):
    """
    Scenario: Provisioning fails.

    Expected:
    1. ProvisionerNotifier picks up failure message
    2. Formats error message with details
    3. Sends to all admins with error indicator
    """
    notifier = ProvisionerNotifier(client=stream_client, admin_ids=admin_ids)

    result = ProvisionerResult(
        request_id="test-req-2",
        status="failed",
        server_handle="srv-staging",
        errors=["SSH connection timeout", "Ansible playbook failed"],
    )

    task = await notifier.start(mock_bot)
    await asyncio.sleep(0.2)

    await stream_client.publish("provisioner:results", result.model_dump(mode="json"))
    await asyncio.sleep(0.5)

    await notifier.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert mock_bot.send_message.call_count == len(admin_ids)

    text = mock_bot.send_message.call_args_list[0].kwargs["text"]
    assert "srv-staging" in text
    assert "SSH connection timeout" in text or "Ansible" in text


@pytest.mark.asyncio
async def test_no_notification_when_no_admins(raw_redis, stream_client, mock_bot):
    """
    Scenario: No admin IDs configured.

    Expected:
    1. ProvisionerNotifier processes message
    2. No messages sent (no recipients)
    """
    notifier = ProvisionerNotifier(client=stream_client, admin_ids=set())

    result = ProvisionerResult(
        request_id="test-req-3",
        status="success",
        server_handle="srv-test",
    )

    task = await notifier.start(mock_bot)
    await asyncio.sleep(0.2)

    await stream_client.publish("provisioner:results", result.model_dump(mode="json"))
    await asyncio.sleep(0.5)

    await notifier.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    mock_bot.send_message.assert_not_called()
