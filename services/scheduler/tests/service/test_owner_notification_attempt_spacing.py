"""An owed owner notification gets one attempt per delivery interval, whoever asks.

Two code paths attempt a record: the routing that owed it makes its in-tick
attempt, and the recovery sweep re-attempts whatever is still owed. They used
to be kept from attempting the same record twice in one cycle only by where the
dispatcher calls them. These tests drive the real scheduler seam against the
real API and Postgres, in both orders and concurrently, for a record on a Run
and a record on a Story, and count what reached ``po:input``.

``po:input`` is a stand-in that refuses every publish unless told otherwise, so
the record stays owed after an attempt and only the spacing can stop a second
one. The API decides the spacing on its own clock; a later cycle is reached by
ageing the record's ``last_attempt_at`` by one interval directly in Postgres —
the one thing this test does to the record that the API does not.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import asyncpg
import httpx
import pytest
import structlog

from shared.contracts.dto.owner_notification import (
    OWNER_NOTIFICATION_ATTEMPT_INTERVAL,
    OWNER_NOTIFICATION_KEY,
    OwnerNotification,
    OwnerNotificationState,
)
from shared.contracts.dto.story import StoryStatus
from shared.contracts.vocab import OwnerNotificationEvent
from shared.queues import PO_INPUT_QUEUE

logger = structlog.get_logger(__name__)

SOURCES = ("run", "story")


class _PoInput:
    """``po:input`` as the seam meets it: every publish attempt is counted."""

    def __init__(self, *, refuse: bool = True) -> None:
        self.refuse = refuse
        self.attempts: list[str] = []
        self.published: list[str] = []

    async def publish_flat(self, queue: str, fields: dict) -> None:
        assert queue == PO_INPUT_QUEUE
        self.attempts.append(fields["story_id"])
        if self.refuse:
            raise ConnectionError("po:input is unreachable")
        self.published.append(fields["story_id"])

    def attempts_for(self, story_id: str) -> int:
        return self.attempts.count(story_id)


class _Owed:
    """One owed record on a Run or a Story, reached only through the real API."""

    def __init__(self, api_client, kind: str, source_id: str, story_id: str) -> None:
        self.api_client = api_client
        self.kind = kind
        self.source_id = source_id
        self.story_id = story_id

    @property
    def story_record(self) -> bool:
        return self.kind == "story"

    async def record(self) -> OwnerNotification:
        if self.story_record:
            return await self.api_client.get_story_owner_notification(self.source_id)
        run = await self.api_client.get_run(self.source_id)
        return OwnerNotification.model_validate(run.run_metadata[OWNER_NOTIFICATION_KEY])

    async def routing(self, po: _PoInput, record: OwnerNotification | None = None):
        """Routing's in-tick attempt: the record it owed, handed straight to the seam."""
        from src.tasks.owner_notifications import deliver_owed_notification

        return await deliver_owed_notification(
            self.api_client,
            po,
            self.source_id,
            record or await self.record(),
            logger.bind(story_id=self.story_id),
            story_record=self.story_record,
        )

    async def age_by_one_interval(self) -> None:
        """Move the API's view of this record one delivery interval into the past."""
        stamp = (await self.record()).last_attempt_at
        assert stamp is not None
        aged = json.dumps((stamp - OWNER_NOTIFICATION_ATTEMPT_INTERVAL).isoformat())
        connection = await asyncpg.connect(os.environ["TEST_DATABASE_URL"])
        try:
            if self.story_record:
                await connection.execute(
                    "UPDATE stories SET owner_notification = jsonb_set("
                    "owner_notification::jsonb, '{last_attempt_at}', $2::jsonb)::json "
                    "WHERE id = $1",
                    self.source_id,
                    aged,
                )
            else:
                await connection.execute(
                    "UPDATE runs SET metadata = jsonb_set("
                    "metadata::jsonb, '{owner_notification,last_attempt_at}', $2::jsonb)::json "
                    "WHERE id = $1",
                    self.source_id,
                    aged,
                )
        finally:
            await connection.close()


async def _sweep(api_client, po: _PoInput) -> None:
    from src.tasks.owner_notifications import supervise_owed_owner_notifications

    await supervise_owed_owner_notifications(api_client, po)


