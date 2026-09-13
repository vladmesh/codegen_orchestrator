"""The administrator audience of a terminal notification is owed and settled on its own.

A park owes the owner and administrators in one record. Each audience keeps its
own persisted state and bounded attempts, so one audience retrying, exhausting or
being voided never resends or loses the other, across any number of scheduler
restarts. Every tick below builds a fresh API client and stream, as a restarted
process would; only the stored record survives between them.

The administrator channel is the production one: the real
`shared.notifications.deliver_to_admins` reads the users API and calls
`send_telegram_message`, which is faked only at its own contract — it returns
`False` for a failed send, as rate limiting, non-200 answers and timeouts do.
"""

from __future__ import annotations

from datetime import UTC, datetime
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from shared.contracts.dto.owner_notification import OwnerNotification, OwnerNotificationState
from shared.contracts.dto.story import StoryStatus
import shared.notifications as notifications_mod
from src.tasks.owner_notifications import (
    OWNER_NOTIFICATION_MAX_ATTEMPTS,
    supervise_owed_owner_notifications,
)

STORY_ID = "story-1"
PROJECT_ID = "00000000-0000-0000-0000-000000000001"
ADMIN_TEXT = "Engineering infrastructure refusal parked story story-1."
ADMINS = (5001, 5002)


def _record(
    *,
    owner: OwnerNotificationState = OwnerNotificationState.OWED,
    admin: OwnerNotificationState | None = OwnerNotificationState.OWED,
) -> dict:
    return OwnerNotification(
        event="story_blocked",
        text="Your story needs an operator.",
        story_id=STORY_ID,
        project_id=PROJECT_ID,
        terminal_status=StoryStatus.WAITING_HUMAN_REVIEW,
        state=owner,
        owed_at=datetime.now(UTC),
        admin_text=None if admin is None else ADMIN_TEXT,
        admin_state=admin,
    ).model_dump(mode="json")


class _World:
    """What survives a restart: the stored record, the owner stream and Telegram."""

    def __init__(self, record: dict) -> None:
        self.record = record
        self.story_status = StoryStatus.WAITING_HUMAN_REVIEW
        self.publish_failures = 0
        self.admin_ids: tuple[int, ...] = ADMINS
        #: Per tick, the administrators Telegram refuses (send returns False).
        self.refusing: list[set[int]] = []
        self.users_api_failures = 0
        self.published: list[dict] = []
        #: Every accepted Telegram send, as (telegram_id, text).
        self.telegram: list[tuple[int, str]] = []
        self.telegram_calls = 0

    @property
    def stored(self) -> OwnerNotification:
        return OwnerNotification.model_validate(self.record)

    def admin_messages(self, telegram_id: int | None = None) -> list[str]:
        return [
            text
            for chat, text in self.telegram
            if (telegram_id is None or chat == telegram_id) and ADMIN_TEXT in text
        ]

    def _api(self) -> AsyncMock:
        api = AsyncMock()
        api.list_runs_owing_owner_notification.return_value = []

        async def owed_stories(*, limit: int):
            stored = self.stored
            if stored.owed or stored.admin_owed:
                return [SimpleNamespace(id=STORY_ID, owner_notification=self.record)]
            return []

        async def write(story_id: str, record: dict) -> None:
            assert story_id == STORY_ID
            self.record = record

        async def story(story_id: str):
            return SimpleNamespace(id=story_id, status=self.story_status)

        api.list_stories_owing_owner_notification.side_effect = owed_stories
        api.update_story_owner_notification.side_effect = write
        api.get_story.side_effect = story
        api.get_project.return_value = SimpleNamespace(owner_id=7)
        api.get_user.return_value = SimpleNamespace(telegram_id=4242)
        return api

    def _redis(self) -> AsyncMock:
        redis = AsyncMock()

        async def publish(_queue: str, fields: dict) -> None:
            if self.publish_failures:
                self.publish_failures -= 1
                raise ConnectionError("po:input is unreachable")
            self.published.append(fields)

        redis.publish_flat.side_effect = publish
        return redis

    def users_handler(self, request: httpx.Request) -> httpx.Response:
        if self.users_api_failures:
            self.users_api_failures -= 1
            return httpx.Response(503, json={"detail": "unavailable"})
        users = [
            {
                "id": index,
                "telegram_id": telegram_id,
                "is_admin": True,
                "created_at": "2026-09-13T00:00:00Z",
            }
            for index, telegram_id in enumerate(self.admin_ids, start=1)
        ]
        users.append({"id": 99, "telegram_id": 4242, "created_at": "2026-09-13T00:00:00Z"})
        return httpx.Response(200, json=users)

    async def send_telegram_message(
        self, telegram_id: int, text: str, parse_mode: str = "Markdown"
    ) -> bool:
        self.telegram_calls += 1
        refusing = self.refusing[0] if self.refusing else set()
        if telegram_id in refusing:
            return False
        self.telegram.append((telegram_id, text))
        return True

    async def tick(self) -> dict[str, int]:
        try:
            return await supervise_owed_owner_notifications(self._api(), self._redis())
        finally:
            if self.refusing:
                self.refusing.pop(0)


