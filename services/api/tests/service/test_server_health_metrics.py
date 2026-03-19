"""Integration tests for Server health metrics + metrics history endpoints."""

from http import HTTPStatus

import pytest


@pytest.fixture
async def test_server(async_client):
    """Create a test server, clean up after."""
    handle = "test-health-srv"
    resp = await async_client.post(
        "/api/servers/",
        json={
            "handle": handle,
            "host": "test.example.com",
            "public_ip": "10.0.0.99",
            "status": "active",
            "is_managed": True,
        },
    )
    if resp.status_code == HTTPStatus.BAD_REQUEST:
        # Already exists from previous run — just use it
        pass
    else:
        assert resp.status_code == HTTPStatus.CREATED
    yield handle


@pytest.mark.asyncio
async def test_patch_server_with_health_metrics(async_client, test_server):
    """PATCH server with health metrics, then GET and verify round-trip."""
    handle = test_server
    health_data = {
        "cpu_usage_pct": 42.5,
        "load_avg_1m": 1.2,
        "load_avg_5m": 0.8,
        "load_avg_15m": 0.5,
        "network_rx_errors": 10,
        "network_tx_errors": 3,
        "container_count_running": 5,
        "container_count_total": 7,
        "uptime_seconds": 86400.0,
        "last_health_check": "2026-03-17T00:00:00Z",
    }

    resp = await async_client.patch(f"/api/servers/{handle}", json=health_data)
    assert resp.status_code == HTTPStatus.OK
    data = resp.json()
    assert data["cpu_usage_pct"] == 42.5
    assert data["container_count_running"] == 5
    assert data["uptime_seconds"] == 86400.0
    assert data["last_health_check"] is not None

    # GET should also return the same data
    resp = await async_client.get(f"/api/servers/{handle}")
    assert resp.status_code == HTTPStatus.OK
    data = resp.json()
    assert data["cpu_usage_pct"] == 42.5
    assert data["load_avg_1m"] == 1.2
    assert data["network_rx_errors"] == 10


@pytest.mark.asyncio
async def test_metrics_history_roundtrip(async_client, test_server):
    """POST a metrics snapshot, GET history, verify content and ordering."""
    handle = test_server
    metrics = {"cpu_usage_pct": 55.0, "load_avg_1m": 2.1, "ram_used_bytes": 1073741824}

    # POST snapshot
    resp = await async_client.post(
        f"/api/servers/{handle}/metrics-history",
        json={"metrics": metrics},
    )
    assert resp.status_code == HTTPStatus.CREATED
    created = resp.json()
    assert created["server_handle"] == handle
    assert created["metrics"]["cpu_usage_pct"] == 55.0
    assert created["id"] is not None

    # GET history
    resp = await async_client.get(f"/api/servers/{handle}/metrics-history?hours=1")
    assert resp.status_code == HTTPStatus.OK
    history = resp.json()
    assert len(history) >= 1
    # Most recent first
    assert history[0]["metrics"]["cpu_usage_pct"] == 55.0


@pytest.mark.asyncio
async def test_metrics_history_not_found(async_client):
    """GET metrics history for non-existent server returns 404."""
    resp = await async_client.get("/api/servers/nonexistent-srv/metrics-history")
    assert resp.status_code == HTTPStatus.NOT_FOUND
