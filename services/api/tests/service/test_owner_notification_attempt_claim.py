"""One owner-notification delivery attempt per interval, decided on the locked row.

The claim is the only place the spacing is decided: owed, and last attempted at
least ``OWNER_NOTIFICATION_ATTEMPT_INTERVAL`` ago, checked and stamped in one
transaction. These tests hold it to that against Postgres for both homes a
record can have, including callers that ask at the same moment, records written
before the stamp existed, and a write from an attempt that has been superseded.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import uuid

from fastapi import status
from httpx import AsyncClient
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.owner_notification import (
    OWNER_NOTIFICATION_ATTEMPT_INTERVAL,
    OWNER_NOTIFICATION_ATTEMPT_SUPERSEDED,
    OWNER_NOTIFICATION_KEY,
    OwnerNotification,
    OwnerNotificationAttemptClaim,
    OwnerNotificationState,
)
from shared.contracts.dto.story import StoryStatus
from shared.models import Run, Story

SOURCES = ("run", "story")


def _legacy_record(story_id: str, project_id: str, **overrides) -> dict:
    """A record as production stored it before ``last_attempt_at`` existed."""
    record = OwnerNotification(
        event="story_completed",
        text="The story is finished.",
        story_id=story_id,
        project_id=project_id,
        terminal_status=StoryStatus.COMPLETED,
        state=OwnerNotificationState.OWED,
        owed_at=datetime.now(UTC),
        **overrides,
    ).model_dump(mode="json")
    del record["last_attempt_at"]
    return record


class _Source:
    def __init__(self, client: AsyncClient, kind: str, source_id: str) -> None:
        self.client = client
        self.kind = kind
        self.source_id = source_id

    @property
    def base(self) -> str:
        return f"/api/{'runs' if self.kind == 'run' else 'stories'}/{self.source_id}"

    async def claim(self) -> OwnerNotificationAttemptClaim:
        response = await self.client.post(f"{self.base}/owner-notification/attempt")
        assert response.status_code == status.HTTP_200_OK, response.text
        return OwnerNotificationAttemptClaim.model_validate(response.json())

    async def write(self, record: OwnerNotification):
        payload = record.model_dump(mode="json")
        if self.kind == "run":
            return await self.client.patch(
                self.base, json={"run_metadata": {OWNER_NOTIFICATION_KEY: payload}}
            )
        return await self.client.patch(f"{self.base}/owner-notification", json=payload)

    async def stored(self) -> OwnerNotification:
        if self.kind == "run":
            response = await self.client.get(self.base)
            return OwnerNotification.model_validate(
                response.json()["run_metadata"][OWNER_NOTIFICATION_KEY]
            )
        response = await self.client.get(f"{self.base}/owner-notification")
        return OwnerNotification.model_validate(response.json())


async def _source(
    async_client: AsyncClient,
    db_session: AsyncSession,
    project_id: str,
    kind: str,
    record: dict | None,
) -> _Source:
    story = await async_client.post(
        "/api/stories/", json={"project_id": project_id, "title": "Claim an attempt"}
    )
    assert story.status_code == status.HTTP_201_CREATED, story.text
    story_id = story.json()["id"]
    if kind == "story":
        await db_session.execute(
            update(Story)
            .where(Story.id == story_id)
            .values(status=StoryStatus.COMPLETED.value, owner_notification=record)
        )
        await db_session.commit()
        return _Source(async_client, kind, story_id)
    run_id = f"deploy-claim-{uuid.uuid4().hex[:12]}"
    metadata = {} if record is None else {OWNER_NOTIFICATION_KEY: record}
    created = await async_client.post(
        "/api/runs/",
        json={
            "id": run_id,
            "type": "deploy",
            "project_id": project_id,
            "story_id": story_id,
            "run_metadata": metadata,
        },
    )
    assert created.status_code == status.HTTP_201_CREATED, created.text
    return _Source(async_client, kind, run_id)


async def _age(db_session: AsyncSession, source: _Source) -> None:
    """Put the last attempt one full interval further back, as a later cycle sees it."""
    record = await source.stored()
    aged = record.model_copy(
        update={"last_attempt_at": record.last_attempt_at - OWNER_NOTIFICATION_ATTEMPT_INTERVAL}
    ).model_dump(mode="json")
    if source.kind == "story":
        statement = update(Story).where(Story.id == source.source_id)
        await db_session.execute(statement.values(owner_notification=aged))
    else:
        metadata = (await source.client.get(source.base)).json()["run_metadata"]
        statement = update(Run).where(Run.id == source.source_id)
        await db_session.execute(
            statement.values({Run.run_metadata: {**metadata, OWNER_NOTIFICATION_KEY: aged}})
        )
    await db_session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", SOURCES)
async def test_a_record_written_before_the_stamp_existed_is_granted_and_stamped(
    async_client: AsyncClient, db_session: AsyncSession, _tasks_project, kind
):
    source = await _source(
        async_client, db_session, _tasks_project, kind, _legacy_record("s", _tasks_project)
    )

    claim = await source.claim()

    assert claim.granted is True
    assert claim.notification.last_attempt_at is not None
    assert (await source.stored()).last_attempt_at == claim.notification.last_attempt_at


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", SOURCES)
async def test_a_second_claim_inside_the_interval_is_refused_and_changes_nothing(
    async_client: AsyncClient, db_session: AsyncSession, _tasks_project, kind
):
    source = await _source(
        async_client, db_session, _tasks_project, kind, _legacy_record("s", _tasks_project)
    )
    granted = await source.claim()

    refused = await source.claim()

    assert refused.granted is False
    assert refused.notification == granted.notification
    assert await source.stored() == granted.notification


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", SOURCES)
async def test_a_claim_an_interval_later_is_granted_again(
    async_client: AsyncClient, db_session: AsyncSession, _tasks_project, kind
):
    source = await _source(
        async_client, db_session, _tasks_project, kind, _legacy_record("s", _tasks_project)
    )
    await source.claim()
    await _age(db_session, source)

    again = await source.claim()

    assert again.granted is True


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", SOURCES)
async def test_concurrent_claims_grant_exactly_one(
    async_client: AsyncClient, db_session: AsyncSession, _tasks_project, kind
):
    source = await _source(
        async_client, db_session, _tasks_project, kind, _legacy_record("s", _tasks_project)
    )

    claims = await asyncio.gather(*(source.claim() for _ in range(5)))

    granted = [claim for claim in claims if claim.granted]
    assert len(granted) == 1
    assert (await source.stored()).last_attempt_at == granted[0].notification.last_attempt_at


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", SOURCES)
async def test_a_settled_or_missing_record_is_never_granted(
    async_client: AsyncClient, db_session: AsyncSession, _tasks_project, kind
):
    settled = await _source(
        async_client,
        db_session,
        _tasks_project,
        kind,
        _legacy_record("s", _tasks_project, state=OwnerNotificationState.DELIVERED, attempts=1),
    )
    missing = await _source(async_client, db_session, _tasks_project, kind, None)

    refused = await settled.claim()
    absent = await missing.claim()

    assert refused.granted is False
    assert refused.notification.state is OwnerNotificationState.DELIVERED
    assert refused.notification.last_attempt_at is None
    assert absent == OwnerNotificationAttemptClaim(granted=False, notification=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", SOURCES)
async def test_a_write_from_a_superseded_attempt_is_refused(
    async_client: AsyncClient, db_session: AsyncSession, _tasks_project, kind
):
    source = await _source(
        async_client, db_session, _tasks_project, kind, _legacy_record("s", _tasks_project)
    )
    first = (await source.claim()).notification
    await _age(db_session, source)
    second = (await source.claim()).notification

    stale = await source.write(
        first.model_copy(update={"state": OwnerNotificationState.OWED, "attempts": 1})
    )
    unstamped = await source.write(second.model_copy(update={"last_attempt_at": None}))
    current = await source.write(
        second.model_copy(update={"state": OwnerNotificationState.DELIVERED, "attempts": 1})
    )

    assert stale.status_code == status.HTTP_409_CONFLICT, stale.text
    assert stale.json()["detail"]["code"] == OWNER_NOTIFICATION_ATTEMPT_SUPERSEDED
    assert unstamped.status_code == status.HTTP_409_CONFLICT, unstamped.text
    assert current.status_code == status.HTTP_200_OK, current.text
    assert (await source.stored()).state is OwnerNotificationState.DELIVERED


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", SOURCES)
async def test_a_record_owed_afresh_replaces_a_stamped_one(
    async_client: AsyncClient, db_session: AsyncSession, _tasks_project, kind
):
    """A voided ending that became real is a new obligation, starting from scratch."""
    source = await _source(
        async_client, db_session, _tasks_project, kind, _legacy_record("s", _tasks_project)
    )
    stamped = (await source.claim()).notification
    await source.write(stamped.model_copy(update={"state": OwnerNotificationState.VOIDED}))

    fresh = OwnerNotification.model_validate(_legacy_record("s", _tasks_project))
    written = await source.write(fresh)

    assert written.status_code == status.HTTP_200_OK, written.text
    stored = await source.stored()
    assert stored.state is OwnerNotificationState.OWED
    assert stored.last_attempt_at is None
    assert (await source.claim()).granted is True
