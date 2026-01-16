"""Service tests for Provisioner Result Listener.

RED phase: These tests will FAIL until provisioner_result_listener.py is implemented.

Test scenario:
1. Create server in 'provisioning' status via API
2. Publish ProvisionerResult to provisioner:results stream
3. Run listener (single iteration)
4. Verify server status updated via API
"""

import os
from unittest.mock import AsyncMock, patch
import uuid

import httpx
import pytest
import redis.asyncio as redis

from shared.contracts.dto.server import ServerStatus
from shared.contracts.queues.provisioner import ProvisionerResult


@pytest.fixture
async def redis_client():
    """Real Redis client from test environment."""
    redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    client = redis.from_url(redis_url, decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def unique_server_handle():
    """Generate unique server handle per test to avoid DB conflicts."""
    return f"vps-test-{uuid.uuid4().hex[:8]}"


@pytest.fixture
async def seeded_provisioning_server(api_client, unique_server_handle):
    """Create a server in 'provisioning' status for testing."""
    async with httpx.AsyncClient(base_url=api_client.base_url) as client:
        resp = await client.post(
            "/api/servers/",
            json={
                "handle": unique_server_handle,
                "host": f"{unique_server_handle}.example.com",
                "public_ip": f"192.168.{hash(unique_server_handle) % 256}.1",
                "is_managed": True,
                "status": "provisioning",
                "labels": {"provider_id": unique_server_handle},
            },
        )
        assert resp.status_code == httpx.codes.CREATED, f"Failed to create server: {resp.text}"

        yield {"handle": unique_server_handle}


@pytest.mark.asyncio
async def test_provisioner_success_updates_server_to_active(
    redis_client, api_client, seeded_provisioning_server, unique_server_handle
):
    """
    Service Test: Successful provisioning updates server status.

    GIVEN a server in 'provisioning' status
    WHEN ProvisionerResult(status="success") is published to provisioner:results
    AND the listener processes the message
    THEN server status is updated to 'active' via API
    """
    # Import will fail until module is created (RED phase)
    from src.tasks.provisioner_result_listener import process_provisioner_result

    server_handle = unique_server_handle

    # Arrange: Create success result
    result = ProvisionerResult(
        request_id=f"req-{uuid.uuid4().hex[:8]}",
        status="success",
        server_handle=server_handle,
        server_ip="192.168.100.1",
        services_redeployed=0,
    )

    await redis_client.xadd(
        "provisioner:results",
        {"data": result.model_dump_json()},
    )

    # Act: Process single result (simulates listener iteration)
    await process_provisioner_result(result)

    # Assert: Server status updated to active
    servers = await api_client.get_servers()
    target = next((s for s in servers if s.handle == server_handle), None)

    assert target is not None, "Server not found after processing"
    assert target.status == ServerStatus.ACTIVE, f"Expected 'active', got '{target.status}'"


@pytest.mark.asyncio
async def test_provisioner_failure_updates_server_and_notifies_admins(
    redis_client, api_client, seeded_provisioning_server, unique_server_handle
):
    """
    Service Test: Failed provisioning updates status and notifies admins.

    GIVEN a server in 'provisioning' status
    WHEN ProvisionerResult(status="failed", errors=["SSH timeout"]) is published
    AND the listener processes the message
    THEN server status is updated to 'unreachable'
    AND notify_admins is called with error details
    """
    from src.tasks.provisioner_result_listener import process_provisioner_result

    server_handle = unique_server_handle

    # Arrange: Create failure result
    result = ProvisionerResult(
        request_id=f"req-{uuid.uuid4().hex[:8]}",
        status="failed",
        server_handle=server_handle,
        server_ip=None,
        errors=["SSH connection timeout", "Host unreachable"],
    )

    await redis_client.xadd(
        "provisioner:results",
        {"data": result.model_dump_json()},
    )

    # Act: Process with mocked notify_admins
    with patch(
        "src.tasks.provisioner_result_listener.notify_admins", new_callable=AsyncMock
    ) as mock_notify:
        await process_provisioner_result(result)

        # Assert: notify_admins was called
        mock_notify.assert_called_once()
        call_args = mock_notify.call_args
        assert server_handle in call_args[0][0]  # server handle in message
        assert "SSH connection timeout" in call_args[0][0]  # error in message
        assert call_args[1]["level"] == "error"  # level=error

    # Assert: Server status updated to unreachable
    servers = await api_client.get_servers()
    target = next((s for s in servers if s.handle == server_handle), None)

    assert target is not None
    assert (
        target.status == ServerStatus.UNREACHABLE
    ), f"Expected 'unreachable', got '{target.status}'"


@pytest.mark.asyncio
async def test_provisioner_result_with_unknown_server_logs_error(redis_client, api_client):
    """
    Service Test: Unknown server handle is gracefully handled.

    GIVEN no server exists with handle 'vps-nonexistent'
    WHEN ProvisionerResult for this handle is processed
    THEN error is logged but no exception raised
    AND processing continues (idempotent)
    """
    from src.tasks.provisioner_result_listener import process_provisioner_result

    # Arrange: Result for non-existent server
    nonexistent_handle = f"vps-nonexistent-{uuid.uuid4().hex[:8]}"
    result = ProvisionerResult(
        request_id=f"req-{uuid.uuid4().hex[:8]}",
        status="success",
        server_handle=nonexistent_handle,
        server_ip="10.0.0.99",
    )

    # Act: Should not raise, just log warning
    # This test passes if no exception is raised
    await process_provisioner_result(result)

    # Assert: No crash, operation is idempotent
