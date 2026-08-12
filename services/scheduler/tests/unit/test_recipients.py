"""Recipient resolution: internal user id in, Telegram chat id out.

The scheduler holds ``Project.owner_id`` — a ``User.id``. Sending that number to
Telegram addresses nothing, so it is resolved before any event is published, and
a recipient that cannot be resolved becomes an admin alert rather than silence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.dto.user import UserDTO
from src.tasks._recipients import resolve_owner_recipient, resolve_project_recipient

OWNER_USER_ID = 1
OWNER_TELEGRAM_ID = 987654321


def _user(user_id: int = OWNER_USER_ID, telegram_id: int = OWNER_TELEGRAM_ID) -> UserDTO:
    return UserDTO(
        id=user_id,
        telegram_id=telegram_id,
        is_admin=False,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.fixture
def api_client():
    client = AsyncMock()
    client.get_project.return_value = SimpleNamespace(owner_id=OWNER_USER_ID)
    client.get_user.return_value = _user()
    return client


class TestResolution:
    @pytest.mark.asyncio
    async def test_project_owner_resolves_to_their_telegram_chat(self, api_client):
        recipient = await resolve_project_recipient(
            api_client, "proj-1", event="story_completed", story_id="story-1"
        )

        assert recipient.telegram_chat_id == str(OWNER_TELEGRAM_ID)
        assert recipient.owner_user_id == str(OWNER_USER_ID)
        assert recipient.is_addressable

    @pytest.mark.asyncio
    async def test_internal_id_is_never_used_as_the_chat(self, api_client):
        recipient = await resolve_project_recipient(api_client, "proj-1", event="story_completed")

        assert recipient.telegram_chat_id != recipient.owner_user_id


class TestUnresolvableRecipientAlertsAdmins:
    @pytest.mark.asyncio
    async def test_project_without_owner_alerts(self, api_client):
        api_client.get_project.return_value = SimpleNamespace(owner_id=None)

        with patch("src.tasks._recipients.notify_admins_best_effort", new=AsyncMock()) as alert:
            recipient = await resolve_project_recipient(
                api_client, "proj-1", event="story_failed", story_id="story-1"
            )

        assert not recipient.is_addressable
        alert.assert_awaited_once()
        message = alert.await_args.args[0]
        assert "story-1" in message
        assert "proj-1" in message
        assert "story_failed" in message

    @pytest.mark.asyncio
    async def test_missing_project_alerts(self, api_client):
        api_client.get_project.return_value = None

        with patch("src.tasks._recipients.notify_admins_best_effort", new=AsyncMock()) as alert:
            recipient = await resolve_project_recipient(api_client, "proj-1", event="story_failed")

        assert not recipient.is_addressable
        alert.assert_awaited_once()
        assert "project not found" in alert.await_args.args[0]

    @pytest.mark.asyncio
    async def test_owner_that_does_not_exist_alerts(self, api_client):
        api_client.get_user.return_value = None

        with patch("src.tasks._recipients.notify_admins_best_effort", new=AsyncMock()) as alert:
            recipient = await resolve_owner_recipient(
                api_client, OWNER_USER_ID, event="story_failed", project_id="proj-1"
            )

        assert not recipient.is_addressable
        # The internal id survives for the alert even though nothing can be sent.
        assert recipient.owner_user_id == str(OWNER_USER_ID)
        alert.assert_awaited_once()
        assert str(OWNER_USER_ID) in alert.await_args.args[0]

    @pytest.mark.asyncio
    async def test_successful_resolution_raises_no_alert(self, api_client):
        with patch("src.tasks._recipients.notify_admins_best_effort", new=AsyncMock()) as alert:
            await resolve_project_recipient(api_client, "proj-1", event="story_completed")

        alert.assert_not_awaited()
