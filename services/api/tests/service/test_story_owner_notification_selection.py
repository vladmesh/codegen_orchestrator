"""Story-backed owner-notification selection is durable API state.

The completion path is deliberately exercised through the public action
endpoint, with no QA run.  The remaining cases mirror the long-lived
run-backed selection suite: recovery is driven by the record state, never by a
story status scan, so every settling state must round-trip through Postgres.
"""

from datetime import UTC, datetime, timedelta
import uuid

from fastapi import status
from httpx import AsyncClient
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.owner_notification import OwnerNotification, OwnerNotificationState
from shared.contracts.dto.story import StoryStatus
from shared.models import Story

OWED_PATH = "/api/stories/owner-notifications/owed"


def _record(state: OwnerNotificationState, story_id: str, project_id: str, **overrides) -> dict:
    return OwnerNotification(
        event="story_completed",
        text="The story is finished. Tell the user the good news that their product is ready.",
        story_id=story_id,
        project_id=project_id,
        terminal_status=StoryStatus.COMPLETED,
        state=state,
        owed_at=datetime.now(UTC) - timedelta(days=30),
        **overrides,
    ).model_dump(mode="json")


async def _story(async_client: AsyncClient, db_session: AsyncSession) -> tuple[str, str]:
    telegram_id = uuid.uuid4().int % 1_000_000_000
    project_id = str(uuid.uuid4())
    user = await async_client.post(
        "/api/users/",
        json={"telegram_id": telegram_id, "username": f"story_owed_{telegram_id}"},
    )
    assert user.status_code == status.HTTP_201_CREATED
    project = await async_client.post(
        "/api/projects/",
        json={
            "initiating_run_id": "test-run-1",
            "id": project_id,
            "title": "Story notification selection",
            "config": {},
        },
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    assert project.status_code == status.HTTP_201_CREATED
    story = await async_client.post(
        "/api/stories/",
        json={"project_id": project_id, "title": "Notify the owner", "type": "technical"},
    )
    assert story.status_code == status.HTTP_201_CREATED
    return story.json()["id"], project_id


async def _completed_story_with_record(
    async_client: AsyncClient,
    db_session: AsyncSession,
    *,
    state: OwnerNotificationState,
    age: timedelta,
    **overrides,
) -> str:
    story_id, project_id = await _story(async_client, db_session)
    stamp = datetime.now(UTC) - age
    await db_session.execute(
        update(Story)
        .where(Story.id == story_id)
        .values(
            status=StoryStatus.COMPLETED.value,
            owner_notification=_record(state, story_id, project_id, **overrides),
            created_at=stamp,
        )
    )
    await db_session.commit()
    return story_id


async def _owed(async_client: AsyncClient, limit: int = 100) -> list[str]:
    response = await async_client.get(OWED_PATH, params={"limit": limit})
    assert response.status_code == status.HTTP_200_OK
    return [row["id"] for row in response.json()]


@pytest.mark.asyncio
async def test_direct_completion_without_qa_persists_and_settles_a_story_notification(
    async_client: AsyncClient, db_session: AsyncSession
):
    story_id, _ = await _story(async_client, db_session)
    started = await async_client.post(f"/api/stories/{story_id}/start")
    assert started.status_code == status.HTTP_200_OK

    completed = await async_client.post(f"/api/stories/{story_id}/complete")
    assert completed.status_code == status.HTTP_200_OK
    assert completed.json()["status"] == StoryStatus.COMPLETED.value

    stored = await async_client.get(f"/api/stories/{story_id}/owner-notification")
    assert stored.status_code == status.HTTP_200_OK
    notification = OwnerNotification.model_validate(stored.json())
    assert notification.state is OwnerNotificationState.OWED
    assert notification.text == (
        "The story is finished. Tell the user the good news that their product is ready."
    )
    assert story_id in await _owed(async_client)

    delivered = notification.model_copy(
        update={"state": OwnerNotificationState.DELIVERED, "attempts": 1}
    )
    settled = await async_client.patch(
        f"/api/stories/{story_id}/owner-notification", json=delivered.model_dump(mode="json")
    )
    assert settled.status_code == status.HTTP_200_OK
    assert (
        OwnerNotification.model_validate(settled.json()).state is OwnerNotificationState.DELIVERED
    )
    assert story_id not in await _owed(async_client)


@pytest.mark.asyncio
async def test_a_month_old_owed_story_notification_is_selected(
    async_client: AsyncClient, db_session: AsyncSession
):
    story_id = await _completed_story_with_record(
        async_client, db_session, state=OwnerNotificationState.OWED, age=timedelta(days=30)
    )

    assert story_id in await _owed(async_client)


@pytest.mark.asyncio
async def test_a_delivered_story_notification_is_not_selected(
    async_client: AsyncClient, db_session: AsyncSession
):
    story_id = await _completed_story_with_record(
        async_client,
        db_session,
        state=OwnerNotificationState.DELIVERED,
        age=timedelta(days=30),
        attempts=1,
    )

    assert story_id not in await _owed(async_client)


@pytest.mark.asyncio
async def test_settled_story_notification_failures_are_not_selected(
    async_client: AsyncClient, db_session: AsyncSession
):
    abandoned = await _completed_story_with_record(
        async_client,
        db_session,
        state=OwnerNotificationState.ABANDONED,
        age=timedelta(hours=1),
        attempts=3,
        detail="ConnectionError",
    )
    unaddressable = await _completed_story_with_record(
        async_client,
        db_session,
        state=OwnerNotificationState.UNADDRESSABLE,
        age=timedelta(hours=1),
        attempts=1,
        detail="recipient unresolved: owner has no telegram id",
    )
    voided = await _completed_story_with_record(
        async_client,
        db_session,
        state=OwnerNotificationState.VOIDED,
        age=timedelta(hours=1),
        detail="story is testing, not completed",
    )

    selected = await _owed(async_client)
    assert abandoned not in selected
    assert unaddressable not in selected
    assert voided not in selected


@pytest.mark.asyncio
async def test_a_story_without_a_notification_is_not_selected(
    async_client: AsyncClient, db_session: AsyncSession
):
    story_id, _ = await _story(async_client, db_session)

    assert story_id not in await _owed(async_client)
    missing = await async_client.get(f"/api/stories/{story_id}/owner-notification")
    assert missing.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
async def test_a_partially_spent_story_notification_stays_selected(
    async_client: AsyncClient, db_session: AsyncSession
):
    story_id = await _completed_story_with_record(
        async_client,
        db_session,
        state=OwnerNotificationState.OWED,
        age=timedelta(minutes=5),
        attempts=1,
        detail="TimeoutError",
    )

    assert story_id in await _owed(async_client)


@pytest.mark.asyncio
async def test_the_oldest_story_notification_is_selected_first(
    async_client: AsyncClient, db_session: AsyncSession
):
    older = await _completed_story_with_record(
        async_client, db_session, state=OwnerNotificationState.OWED, age=timedelta(days=2)
    )
    newer = await _completed_story_with_record(
        async_client, db_session, state=OwnerNotificationState.OWED, age=timedelta(minutes=1)
    )

    selected = await _owed(async_client)
    assert selected.index(older) < selected.index(newer)


@pytest.mark.asyncio
async def test_the_page_bounds_story_notification_selection(
    async_client: AsyncClient, db_session: AsyncSession
):
    for index in range(3):
        await _completed_story_with_record(
            async_client,
            db_session,
            state=OwnerNotificationState.OWED,
            age=timedelta(minutes=index + 1),
        )

    assert len(await _owed(async_client, limit=2)) == 2
