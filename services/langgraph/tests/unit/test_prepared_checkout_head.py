"""The attempted turn starts after checkout's native publication, not before it."""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_prepared_remote_head_is_persisted_before_the_turn():
    from src.clients.worker_spawner import record_prepared_head

    redis = AsyncMock()
    redis.hget.return_value = b"b" * 40
    with patch("src.clients.api.api_client.patch", new_callable=AsyncMock) as write:
        assert await record_prepared_head(redis, "eng-1", "worker-1") == "b" * 40
    redis.hget.assert_awaited_once_with("worker:meta:worker-1", "prepared_head_sha")
    write.assert_awaited_once_with(
        "runs/eng-1", json={"run_metadata": {"pre_attempt_head_sha": "b" * 40}}
    )


@pytest.mark.asyncio
async def test_missing_prepared_ref_fails_before_recording_or_sending_work():
    from src.clients.worker_spawner import record_prepared_head

    redis = AsyncMock()
    redis.hget.return_value = None
    with (
        patch("src.clients.api.api_client.patch", new_callable=AsyncMock) as write,
        pytest.raises(RuntimeError, match="prepared checkout"),
    ):
        await record_prepared_head(redis, "eng-1", "worker-1")
    write.assert_not_awaited()
