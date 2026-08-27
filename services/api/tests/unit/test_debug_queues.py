"""Unit tests for GET /api/debug/queues endpoint."""

from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient
from internal_caller import INTERNAL_HEADERS
import pytest

from shared.queues import QUEUE_TOPOLOGY
from src.main import app


@pytest.fixture
def mock_redis():
    """Create a mock Redis that simulates XINFO responses."""
    r = AsyncMock()

    # Simulate stream info for any stream
    r.xinfo_stream = AsyncMock(return_value={"length": 5})

    # Simulate group info — return one matching group
    async def fake_xinfo_groups(stream):
        return [
            {
                "name": "capability-workers",
                "consumers": 2,
                "pending": 0,
                "last-delivered-id": "1-0",
            },
            {
                "name": "infrastructure-workers",
                "consumers": 1,
                "pending": 0,
                "last-delivered-id": "3-0",
            },
            {
                "name": "scheduler-consumers",
                "consumers": 1,
                "pending": 0,
                "last-delivered-id": "4-0",
            },
            {
                "name": "telegram-bot",
                "consumers": 1,
                "pending": 0,
                "last-delivered-id": "5-0",
            },
            {
                "name": "worker_manager",
                "consumers": 1,
                "pending": 0,
                "last-delivered-id": "6-0",
            },
            {
                "name": "po-consumer",
                "consumers": 1,
                "pending": 0,
                "last-delivered-id": "7-0",
            },
            {
                "name": "tg-bot-proactive",
                "consumers": 1,
                "pending": 0,
                "last-delivered-id": "8-0",
            },
            {
                "name": "architect-consumers",
                "consumers": 1,
                "pending": 0,
                "last-delivered-id": "9-0",
            },
            {
                "name": "scaffold-consumers",
                "consumers": 1,
                "pending": 0,
                "last-delivered-id": "10-0",
            },
            {
                "name": "qa-consumers",
                "consumers": 1,
                "pending": 0,
                "last-delivered-id": "11-0",
            },
        ]

    r.xinfo_groups = AsyncMock(side_effect=fake_xinfo_groups)
    r.aclose = AsyncMock()
    return r


@pytest.mark.asyncio
async def test_debug_queues_ok(mock_redis):
    """Healthy state returns status=ok with all bindings."""
    with patch(
        "src.routers.debug.aioredis.from_url",
        return_value=mock_redis,
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
        ) as client:
            resp = await client.get("/api/debug/queues")

    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert data["status"] == "ok"
    assert len(data["bindings"]) == len(QUEUE_TOPOLOGY)
    assert data["issues"] == []

    # Spot-check one binding
    eng = next(b for b in data["bindings"] if b["stream"] == "engineering:queue")
    assert eng["group"] == "capability-workers"
    assert eng["stream_info"]["length"] == 5  # noqa: PLR2004


@pytest.mark.asyncio
async def test_debug_queues_missing_group(mock_redis):
    """Missing group is flagged as degraded."""
    # Return empty groups list → all groups missing
    mock_redis.xinfo_groups = AsyncMock(return_value=[])

    with patch(
        "src.routers.debug.aioredis.from_url",
        return_value=mock_redis,
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
        ) as client:
            resp = await client.get("/api/debug/queues")

    data = resp.json()
    assert data["status"] == "degraded"
    assert len(data["issues"]) == len(QUEUE_TOPOLOGY)  # One issue per binding


@pytest.mark.asyncio
async def test_debug_queues_high_pending(mock_redis):
    """Pending > 100 flagged as issue."""

    async def high_pending_groups(stream):
        return [
            {
                "name": "capability-workers",
                "consumers": 2,
                "pending": 150,
                "last-delivered-id": "1-0",
            },
            {
                "name": "infrastructure-workers",
                "consumers": 1,
                "pending": 0,
                "last-delivered-id": "3-0",
            },
            {
                "name": "scheduler-consumers",
                "consumers": 1,
                "pending": 0,
                "last-delivered-id": "4-0",
            },
            {
                "name": "telegram-bot",
                "consumers": 1,
                "pending": 0,
                "last-delivered-id": "5-0",
            },
            {
                "name": "worker_manager",
                "consumers": 1,
                "pending": 0,
                "last-delivered-id": "6-0",
            },
            {
                "name": "po-consumer",
                "consumers": 1,
                "pending": 0,
                "last-delivered-id": "7-0",
            },
            {
                "name": "tg-bot-proactive",
                "consumers": 1,
                "pending": 0,
                "last-delivered-id": "8-0",
            },
            {
                "name": "architect-consumers",
                "consumers": 1,
                "pending": 0,
                "last-delivered-id": "9-0",
            },
            {
                "name": "scaffold-consumers",
                "consumers": 1,
                "pending": 0,
                "last-delivered-id": "10-0",
            },
        ]

    mock_redis.xinfo_groups = AsyncMock(side_effect=high_pending_groups)

    with patch(
        "src.routers.debug.aioredis.from_url",
        return_value=mock_redis,
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
        ) as client:
            resp = await client.get("/api/debug/queues")

    data = resp.json()
    assert data["status"] == "degraded"
    high_pending_issues = [i for i in data["issues"] if "High pending" in i]
    assert len(high_pending_issues) >= 1


@pytest.mark.asyncio
async def test_debug_queues_keeps_redis_errors_explicit_instead_of_zeroing_them(mock_redis):
    """An unreadable stream is degraded state, not an empty healthy queue."""
    mock_redis.xinfo_stream = AsyncMock(side_effect=RuntimeError("connection reset"))
    mock_redis.xinfo_groups = AsyncMock(side_effect=RuntimeError("connection reset"))

    with patch("src.routers.debug.aioredis.from_url", return_value=mock_redis):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
        ) as client:
            response = await client.get("/api/debug/queues")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert all(binding["stream_info"] is None for binding in body["bindings"])
    assert all(binding["group_info"] is None for binding in body["bindings"])
    assert any("Stream error" in issue for issue in body["issues"])