@pytest.fixture
def world(monkeypatch):
    world = _World(_record())
    real_client = httpx.AsyncClient

    def client_factory(**kwargs):
        return real_client(transport=httpx.MockTransport(world.users_handler), **kwargs)

    notifications_mod._config = None
    monkeypatch.setattr(notifications_mod, "send_telegram_message", world.send_telegram_message)
    monkeypatch.setattr("shared.clients.internal_api.httpx.AsyncClient", client_factory)
    monkeypatch.setattr("src.tasks.owner_notifications.notify_admins_best_effort", AsyncMock())
    monkeypatch.setattr("src.tasks._recipients.notify_admins_best_effort", AsyncMock())
    env = {
        "TELEGRAM_BOT_TOKEN": "0000000000:test-token",
        "API_BASE_URL": "http://api:8000",
        "INTERNAL_API_KEY": "test-internal-key",
    }
    with patch.dict(os.environ, env):
        yield world
    notifications_mod._config = None


@pytest.mark.asyncio
async def test_audiences_retry_independently_across_restarts_and_settle_once(world):
    world.publish_failures = 1
    world.refusing = [set(ADMINS), set(ADMINS)]

    await world.tick()
    assert (world.stored.state, world.stored.attempts) == (OwnerNotificationState.OWED, 1)
    assert (world.stored.admin_state, world.stored.admin_attempts) == (
        OwnerNotificationState.OWED,
        1,
    )

    await world.tick()
    assert world.stored.state is OwnerNotificationState.DELIVERED
    assert (world.stored.admin_state, world.stored.admin_attempts) == (
        OwnerNotificationState.OWED,
        2,
    )

    await world.tick()
    assert world.stored.admin_state is OwnerNotificationState.DELIVERED
    assert world.stored.admin_attempts == 3
    assert world.stored.admin_detail is None

    await world.tick()
    await world.tick()
    assert len(world.published) == 1
    assert [len(world.admin_messages(admin)) for admin in ADMINS] == [1, 1]
    assert world.stored.attempts == 2


@pytest.mark.asyncio
async def test_telegram_refusing_every_administrator_stays_owed_then_settles(world):
    """Production failures return False; that is not a delivery."""
    world.record = _record(owner=OwnerNotificationState.DELIVERED)
    world.refusing = [set(ADMINS)]

    counts = await world.tick()

    assert counts["retrying"] == 1
    assert world.stored.admin_state is OwnerNotificationState.OWED
    assert world.stored.admin_attempts == 1
    assert world.stored.admin_detail == "failed: Telegram accepted 0 of 2 configured administrators"
    assert world.admin_messages() == []

    counts = await world.tick()

    assert counts["delivered"] == 1
    assert world.stored.admin_state is OwnerNotificationState.DELIVERED
    assert world.stored.admin_attempts == 2
    assert world.stored.state is OwnerNotificationState.DELIVERED
    assert world.published == []


@pytest.mark.asyncio
async def test_a_partial_delivery_is_a_failed_attempt_and_retries_the_audience(world):
    world.record = _record(owner=OwnerNotificationState.DELIVERED)
    world.refusing = [{ADMINS[0]}]

    await world.tick()

    assert world.stored.admin_state is OwnerNotificationState.OWED
    assert world.stored.admin_attempts == 1
    assert (
        world.stored.admin_detail == "partial: Telegram accepted 1 of 2 configured administrators"
    )

    await world.tick()

    assert world.stored.admin_state is OwnerNotificationState.DELIVERED
    # At-least-once: the administrator reached by the partial attempt is told again.
    assert [len(world.admin_messages(admin)) for admin in ADMINS] == [1, 2]


