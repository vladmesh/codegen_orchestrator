import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from redis.exceptions import TimeoutError as RedisTimeoutError

os.environ.setdefault("INTERNAL_API_KEY", "test-internal-key")

from src import provisioner, worker_events


class _FailingIterator:
    def __init__(self, error):
        self.error = error

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise self.error


class _PubSub:
    def __init__(self, error):
        self.error = error
        self.subscribe = AsyncMock()

    def listen(self):
        return _FailingIterator(self.error)


class _Client:
    def __init__(self, error):
        self._pubsub = _PubSub(error)
        self.aclose = AsyncMock()

    def pubsub(self):
        return self._pubsub


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module", "listener"),
    [
        (provisioner, provisioner.listen_provisioner_triggers),
        (worker_events, worker_events.listen_worker_events),
    ],
)
async def test_read_timeout_reconnects_without_error_log(monkeypatch, module, listener):
    clients = [
        _Client(RedisTimeoutError("Timeout reading from Redis")),
        _Client(asyncio.CancelledError()),
    ]
    logger = MagicMock()
    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(redis_url="redis://test"))
    monkeypatch.setattr(module.redis, "from_url", lambda *args, **kwargs: clients.pop(0))
    monkeypatch.setattr(module, "logger", logger)

    await listener()

    assert not clients
    logger.error.assert_not_called()
    logger.warning.assert_not_called()
