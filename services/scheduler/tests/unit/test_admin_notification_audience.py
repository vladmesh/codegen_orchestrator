"""The administrator audience of a terminal notification is owed and settled on its own.

A park owes the owner and administrators in one record. Each audience keeps its
own persisted state and bounded attempts, so one audience retrying, exhausting or
being voided never resends or loses the other, across any number of scheduler
restarts. Every tick below builds a fresh API client and stream, as a restarted
process would; only the stored record survives between them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.contracts.dto.owner_notification import OwnerNotification, OwnerNotificationState
from shared.contracts.dto.story import StoryStatus
from src.tasks.owner_notifications import (
    OWNER_NOTIFICATION_MAX_ATTEMPTS,
    supervise_owed_owner_notifications,
)

STORY_ID = "story-1"
PROJECT_ID = "00000000-0000-0000-0000-000000000001"
ADMIN_TEXT = "Engineering infrastructure refusal parked story story-1."


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
    """What survives a restart: the stored record, the owner stream and the admin chat."""

    def __init__(self, record: dict) -> None:
        self.record = record
        self.story_status = StoryStatus.WAITING_HUMAN_REVIEW
        self.publish_failures = 0
        self.admin_failures = 0
        self.published: list[dict] = []
        self.admin_messages: list[str] = []

    @property
    def stored(self) -> OwnerNotification:
        return OwnerNotification.model_validate(self.record)

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

    async def notify_admins(self, message: str, level: str = "info") -> int:
        if self.admin_failures:
            self.admin_failures -= 1
            raise ConnectionError("Telegram is unreachable")
        self.admin_messages.append(message)
        return 1

    async def tick(self) -> dict[str, int]:
        return await supervise_owed_owner_notifications(self._api(), self._redis())


@pytest.fixture
def world(monkeypatch):
    world = _World(_record())
    monkeypatch.setattr("src.tasks.owner_notifications.notify_admins", world.notify_admins)
    monkeypatch.setattr("src.tasks.owner_notifications.notify_admins_best_effort", AsyncMock())
    monkeypatch.setattr("src.tasks._recipients.notify_admins_best_effort", AsyncMock())
    return world


@pytest.mark.asyncio
async def test_audiences_retry_independently_across_restarts_and_settle_once(world):
    world.publish_failures = 1
    world.admin_failures = 2

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

    await world.tick()
    await world.tick()
    assert len(world.published) == 1
    assert world.admin_messages == [ADMIN_TEXT]
    assert world.stored.attempts == 2


@pytest.mark.asyncio
async def test_an_exhausted_admin_audience_never_resends_the_settled_owner(world):
    world.admin_failures = OWNER_NOTIFICATION_MAX_ATTEMPTS + 5

    for _ in range(OWNER_NOTIFICATION_MAX_ATTEMPTS + 2):
        await world.tick()

    assert world.stored.state is OwnerNotificationState.DELIVERED
    assert world.stored.admin_state is OwnerNotificationState.ABANDONED
    assert world.stored.admin_attempts == OWNER_NOTIFICATION_MAX_ATTEMPTS
    assert len(world.published) == 1
    assert world.admin_messages == []


@pytest.mark.asyncio
async def test_an_exhausted_owner_audience_does_not_lose_the_admin_notice(world):
    world.publish_failures = OWNER_NOTIFICATION_MAX_ATTEMPTS + 5
    world.admin_failures = 1

    for _ in range(OWNER_NOTIFICATION_MAX_ATTEMPTS + 2):
        await world.tick()

    assert world.stored.state is OwnerNotificationState.ABANDONED
    assert world.stored.admin_state is OwnerNotificationState.DELIVERED
    assert world.admin_messages == [ADMIN_TEXT]
    assert world.published == []


@pytest.mark.asyncio
async def test_the_admin_notice_survives_a_story_that_already_moved_on(world):
    world.story_status = StoryStatus.IN_PROGRESS

    await world.tick()
    await world.tick()

    assert world.stored.state is OwnerNotificationState.VOIDED
    assert world.stored.admin_state is OwnerNotificationState.DELIVERED
    assert world.published == []
    assert world.admin_messages == [ADMIN_TEXT]


@pytest.mark.asyncio
async def test_an_admin_only_obligation_publishes_nothing_to_the_owner(world):
    world.record = _record(owner=OwnerNotificationState.DELIVERED)

    counts = await world.tick()

    assert counts["delivered"] == 1
    assert world.published == []
    assert world.admin_messages == [ADMIN_TEXT]
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
    assert world.admin_messages == []