@pytest.mark.asyncio
async def test_no_configured_administrator_is_unaddressable_not_delivered_or_retried(world):
    world.admin_ids = ()

    counts = await world.tick()

    assert counts["delivered"] == 1
    assert world.stored.state is OwnerNotificationState.DELIVERED
    assert world.stored.admin_state is OwnerNotificationState.UNADDRESSABLE
    assert world.stored.admin_attempts == 1
    assert world.stored.admin_detail == (
        "unaddressable: Telegram accepted 0 of 0 configured administrators"
    )

    world.admin_ids = ADMINS
    assert await world.tick() == dict.fromkeys(counts, 0)
    assert world.telegram_calls == 0
    assert len(world.published) == 1


@pytest.mark.asyncio
async def test_a_failing_users_read_spends_an_attempt(world):
    world.record = _record(owner=OwnerNotificationState.DELIVERED)
    world.users_api_failures = 1

    await world.tick()

    assert world.stored.admin_state is OwnerNotificationState.OWED
    assert world.stored.admin_attempts == 1
    assert "HTTP 503" in world.stored.admin_detail
    assert world.telegram_calls == 0


@pytest.mark.asyncio
async def test_exhausted_false_returns_abandon_and_never_resend_the_settled_owner(world):
    world.refusing = [set(ADMINS)] * (OWNER_NOTIFICATION_MAX_ATTEMPTS + 5)

    for _ in range(OWNER_NOTIFICATION_MAX_ATTEMPTS + 2):
        await world.tick()

    assert world.stored.state is OwnerNotificationState.DELIVERED
    assert world.stored.attempts == 1
    assert world.stored.admin_state is OwnerNotificationState.ABANDONED
    assert world.stored.admin_attempts == OWNER_NOTIFICATION_MAX_ATTEMPTS
    assert world.stored.admin_detail == "failed: Telegram accepted 0 of 2 configured administrators"
    assert world.telegram_calls == OWNER_NOTIFICATION_MAX_ATTEMPTS * len(ADMINS)
    assert len(world.published) == 1
    assert world.admin_messages() == []


@pytest.mark.asyncio
async def test_an_exhausted_owner_audience_does_not_lose_the_admin_notice(world):
    world.publish_failures = OWNER_NOTIFICATION_MAX_ATTEMPTS + 5
    world.refusing = [set(ADMINS)]

    for _ in range(OWNER_NOTIFICATION_MAX_ATTEMPTS + 2):
        await world.tick()

    assert world.stored.state is OwnerNotificationState.ABANDONED
    assert world.stored.admin_state is OwnerNotificationState.DELIVERED
    assert [len(world.admin_messages(admin)) for admin in ADMINS] == [1, 1]
    assert world.published == []


@pytest.mark.asyncio
async def test_the_admin_notice_survives_a_story_that_already_moved_on(world):
    world.story_status = StoryStatus.IN_PROGRESS

    await world.tick()
    await world.tick()

    assert world.stored.state is OwnerNotificationState.VOIDED
    assert world.stored.admin_state is OwnerNotificationState.DELIVERED
    assert world.published == []
    assert [len(world.admin_messages(admin)) for admin in ADMINS] == [1, 1]


@pytest.mark.asyncio
async def test_an_admin_only_obligation_publishes_nothing_to_the_owner(world):
    world.record = _record(owner=OwnerNotificationState.DELIVERED)

    counts = await world.tick()

    assert counts["delivered"] == 1
    assert world.published == []
    assert [len(world.admin_messages(admin)) for admin in ADMINS] == [1, 1]
    assert await world.tick() == dict.fromkeys(counts, 0)


@pytest.mark.asyncio
async def test_a_released_record_without_an_admin_audience_keeps_its_owner_meaning(world):
    released = _record(admin=None)
    for key in ("admin_text", "admin_state", "admin_attempts", "admin_detail"):
        released.pop(key)
    world.record = released

    await world.tick()
    await world.tick()

    assert world.stored.state is OwnerNotificationState.DELIVERED
    assert world.stored.admin_state is None
    assert len(world.published) == 1
    assert world.telegram_calls == 0
