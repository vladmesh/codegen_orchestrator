"""Integration test: concurrent port allocation must not produce duplicates.

Validates that the atomic allocate-next endpoint handles race conditions
correctly when two concurrent requests compete for ports on the same server.
"""

import asyncio
from uuid import uuid4

import pytest


@pytest.fixture
async def race_server(api_client):
    """Create a dedicated server for race condition testing."""
    handle = "race-test-srv"
    body = {
        "handle": handle,
        "host": "race.example.com",
        "public_ip": "192.0.2.99",
        "status": "ready",
        "capacity_ram_mb": 8192,
        "capacity_disk_mb": 51200,
        "is_managed": True,
    }
    resp = await api_client.post("/api/servers/", json=body)
    # 201 = created, 400 = already exists (from previous test run)
    if resp.status_code not in (201, 400):  # noqa: PLR2004
        pytest.fail(f"Failed to seed server: {resp.text}")
    yield handle


@pytest.mark.asyncio
async def test_concurrent_allocations_get_different_ports(api_client, race_server):
    """Two concurrent allocate-next calls must return different ports."""

    async def allocate_one(idx: int) -> dict:
        resp = await api_client.post(
            f"/api/servers/{race_server}/ports/allocate-next",
            json={
                "service_name": f"svc-{idx}",
                "project_id": str(uuid4()),
            },
        )
        assert resp.status_code == 200, f"Allocation {idx} failed: {resp.text}"  # noqa: PLR2004
        return resp.json()

    # Fire two allocations concurrently
    results = await asyncio.gather(allocate_one(1), allocate_one(2))

    ports = {r["port"] for r in results}
    assert len(ports) == 2, f"Race condition! Both got same port: {results}"  # noqa: PLR2004


@pytest.mark.asyncio
async def test_five_concurrent_allocations_all_unique(api_client, race_server):
    """Five concurrent allocations must all get unique ports."""

    async def allocate_one(idx: int) -> dict:
        resp = await api_client.post(
            f"/api/servers/{race_server}/ports/allocate-next",
            json={
                "service_name": f"burst-{idx}",
                "project_id": str(uuid4()),
            },
        )
        assert resp.status_code == 200, f"Allocation {idx} failed: {resp.text}"  # noqa: PLR2004
        return resp.json()

    results = await asyncio.gather(*[allocate_one(i) for i in range(5)])

    ports = {r["port"] for r in results}
    assert len(ports) == 5, f"Port collision detected: {[r['port'] for r in results]}"  # noqa: PLR2004
