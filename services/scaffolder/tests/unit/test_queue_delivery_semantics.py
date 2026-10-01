"""Queue semantics for the scaffolder worker loop."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src import consumer


class _FakeRedisClient:
    def __init__(self):
        self.redis = AsyncMock()
        self.rejected = []
        self.closed = False

    async def connect(self):
        return None

    async def close(self):
        self.closed = True

    async def consume(self, *_args, **_kwargs):
        yield SimpleNamespace(
            message_id="bad-1",
            data={"project_id": "proj-1"},
            reclaimed=False,
        )
        raise asyncio.CancelledError

    async def reject_entry(self, stream, group, message_id, **kwargs):
        self.rejected.append((stream, group, message_id, kwargs))

    async def reject_if_exhausted(self, *_args, **_kwargs):
        return False


@pytest.mark.asyncio
async def test_invalid_scaffold_message_is_quarantined(monkeypatch):
    redis = _FakeRedisClient()
    api = AsyncMock()

    monkeypatch.setattr(consumer, "RedisStreamClient", lambda: redis)
    monkeypatch.setattr(consumer, "get_api_client", lambda: api)
    monkeypatch.setattr(consumer, "setup_logging", lambda **_kwargs: None)
    monkeypatch.setattr(consumer, "_shutdown", False)

    with pytest.raises(asyncio.CancelledError):
        await consumer.run_worker()

    assert [item[2] for item in redis.rejected] == ["bad-1"]
    redis.redis.delete.assert_awaited_once_with("scaffold:inflight:proj-1")
    assert redis.closed is True
    api.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_pending_processing_failure_keeps_inflight_marker(monkeypatch):
    redis = _FakeRedisClient()
    api = AsyncMock()

    monkeypatch.setattr(consumer, "RedisStreamClient", lambda: redis)
    monkeypatch.setattr(consumer, "get_api_client", lambda: api)
    monkeypatch.setattr(consumer, "setup_logging", lambda **_kwargs: None)
    monkeypatch.setattr(consumer, "_shutdown", False)
    monkeypatch.setattr(consumer.ScaffoldMessage, "model_validate", lambda _data: object())

    async def fail_processing(_data, _redis):
        raise RuntimeError("transient processing failure")

    monkeypatch.setattr(consumer, "process_scaffold_job", fail_processing)

    with pytest.raises(asyncio.CancelledError):
        await consumer.run_worker()

    redis.redis.delete.assert_not_awaited()
    assert redis.closed is True
    api.close.assert_awaited_once()