async def _owed(api_client, kind: str) -> _Owed:
    """A story that really reached the ending, and the record that owes its owner."""
    from src.tasks.owner_notifications import owe_owner_notification

    headers = {"X-Internal-Key": os.environ["INTERNAL_API_KEY"]}
    telegram_id = uuid.uuid4().int % 1_000_000_000
    project_id = str(uuid.uuid4())
    async with httpx.AsyncClient(
        base_url=api_client.base_url, headers=headers, timeout=30.0
    ) as client:
        user = await client.post(
            "/api/users/",
            json={"telegram_id": telegram_id, "username": f"spacing_{telegram_id}"},
        )
        assert user.status_code == httpx.codes.CREATED, user.text
        project = await client.post(
            "/api/projects/",
            json={
                "id": project_id,
                "initiating_run_id": "test-run-1",
                "title": "Owner notification spacing",
                "config": {},
            },
            headers={**headers, "X-Telegram-ID": str(telegram_id)},
        )
        assert project.status_code == httpx.codes.CREATED, project.text
        story = await client.post(
            "/api/stories/", json={"project_id": project_id, "title": "Space the attempts"}
        )
        assert story.status_code == httpx.codes.CREATED, story.text
        story_id = story.json()["id"]
        assert (await client.post(f"/api/stories/{story_id}/start")).status_code == 200

        if kind == "story":
            # The completion transaction owes the message on the story.
            completed = await client.post(f"/api/stories/{story_id}/complete")
            assert completed.status_code == httpx.codes.OK, completed.text
            return _Owed(api_client, kind, story_id, story_id)

        run_id = f"deploy-spacing-{uuid.uuid4().hex[:12]}"
        created = await client.post(
            "/api/runs/",
            json={
                "id": run_id,
                "type": "deploy",
                "project_id": project_id,
                "story_id": story_id,
                "run_metadata": {},
            },
        )
        assert created.status_code == httpx.codes.CREATED, created.text
        await owe_owner_notification(
            api_client,
            await api_client.get_run(run_id),
            event=OwnerNotificationEvent.STORY_QUARANTINED,
            text="The story needs a human to look at it.",
            story_id=story_id,
            project_id=project_id,
            terminal_status=StoryStatus.WAITING_HUMAN_REVIEW,
            log=logger,
        )
        parked = await client.post(f"/api/stories/{story_id}/human-review")
        assert parked.status_code == httpx.codes.OK, parked.text
        return _Owed(api_client, kind, run_id, story_id)


async def _assert_one_attempt(owed: _Owed, po: _PoInput) -> None:
    record = await owed.record()
    assert po.attempts_for(owed.story_id) == 1
    assert record.state is OwnerNotificationState.OWED
    assert record.attempts == 1
    assert record.last_attempt_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", SOURCES)
async def test_sweep_then_routing_attempts_once(api_client, kind):
    from src.tasks.owner_notifications import OwnerNotificationOutcome

    owed = await _owed(api_client, kind)
    po = _PoInput()

    await _sweep(api_client, po)
    assert await owed.routing(po) is OwnerNotificationOutcome.NOT_DUE

    await _assert_one_attempt(owed, po)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", SOURCES)
async def test_routing_then_sweep_attempts_once(api_client, kind):
    from src.tasks.owner_notifications import OwnerNotificationOutcome

    owed = await _owed(api_client, kind)
    po = _PoInput()

    assert await owed.routing(po) is OwnerNotificationOutcome.RETRYING
    await _sweep(api_client, po)

    await _assert_one_attempt(owed, po)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", SOURCES)
async def test_concurrent_sweep_and_routing_attempt_once(api_client, kind):
    owed = await _owed(api_client, kind)
    po = _PoInput()

    await asyncio.gather(_sweep(api_client, po), owed.routing(po))

    await _assert_one_attempt(owed, po)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", SOURCES)
async def test_two_concurrent_sweeps_attempt_once(api_client, kind):
    owed = await _owed(api_client, kind)
    po = _PoInput()

    await asyncio.gather(_sweep(api_client, po), _sweep(api_client, po))

    await _assert_one_attempt(owed, po)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", SOURCES)
async def test_concurrent_callers_publish_a_delivered_message_once(api_client, kind):
    owed = await _owed(api_client, kind)
    po = _PoInput(refuse=False)
    record = await owed.record()

    await asyncio.gather(_sweep(api_client, po), _sweep(api_client, po), owed.routing(po, record))

    assert po.published.count(owed.story_id) == 1
    settled = await owed.record()
    assert settled.state is OwnerNotificationState.DELIVERED
    assert settled.attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", SOURCES)
async def test_spaced_cycles_exhaust_the_bound_and_call_a_human_once(api_client, kind, monkeypatch):
    from src.tasks.owner_notifications import OWNER_NOTIFICATION_MAX_ATTEMPTS

    alerts: list[str] = []

    async def _alert(message, level="info", **context):
        alerts.append(message)

    monkeypatch.setattr("src.tasks.owner_notifications.notify_admins_best_effort", _alert)
    owed = await _owed(api_client, kind)
    po = _PoInput()
    # Routing keeps the copy it owed; a later cycle's routing would hand in the same.
    stale = await owed.record()

    for cycle in range(OWNER_NOTIFICATION_MAX_ATTEMPTS + 2):
        await asyncio.gather(_sweep(api_client, po), owed.routing(po, stale))
        record = await owed.record()
        assert po.attempts_for(owed.story_id) == min(cycle + 1, OWNER_NOTIFICATION_MAX_ATTEMPTS)
        assert record.attempts == min(cycle + 1, OWNER_NOTIFICATION_MAX_ATTEMPTS)
        if record.owed:
            await owed.age_by_one_interval()

    assert record.state is OwnerNotificationState.ABANDONED
    assert record.attempts == OWNER_NOTIFICATION_MAX_ATTEMPTS
    escalations = [alert for alert in alerts if f"story={owed.story_id}" in alert]
    assert len(escalations) == 1
    assert f"undelivered after {OWNER_NOTIFICATION_MAX_ATTEMPTS} attempts" in escalations[0]
    source = f"source=story:{owed.source_id}" if kind == "story" else f"run={owed.source_id}"
    assert source in escalations[0]
