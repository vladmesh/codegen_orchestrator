"""Worker-manager publishes credential-safe executor availability."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.manager import WorkerManager


@pytest.mark.asyncio
async def test_publish_executor_diagnostics_writes_a_bounded_snapshot(monkeypatch):
    redis = AsyncMock()
    redis.scan_iter = MagicMock()
    redis.scan_iter.return_value = _empty_scan()
    manager = WorkerManager(redis=redis, docker_client=MagicMock())

    await manager.publish_executor_diagnostics()

    redis.set.assert_awaited_once()
    assert redis.set.await_args.args[0] == "executor:diagnostics:v1"
    assert redis.set.await_args.kwargs["ex"] > 0


async def _empty_scan():
    if False:
        yield ""
