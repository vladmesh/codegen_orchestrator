"""Unit tests for GET /debug/queues endpoint."""

from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient
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
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/debug/queues")

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
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/debug/queues")

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
        ]

    mock_redis.xinfo_groups = AsyncMock(side_effect=high_pending_groups)

    with patch(
        "src.routers.debug.aioredis.from_url",
        return_value=mock_redis,
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/debug/queues")

    data = resp.json()
    assert data["status"] == "degraded"
    high_pending_issues = [i for i in data["issues"] if "High pending" in i]
    assert len(high_pending_issues) >= 1
