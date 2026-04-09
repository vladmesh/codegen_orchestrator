"""Tests for periodic queue cleanup worker."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture()
def mock_redis_client():
    """Mock RedisStreamClient with underlying redis."""
    client = AsyncMock()
    client.redis = AsyncMock()
    client.connect = AsyncMock()
    client.close = AsyncMock()
    return client


class TestCleanOrphanStreams:
    """Tests for _clean_orphan_streams."""

    @pytest.mark.asyncio()
    async def test_orphan_response_streams_cleaned(self, mock_redis_client):
        """Response streams idle > threshold are deleted."""
        from src.tasks.queue_cleanup import _clean_orphan_streams

        redis = mock_redis_client.redis
        # SCAN returns cursor=0 (done) and 3 keys
        redis.scan.return_value = (
            0,
            [
                "po:response:req-001",
                "po:response:req-002",
                "po:response:req-003",
            ],
        )
        # All streams are idle > 600s
        redis.object.side_effect = [700, 800, 900]

        cleaned = await _clean_orphan_streams(mock_redis_client, idle_threshold_s=600)

        assert cleaned == 3
        assert redis.delete.call_count == 3

    @pytest.mark.asyncio()
    async def test_fresh_response_streams_kept(self, mock_redis_client):
        """Response streams idle < threshold are NOT deleted."""
        from src.tasks.queue_cleanup import _clean_orphan_streams

        redis = mock_redis_client.redis
        redis.scan.return_value = (0, ["po:response:fresh-1"])
        redis.object.return_value = 30  # 30s idle — well under threshold

        cleaned = await _clean_orphan_streams(mock_redis_client, idle_threshold_s=600)

        assert cleaned == 0
        redis.delete.assert_not_called()

    @pytest.mark.asyncio()
    async def test_worker_streams_cleaned(self, mock_redis_client):
        """Orphan worker:*:input and worker:*:output streams are cleaned."""
        from src.tasks.queue_cleanup import _clean_orphan_streams

        redis = mock_redis_client.redis
        # SCAN called 3 times (po:response:*, worker:*:input, worker:*:output)
        redis.scan.side_effect = [
            (0, []),  # no po:response streams
            (0, ["worker:dead-123:input"]),  # orphan input stream
            (0, ["worker:dead-123:output"]),  # orphan output stream
        ]
        redis.object.side_effect = [700, 700]

        cleaned = await _clean_orphan_streams(mock_redis_client, idle_threshold_s=600)

        assert cleaned == 2
        assert redis.delete.call_count == 2

    @pytest.mark.asyncio()
    async def test_scan_pagination(self, mock_redis_client):
        """SCAN with multiple pages is followed correctly."""
        from src.tasks.queue_cleanup import _clean_orphan_streams

        redis = mock_redis_client.redis
        # First pattern: two pages
        redis.scan.side_effect = [
            (42, ["po:response:page1"]),  # cursor 42 → more to scan
            (0, ["po:response:page2"]),  # cursor 0 → done
            (0, []),  # worker:*:input
            (0, []),  # worker:*:output
        ]
        redis.object.side_effect = [700, 700]

        cleaned = await _clean_orphan_streams(mock_redis_client, idle_threshold_s=600)

        assert cleaned == 2

    @pytest.mark.asyncio()
    async def test_object_idletime_error_skips_key(self, mock_redis_client):
        """If OBJECT IDLETIME fails for a key, skip it (don't crash)."""
        from src.tasks.queue_cleanup import _clean_orphan_streams

        redis = mock_redis_client.redis
        redis.scan.side_effect = [
            (0, ["po:response:broken", "po:response:ok"]),
            (0, []),
            (0, []),
        ]
        redis.object.side_effect = [Exception("key gone"), 700]

        cleaned = await _clean_orphan_streams(mock_redis_client, idle_threshold_s=600)

        assert cleaned == 1


class TestTrimOldMessages:
    """Tests for _trim_old_messages."""

    @pytest.mark.asyncio()
    async def test_xtrim_called_for_all_queues(self, mock_redis_client):
        """XTRIM MINID is called for every queue in topology."""
        from src.tasks.queue_cleanup import _trim_old_messages

        redis = mock_redis_client.redis
        redis.xtrim.return_value = 0

        with patch("src.tasks.queue_cleanup.QUEUE_TOPOLOGY") as mock_topo:
            from dataclasses import dataclass

            @dataclass(frozen=True)
            class FakeBinding:
                stream: str
                group: str
                description: str

            mock_topo.__iter__ = lambda self: iter(
                [
                    FakeBinding("queue:a", "group-a", "Queue A"),
                    FakeBinding("queue:b", "group-b", "Queue B"),
                ]
            )

            await _trim_old_messages(mock_redis_client, ttl_seconds=7 * 86400)

        assert redis.xtrim.call_count == 2

    @pytest.mark.asyncio()
    async def test_xtrim_minid_is_time_based(self, mock_redis_client):
        """XTRIM uses MINID computed from current time - TTL."""
        from src.tasks.queue_cleanup import _trim_old_messages

        redis = mock_redis_client.redis
        redis.xtrim.return_value = 5

        await _trim_old_messages(mock_redis_client, ttl_seconds=7 * 86400)

        # Verify xtrim was called with minid parameter
        for call in redis.xtrim.call_args_list:
            assert "minid" in call.kwargs
            # minid should be a string like "1234567890000-0"
            minid = call.kwargs["minid"]
            assert minid.endswith("-0")
            ts_ms = int(minid.split("-")[0])
            assert ts_ms > 0
