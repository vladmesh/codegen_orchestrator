"""Unit tests for PO reminder poller."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from shared.queues import PO_INPUT_QUEUE, PO_REMINDERS_KEY
from src.po.reminders import _poll_once, run_reminder_poller


@pytest.fixture
def mock_client():
    """Mock RedisStreamClient."""
    client = AsyncMock()
    client.redis = AsyncMock()
    client.redis.zrangebyscore = AsyncMock(return_value=[])
    client.redis.zrem = AsyncMock()
    client.publish_flat = AsyncMock()
    return client


def _make_reminder(user_id: str = "user-42", text: str = "check task eng-abc123") -> str:
    return json.dumps(
        {
            "type": "reminder",
            "user_id": user_id,
            "text": text,
            "timestamp": "2026-02-15T14:30:00+00:00",
        }
    )


class TestPollOnce:
    @pytest.mark.asyncio
    async def test_fires_due_reminder(self, mock_client):
        reminder = _make_reminder()
        mock_client.redis.zrangebyscore.return_value = [reminder]

        fired = await _poll_once(mock_client)

        assert fired == 1
        mock_client.publish_flat.assert_called_once()
        call_args = mock_client.publish_flat.call_args
        assert call_args[0][0] == PO_INPUT_QUEUE
        fields = call_args[0][1]
        assert fields["type"] == "reminder"
        assert fields["user_id"] == "user-42"
        assert fields["text"] == "check task eng-abc123"

    @pytest.mark.asyncio
    async def test_ignores_future_reminders(self, mock_client):
        """ZRANGEBYSCORE with score > now returns empty — nothing to fire."""
        mock_client.redis.zrangebyscore.return_value = []

        fired = await _poll_once(mock_client)

        assert fired == 0
        mock_client.publish_flat.assert_not_called()

    @pytest.mark.asyncio
    async def test_fires_multiple_reminders(self, mock_client):
        r1 = _make_reminder(user_id="user-1", text="check task 1")
        r2 = _make_reminder(user_id="user-2", text="check task 2")
        mock_client.redis.zrangebyscore.return_value = [r1, r2]

        fired = await _poll_once(mock_client)

        assert fired == 2  # noqa: PLR2004
        assert mock_client.publish_flat.call_count == 2  # noqa: PLR2004
        assert mock_client.redis.zrem.call_count == 2  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_removes_fired_reminder(self, mock_client):
        reminder = _make_reminder()
        mock_client.redis.zrangebyscore.return_value = [reminder]

        await _poll_once(mock_client)

        mock_client.redis.zrem.assert_called_once_with(PO_REMINDERS_KEY, reminder)

    @pytest.mark.asyncio
    async def test_continues_on_parse_error(self, mock_client):
        """Invalid JSON should be removed and not block other reminders."""
        bad_entry = "not-valid-json"
        good_reminder = _make_reminder()
        mock_client.redis.zrangebyscore.return_value = [bad_entry, good_reminder]

        fired = await _poll_once(mock_client)

        assert fired == 1
        # Bad entry should still be removed from ZSET
        assert mock_client.redis.zrem.call_count == 2  # noqa: PLR2004
        mock_client.publish_flat.assert_called_once()

    @pytest.mark.asyncio
    async def test_queries_correct_score_range(self, mock_client):
        """Should query from 0 to current time."""
        mock_client.redis.zrangebyscore.return_value = []

        with patch("src.po.reminders.time") as mock_time:
            mock_time.time.return_value = 1000.0
            await _poll_once(mock_client)

        mock_client.redis.zrangebyscore.assert_called_once_with(PO_REMINDERS_KEY, 0, 1000.0)


class TestRunReminderPoller:
    @pytest.mark.asyncio
    async def test_graceful_shutdown(self, mock_client):
        """CancelledError should exit cleanly."""
        mock_client.redis.zrangebyscore.side_effect = asyncio.CancelledError()

        # Should not raise — exits cleanly
        await run_reminder_poller(mock_client)

    @pytest.mark.asyncio
    async def test_continues_on_error(self, mock_client):
        """Non-cancellation errors should be caught and the loop continues."""
        call_count = 0

        async def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("redis down")
            raise asyncio.CancelledError()

        mock_client.redis.zrangebyscore.side_effect = side_effect

        with patch("src.po.reminders.asyncio.sleep", new_callable=AsyncMock):
            await run_reminder_poller(mock_client)

        assert call_count == 2  # noqa: PLR2004


class TestSetReminderUsesConstant:
    def test_tools_module_imports_constant(self):
        """Verify set_reminder uses PO_REMINDERS_KEY, not a hardcoded string."""
        import src.po.tools as tools_module

        # The module should import PO_REMINDERS_KEY
        assert hasattr(tools_module, "PO_REMINDERS_KEY")
        assert tools_module.PO_REMINDERS_KEY == "po:reminders"

    @pytest.mark.asyncio
    async def test_set_reminder_uses_constant(self):
        """set_reminder should call zadd with PO_REMINDERS_KEY."""
        from src.po.tools import init_po_clients, set_reminder

        mock_client = AsyncMock()
        mock_client.redis = AsyncMock()
        mock_api = AsyncMock()
        init_po_clients(mock_api, mock_client)

        await set_reminder.ainvoke(
            {"delay_minutes": 5, "reason": "test"},
            config={"configurable": {"thread_id": "po-user-user-1", "user_id": "user-1"}},
        )

        call_args = mock_client.redis.zadd.call_args
        assert call_args[0][0] == PO_REMINDERS_KEY
