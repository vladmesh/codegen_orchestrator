"""Delivery contract for PO proactive notifications.

A notification the user never received has to be visible: delivery retries a
bounded number of times, and exhaustion raises an admin alert carrying the
identifiers needed to find the story it belonged to.
"""

from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.queues.po import POProactiveMessage
from src.proactive import PROACTIVE_MAX_ATTEMPTS, deliver_proactive_message


def _message(**overrides) -> POProactiveMessage:
    fields = {
        "text": "Your project is deployed",
        "telegram_chat_id": "987654321",
        "owner_user_id": "1",
        "event": "story_completed",
        "story_id": "story-7",
        "project_id": "proj-3",
    }
    fields.update(overrides)
    return POProactiveMessage(**fields)


@pytest.fixture(autouse=True)
def _no_sleep():
    """Retries back off in production; the test does not have to wait for it."""
    with patch("src.proactive.asyncio.sleep", new=AsyncMock()):
        yield


class TestBoundedRetry:
    @pytest.mark.asyncio
    async def test_delivered_on_first_attempt_sends_once(self):
        bot = AsyncMock()

        delivered = await deliver_proactive_message(bot, _message())

        assert delivered is True
        assert bot.send_message.await_count == 1
        assert bot.send_message.await_args.kwargs["chat_id"] == 987654321  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_transient_failure_is_retried_and_then_delivered(self):
        bot = AsyncMock()
        # Every attempt tries HTML first and falls back to plain text, so a
        # failing attempt costs two calls. The first attempt fails both ways.
        bot.send_message.side_effect = [
            Exception("telegram 502"),
            Exception("telegram 502"),
            None,
        ]

        with patch("src.proactive.notify_admins_best_effort", new=AsyncMock()) as alert:
            delivered = await deliver_proactive_message(bot, _message())

        assert delivered is True
        alert.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_permanent_failure_stops_after_max_attempts(self):
        bot = AsyncMock()
        bot.send_message.side_effect = Exception("chat not found")

        with patch("src.proactive.notify_admins_best_effort", new=AsyncMock()):
            delivered = await deliver_proactive_message(bot, _message())

        assert delivered is False
        # Two calls per attempt (HTML, then the plain-text fallback), and the
        # loop stops instead of retrying forever.
        assert bot.send_message.await_count == PROACTIVE_MAX_ATTEMPTS * 2


class TestAdminAlertOnExhaustion:
    @pytest.mark.asyncio
    async def test_exhaustion_alerts_admins_with_identifiers(self):
        bot = AsyncMock()
        bot.send_message.side_effect = Exception("chat not found")

        with patch("src.proactive.notify_admins_best_effort", new=AsyncMock()) as alert:
            await deliver_proactive_message(bot, _message())

        alert.assert_awaited_once()
        message = alert.await_args.args[0]
        assert "story-7" in message
        assert "proj-3" in message
        assert "story_completed" in message
        assert "987654321" in message
        assert alert.await_args.kwargs["level"] == "error"

    @pytest.mark.asyncio
    async def test_successful_delivery_raises_no_alert(self):
        bot = AsyncMock()

        with patch("src.proactive.notify_admins_best_effort", new=AsyncMock()) as alert:
            await deliver_proactive_message(bot, _message())

        alert.assert_not_awaited()
