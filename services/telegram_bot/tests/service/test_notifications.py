"""Service tests for ProvisionerNotifier.

Tests verify that telegram-bot listens to provisioner:results
and notifies admin users about server provisioning status.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.contracts.queues.provisioner import ProvisionerResult


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


@pytest.mark.asyncio
async def test_provisioner_success_notifies_admins(redis_client, mock_bot, admin_ids):
    """
    Scenario: Provisioning completes successfully.

    Expected:
    1. ProvisionerNotifier picks up message from provisioner:results
    2. Formats success message with server details
    3. Sends to ALL admin IDs
    """
    from src.notifications import ProvisionerNotifier

    notifier = ProvisionerNotifier(redis=redis_client, admin_ids=admin_ids)

    # Create result to publish
    result = ProvisionerResult(
        request_id="test-req-1",
        status="success",
        server_handle="srv-production",
        server_ip="192.168.1.100",
        services_redeployed=3,
    )

    # Start listener in background (will block waiting for messages)
    task = asyncio.create_task(notifier._listen_once(mock_bot))

    # Give listener time to set up consumer group and start blocking
    await asyncio.sleep(0.1)

    # Now publish the message
    await redis_client.xadd(
        "provisioner:results",
        {"data": result.model_dump_json()},
    )

    # Wait for processing
    await asyncio.wait_for(task, timeout=3.0)

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
    assert "✅" in text  # Success emoji


@pytest.mark.asyncio
async def test_provisioner_failure_notifies_admins(redis_client, mock_bot, admin_ids):
    """
    Scenario: Provisioning fails.

    Expected:
    1. ProvisionerNotifier picks up failure message
    2. Formats error message with details
    3. Sends to all admins with error indicator
    """
    from src.notifications import ProvisionerNotifier

    notifier = ProvisionerNotifier(redis=redis_client, admin_ids=admin_ids)

    result = ProvisionerResult(
        request_id="test-req-2",
        status="failed",
        server_handle="srv-staging",
        errors=["SSH connection timeout", "Ansible playbook failed"],
    )

    # Start listener first
    task = asyncio.create_task(notifier._listen_once(mock_bot))
    await asyncio.sleep(0.1)

    # Then publish
    await redis_client.xadd(
        "provisioner:results",
        {"data": result.model_dump_json()},
    )

    await asyncio.wait_for(task, timeout=3.0)

    # Assert: Both admins received message
    assert mock_bot.send_message.call_count == len(admin_ids)

    # Check error content
    text = mock_bot.send_message.call_args_list[0].kwargs["text"]
    assert "srv-staging" in text
    assert "❌" in text  # Failure emoji
    assert "SSH connection timeout" in text or "Ansible" in text


@pytest.mark.asyncio
async def test_no_notification_when_no_admins(redis_client, mock_bot):
    """
    Scenario: No admin IDs configured.

    Expected:
    1. ProvisionerNotifier processes message
    2. No messages sent (no recipients)
    """
    from src.notifications import ProvisionerNotifier

    notifier = ProvisionerNotifier(redis=redis_client, admin_ids=set())

    result = ProvisionerResult(
        request_id="test-req-3",
        status="success",
        server_handle="srv-test",
    )

    # Start listener first
    task = asyncio.create_task(notifier._listen_once(mock_bot))
    await asyncio.sleep(0.1)

    # Then publish
    await redis_client.xadd(
        "provisioner:results",
        {"data": result.model_dump_json()},
    )

    await asyncio.wait_for(task, timeout=3.0)

    # Assert: No messages sent
    mock_bot.send_message.assert_not_called()
