"""Integration tests for Provisioner Result Flow.

RED phase: These tests will FAIL until scheduler has consumer loop for provisioner:results.

Flow being tested:
1. Server created in 'provisioning' status via API
2. ProvisionerResult published to provisioner:results (emulating infra-service)
3. Scheduler consumer loop processes the message
4. Server status updated in DB via API
"""

import asyncio
import uuid

import pytest

from shared.contracts.dto.server import ServerStatus
from shared.contracts.queues.provisioner import ProvisionerResult


@pytest.mark.asyncio
async def test_provisioner_success_flow_updates_server_to_active(redis_client, api_client):
    """
    Integration Test: Full provisioner feedback loop.

    GIVEN: Server in 'provisioning' status exists in DB
    WHEN:  ProvisionerResult(status="success") is published to provisioner:results
    THEN:  Scheduler processes it and updates server status to 'active'
    """
    # Arrange: Create unique server handle
    server_handle = f"int-prov-{uuid.uuid4().hex[:8]}"

    # Create server via API in 'provisioning' status
    resp = await api_client.post(
        "/api/servers/",
        json={
            "handle": server_handle,
            "host": f"{server_handle}.example.com",
            "public_ip": f"10.0.{hash(server_handle) % 256}.1",
            "is_managed": True,
            "status": "provisioning",
            "labels": {"provider_id": server_handle},
        },
    )
    assert resp.status_code == 201, f"Failed to create server: {resp.text}"

    # Act: Publish ProvisionerResult to Redis stream (emulating infra-service)
    result = ProvisionerResult(
        request_id=f"req-{uuid.uuid4().hex[:8]}",
        status="success",
        server_handle=server_handle,
        server_ip="10.0.0.1",
        services_redeployed=0,
    )

    await redis_client.xadd(
        "provisioner:results",
        {"data": result.model_dump_json()},
    )

    # Wait for scheduler to process (polling with timeout)
    max_attempts = 10
    for _attempt in range(max_attempts):
        resp = await api_client.get("/api/servers/")
        servers = resp.json()
        target = next((s for s in servers if s["handle"] == server_handle), None)

        if target and target["status"] == ServerStatus.ACTIVE:
            break

        await asyncio.sleep(1)
    else:
        # Get final status for error message
        resp = await api_client.get("/api/servers/")
        servers = resp.json()
        target = next((s for s in servers if s["handle"] == server_handle), None)
        final_status = target["status"] if target else "NOT FOUND"

        pytest.fail(
            f"Server status not updated to 'active' within {max_attempts} seconds. "
            f"Final status: {final_status}"
        )

    # Assert: Server status is now active
    assert target["status"] == ServerStatus.ACTIVE


@pytest.mark.asyncio
async def test_provisioner_failure_flow_updates_server_to_unreachable(redis_client, api_client):
    """
    Integration Test: Failed provisioning updates status to unreachable.

    GIVEN: Server in 'provisioning' status exists in DB
    WHEN:  ProvisionerResult(status="failed") is published
    THEN:  Scheduler processes it and updates server status to 'unreachable'
    """
    # Arrange: Create unique server
    server_handle = f"int-prov-fail-{uuid.uuid4().hex[:8]}"

    resp = await api_client.post(
        "/api/servers/",
        json={
            "handle": server_handle,
            "host": f"{server_handle}.example.com",
            "public_ip": f"10.1.{hash(server_handle) % 256}.1",
            "is_managed": True,
            "status": "provisioning",
            "labels": {},
        },
    )
    assert resp.status_code == 201, f"Failed to create server: {resp.text}"

    # Act: Publish failure result
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

    # Wait for scheduler to process
    max_attempts = 10
    for _attempt in range(max_attempts):
        resp = await api_client.get("/api/servers/")
        servers = resp.json()
        target = next((s for s in servers if s["handle"] == server_handle), None)

        if target and target["status"] == ServerStatus.UNREACHABLE:
            break

        await asyncio.sleep(1)
    else:
        resp = await api_client.get("/api/servers/")
        servers = resp.json()
        target = next((s for s in servers if s["handle"] == server_handle), None)
        final_status = target["status"] if target else "NOT FOUND"

        pytest.fail(
            f"Server status not updated to 'unreachable' within {max_attempts} seconds. "
            f"Final status: {final_status}"
        )

    # Assert
    assert target["status"] == ServerStatus.UNREACHABLE
