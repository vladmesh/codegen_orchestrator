"""The owed-notification selection answers by the state of the record, not by the story.

A terminal story transition is committed before the owner's message is
published, and it takes the story out of every status the supervisor scans. So
the only thing that can bring a lost message back is a selection keyed on the
record the supervisor wrote before it committed — these tests are what says that
selection exists and what it contains.

Age is deliberately not part of it: a story finished during an outage is owed
its message just as much as one finished a minute ago, which is the same reason
the QA grant selection next door dropped its time window.
"""

from datetime import UTC, datetime, timedelta
import uuid

from fastapi import status
from httpx import AsyncClient
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.owner_notification import (
    OWNER_NOTIFICATION_KEY,
    OwnerNotification,
    OwnerNotificationState,
)
from shared.contracts.dto.story import StoryStatus
from shared.models import Run

OWED_PATH = "/api/runs/owner-notifications/owed"


def _record(state: OwnerNotificationState, story_id: str, **overrides) -> dict:
    record = OwnerNotification(
        event="story_completed",
        text="The story is finished: it is deployed and QA passed.",
        story_id=story_id,
        project_id=str(uuid.uuid4()),
        terminal_status=StoryStatus.COMPLETED,
        state=state,
        owed_at=datetime.now(UTC) - timedelta(days=30),
        **overrides,
    )
    return record.model_dump(mode="json")


async def _qa_run(
    async_client: AsyncClient,
    db_session: AsyncSession,
    *,
    notification: dict | None,
    age: timedelta,
) -> str:
    """A QA run of a given age, carrying the notification record it was given."""
    telegram_id = uuid.uuid4().int % 1_000_000_000
    project_id = str(uuid.uuid4())

    user = await async_client.post(
        "/api/users/",
        json={"telegram_id": telegram_id, "username": f"owed_{telegram_id}"},
    )
    assert user.status_code == status.HTTP_201_CREATED
    project = await async_client.post(
        "/api/projects/",
        json={"id": project_id, "title": "Owed notification selection", "config": {}},
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    assert project.status_code == status.HTTP_201_CREATED

    run_id = f"qa-{uuid.uuid4().hex[:12]}"
    metadata = {"qa_handoff": {"kept": True}}
    if notification is not None:
        metadata[OWNER_NOTIFICATION_KEY] = notification
    created = await async_client.post(
        "/api/runs/",
        json={"id": run_id, "type": "qa", "project_id": project_id, "run_metadata": metadata},
    )
    assert created.status_code == status.HTTP_201_CREATED

    # Aged in place: every test here is about a record that was written long
    # before anything came back for it still being work.
    stamp = datetime.now(UTC) - age
    await db_session.execute(
        update(Run).where(Run.id == run_id).values(created_at=stamp, started_at=stamp)
    )
    await db_session.commit()
    return run_id


async def _owed(async_client: AsyncClient, limit: int = 100) -> list[str]:
    page = await async_client.get(OWED_PATH, params={"limit": limit})
    assert page.status_code == status.HTTP_200_OK
    return [row["id"] for row in page.json()]


@pytest.mark.asyncio
async def test_a_month_old_owed_message_is_still_selected(
    async_client: AsyncClient, db_session: AsyncSession
):
    """The scenario a status scan cannot reach: the story left TESTING long ago."""
    run_id = await _qa_run(
        async_client,
        db_session,
        notification=_record(OwnerNotificationState.OWED, "story-old"),
        age=timedelta(days=30),
    )

    assert run_id in await _owed(async_client)


@pytest.mark.asyncio
async def test_a_delivered_message_is_not_selected(
    async_client: AsyncClient, db_session: AsyncSession
):
    """The owner has been told. Selecting it again is a second message."""
    run_id = await _qa_run(
        async_client,
        db_session,
        notification=_record(OwnerNotificationState.DELIVERED, "story-done", attempts=1),
        age=timedelta(days=30),
    )

    assert run_id not in await _owed(async_client)


@pytest.mark.asyncio
async def test_a_settled_failure_is_not_selected(
    async_client: AsyncClient, db_session: AsyncSession
):
    """The three endings that are not a retry leave the selection for good."""
    abandoned = await _qa_run(
        async_client,
        db_session,
        notification=_record(
            OwnerNotificationState.ABANDONED, "story-gone", attempts=3, detail="ConnectionError"
        ),
        age=timedelta(hours=1),
    )
    unaddressable = await _qa_run(
        async_client,
        db_session,
        notification=_record(
            OwnerNotificationState.UNADDRESSABLE,
            "story-nochat",
            attempts=1,
            detail="recipient unresolved: owner has no telegram id",
        ),
        age=timedelta(hours=1),
    )

    voided = await _qa_run(
        async_client,
        db_session,
        notification=_record(
            OwnerNotificationState.VOIDED,
            "story-uncommitted",
            detail="story is testing, not completed",
        ),
        age=timedelta(hours=1),
    )

    selected = await _owed(async_client)
    assert abandoned not in selected
    assert unaddressable not in selected
    # A record whose transition never landed is settled too: it owes nothing
    # until routing writes the obligation again.
    assert voided not in selected


@pytest.mark.asyncio
async def test_a_run_that_owes_nothing_is_not_selected(
    async_client: AsyncClient, db_session: AsyncSession
):
    run_id = await _qa_run(async_client, db_session, notification=None, age=timedelta(days=30))

    assert run_id not in await _owed(async_client)


@pytest.mark.asyncio
async def test_a_half_spent_record_stays_selected(
    async_client: AsyncClient, db_session: AsyncSession
):
    """An attempt that failed transiently is still owed, and says why."""
    run_id = await _qa_run(
        async_client,
        db_session,
        notification=_record(
            OwnerNotificationState.OWED, "story-retry", attempts=1, detail="TimeoutError"
        ),
        age=timedelta(minutes=5),
    )

    assert run_id in await _owed(async_client)


@pytest.mark.asyncio
async def test_the_longest_waiting_owner_is_served_first(
    async_client: AsyncClient, db_session: AsyncSession
):
    older = await _qa_run(
        async_client,
        db_session,
        notification=_record(OwnerNotificationState.OWED, "story-older"),
        age=timedelta(days=2),
    )
    newer = await _qa_run(
        async_client,
        db_session,
        notification=_record(OwnerNotificationState.OWED, "story-newer"),
        age=timedelta(minutes=1),
    )

    selected = await _owed(async_client)
    assert selected.index(older) < selected.index(newer)


@pytest.mark.asyncio
async def test_the_page_bounds_the_answer(async_client: AsyncClient, db_session: AsyncSession):
    """The scan is bounded by the page, never 'every story'."""
    for index in range(3):
        await _qa_run(
            async_client,
            db_session,
            notification=_record(OwnerNotificationState.OWED, f"story-page-{index}"),
            age=timedelta(minutes=index + 1),
        )

    assert len(await _owed(async_client, limit=2)) == 2
