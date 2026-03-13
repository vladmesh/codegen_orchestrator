"""Unit tests for queue message browser endpoints."""

from http import HTTPStatus
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient
import pytest

from src.main import app


@pytest.fixture
def mock_redis():
    r = AsyncMock()
    r.aclose = AsyncMock()
    return r


@pytest.mark.asyncio
async def test_queue_messages_returns_parsed_messages(mock_redis):
    mock_redis.xrange = AsyncMock(
        return_value=[
            ("1710000000000-0", {"data": '{"type": "task", "task_id": "abc123"}'}),
            ("1710000001000-0", {"key": "value"}),
        ]
    )
    mock_redis.xinfo_stream = AsyncMock(return_value={"length": 2})

    with patch("src.routers.debug.aioredis.from_url", return_value=mock_redis):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/debug/queues/engineering:queue/messages?count=50")

    assert resp.status_code == HTTPStatus.OK
    data = resp.json()
    assert data["stream"] == "engineering:queue"
    assert len(data["messages"]) == 2
    assert data["total"] == 2

    # First message: unwrapped from {data: json}
    msg0 = data["messages"][0]
    assert msg0["id"] == "1710000000000-0"
    assert msg0["data"]["type"] == "task"
    assert msg0["data"]["task_id"] == "abc123"
    assert msg0["timestamp"] == 1710000000.0

    # Second message: raw fields (no data envelope)
    msg1 = data["messages"][1]
    assert msg1["data"]["key"] == "value"


@pytest.mark.asyncio
async def test_queue_messages_empty_stream(mock_redis):
    mock_redis.xrange = AsyncMock(side_effect=Exception("no such key"))

    with patch("src.routers.debug.aioredis.from_url", return_value=mock_redis):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/debug/queues/nonexistent:queue/messages")

    assert resp.status_code == HTTPStatus.OK
    data = resp.json()
    assert data["messages"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_queue_pending_returns_entries(mock_redis):
    mock_redis.xpending_range = AsyncMock(
        return_value=[
            {
                "message_id": "1710000000000-0",
                "consumer": "worker-1",
                "time_since_delivered": 5000,
                "times_delivered": 2,
            },
        ]
    )

    with patch("src.routers.debug.aioredis.from_url", return_value=mock_redis):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/debug/queues/engineering:queue/capability-workers/pending")

    assert resp.status_code == HTTPStatus.OK
    data = resp.json()
    assert len(data["pending"]) == 1
    assert data["pending"][0]["consumer"] == "worker-1"
    assert data["pending"][0]["idle_ms"] == 5000
    assert data["pending"][0]["delivery_count"] == 2


@pytest.mark.asyncio
async def test_queue_pending_nogroup(mock_redis):
    mock_redis.xpending_range = AsyncMock(side_effect=Exception("NOGROUP"))

    with patch("src.routers.debug.aioredis.from_url", return_value=mock_redis):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/debug/queues/engineering:queue/capability-workers/pending")

    assert resp.status_code == HTTPStatus.OK
    assert resp.json()["pending"] == []


@pytest.mark.asyncio
async def test_queue_ack_message(mock_redis):
    mock_redis.xack = AsyncMock(return_value=1)

    with patch("src.routers.debug.aioredis.from_url", return_value=mock_redis):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/debug/queues/engineering:queue/capability-workers/ack/1710000000000-0"
            )

    assert resp.status_code == HTTPStatus.OK
    assert resp.json()["acknowledged"] is True


@pytest.mark.asyncio
async def test_queue_ack_not_found(mock_redis):
    mock_redis.xack = AsyncMock(return_value=0)

    with patch("src.routers.debug.aioredis.from_url", return_value=mock_redis):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/debug/queues/engineering:queue/capability-workers/ack/9999-0"
            )

    assert resp.status_code == HTTPStatus.NOT_FOUND


@pytest.mark.asyncio
async def test_queue_delete_message(mock_redis):
    mock_redis.xdel = AsyncMock(return_value=1)

    with patch("src.routers.debug.aioredis.from_url", return_value=mock_redis):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.delete("/debug/queues/engineering:queue/messages/1710000000000-0")

    assert resp.status_code == HTTPStatus.OK
    assert resp.json()["deleted"] is True


@pytest.mark.asyncio
async def test_queue_delete_not_found(mock_redis):
    mock_redis.xdel = AsyncMock(return_value=0)

    with patch("src.routers.debug.aioredis.from_url", return_value=mock_redis):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.delete("/debug/queues/engineering:queue/messages/9999-0")

    assert resp.status_code == HTTPStatus.NOT_FOUND
