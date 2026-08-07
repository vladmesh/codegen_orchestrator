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
from shared.queues import PROVISIONER_RESULTS, SCHEDULER_CONSUMER_GROUP


def _entry_key(entry_id: str) -> tuple[int, int]:
    ms, seq = entry_id.split("-")
    return int(ms), int(seq)


async def _wait_until_consumed(redis_client, entry_id: str, timeout_sec: int = 15) -> None:
    """Block until the scheduler's consumer group has processed ``entry_id``.

    Asserting that a status did NOT change is only meaningful once the listener
    has actually handled the message, so this anchors the assertion to the real
    consumer progress instead of to a bare sleep. The listener ACKs only after
    processing, so "delivered and no longer pending" means "done".
    """
    for _ in range(timeout_sec):
        groups = await redis_client.xinfo_groups(PROVISIONER_RESULTS)
        group = next((g for g in groups if g["name"] == SCHEDULER_CONSUMER_GROUP), None)
        if group and _entry_key(group["last-delivered-id"]) >= _entry_key(entry_id):
            pending = await redis_client.xpending_range(
                PROVISIONER_RESULTS,
                SCHEDULER_CONSUMER_GROUP,
                min=entry_id,
                max=entry_id,
                count=1,
            )
            if not pending:
                return
        await asyncio.sleep(1)

    pytest.fail(
        f"Scheduler did not consume {entry_id} from {PROVISIONER_RESULTS} "
        f"within {timeout_sec} seconds"
    )


@pytest.mark.asyncio
async def test_provisioner_success_result_does_not_overwrite_terminal_status(
    redis_client, api_client
):
    """
    Integration Test: the success result does not move the server from the side.

    The terminal status of a successful provisioning is written by the success path
    itself (infra-service resets the attempt counter and marks the server READY in
    one atomic, episode-guarded update). The scheduler's result listener is only an
    observer: this test publishes a success result without that success path having
    run, so nothing may change the server's status.

    GIVEN: Server in 'provisioning' status exists in DB
    WHEN:  ProvisionerResult(status="success") is published to provisioner:results
    THEN:  Scheduler consumes it and leaves the status untouched
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

    entry_id = await redis_client.xadd(
        PROVISIONER_RESULTS,
        {"data": result.model_dump_json()},
    )

    await _wait_until_consumed(redis_client, entry_id)

    # Assert: the listener consumed the success result and changed nothing
    resp = await api_client.get("/api/servers/")
    target = next((s for s in resp.json() if s["handle"] == server_handle), None)
    assert target is not None, f"Server {server_handle} disappeared"
    assert target["status"] == ServerStatus.PROVISIONING, (
        "The scheduler listener must not write a terminal status on success: "
        f"got {target['status']}"
    )


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
