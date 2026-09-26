"""A failed planning attempt is a story state, and an operator re-runs it without SQL.

Canary 2, story-92b433c8: an OpenRouter 402 failed the architect's planning,
the story stayed ``in_progress`` with nothing behind it, and recovery needed a
direct SQL ``UPDATE``. These tests go through the real database: the outcome,
the retry count, the park with its owed notices, and the operator's
``retry-planning`` all commit on the locked story row.
"""

from http import HTTPStatus
import json

from fastapi import status
from httpx import AsyncClient
import pytest
from redis.asyncio import Redis

from shared.contracts.dto.owner_notification import OwnerNotification, OwnerNotificationState
from shared.contracts.dto.story import StoryStatus
from shared.contracts.vocab import OwnerNotificationEvent
from shared.queues import ARCHITECT_QUEUE

CAUSE = "LLMChannelsExhausted: every LLM channel failed: openrouter:payment_required"
FAILURE = {"code": "planning_failed", "source": "architect", "detail": CAUSE}
#: `supervisor.story_max_architect_retries` as `scripts/system_configs.yaml` seeds it.
MAX_RETRIES = 3


async def _started_story(client: AsyncClient, project_id: str) -> str:
    created = await client.post("/api/stories/", json={"project_id": project_id, "title": "Plan"})
    assert created.status_code == HTTPStatus.CREATED, created.text
    story_id = created.json()["id"]
    started = await client.post(f"/api/stories/{story_id}/start", json={"actor": "architect"})
    assert started.status_code == HTTPStatus.OK, started.text
    return story_id


async def _report(client: AsyncClient, story_id: str, **fields) -> dict:
    body = {"outcome": "failed", "failure": FAILURE} | fields
    resp = await client.post(f"/api/stories/{story_id}/planning-outcome", json=body)
    assert resp.status_code == HTTPStatus.OK, resp.text
    return resp.json()


async def _architect_messages(redis_client: Redis, story_id: str) -> list[dict]:
    entries = await redis_client.xrange(ARCHITECT_QUEUE)
    messages = [json.loads(fields[b"data"]) for _, fields in entries]
    return [message for message in messages if message["story_id"] == story_id]


@pytest.mark.asyncio
async def test_transient_failures_retry_up_to_the_bound_then_park_with_the_notices(
    async_client: AsyncClient, _tasks_project
):
    story_id = await _started_story(async_client, _tasks_project)

    for attempt in range(1, MAX_RETRIES + 1):
        body = await _report(async_client, story_id, channel_failures=["codex:rate_limited"])
        assert body["status"] == StoryStatus.IN_PROGRESS
        assert body["quarantine_reason"] is None
        assert body["planning"]["state"] == "retrying"
        assert body["planning"]["failed_attempts"] == attempt
        assert body["planning"]["next_attempt_at"] is not None

    parked = await _report(async_client, story_id)

    assert parked["status"] == StoryStatus.WAITING_HUMAN_REVIEW
    assert parked["waiting_on"] == "human_review"
    assert parked["planning"]["state"] == "parked"
    assert parked["planning"]["failed_attempts"] == MAX_RETRIES + 1
    assert parked["quarantine_reason"]["reason"] == "story_failure"
    assert parked["quarantine_reason"]["code"] == "planning_failed"
    assert parked["quarantine_reason"]["detail"] == CAUSE
    record = OwnerNotification.model_validate(
        (await async_client.get(f"/api/stories/{story_id}/owner-notification")).json()
    )
    assert record.event is OwnerNotificationEvent.STORY_BLOCKED
    assert record.state is OwnerNotificationState.OWED
    assert record.admin_state is OwnerNotificationState.OWED
    assert "could not plan the work" in record.text
    diagnostics = (await async_client.get(f"/api/stories/{story_id}/diagnostics")).json()
    assert diagnostics["failure"]["code"] == "planning_failed"


@pytest.mark.asyncio
async def test_an_unretriable_failure_parks_at_once(async_client: AsyncClient, _tasks_project):
    story_id = await _started_story(async_client, _tasks_project)

    parked = await _report(async_client, story_id, retriable=False)

    assert parked["status"] == StoryStatus.WAITING_HUMAN_REVIEW
    assert parked["planning"]["failed_attempts"] == 1


@pytest.mark.asyncio
async def test_a_success_records_which_channels_planned_the_story(
    async_client: AsyncClient, _tasks_project
):
    story_id = await _started_story(async_client, _tasks_project)
    await _report(async_client, story_id)

    resp = await async_client.post(
        f"/api/stories/{story_id}/planning-outcome",
        json={"outcome": "succeeded", "channels": ["claude"], "channel_failures": []},
    )

    assert resp.status_code == HTTPStatus.OK, resp.text
    stored = (await async_client.get(f"/api/stories/{story_id}")).json()["planning"]
    assert stored["state"] == "planned"
    assert stored["channels"] == ["claude"]
    assert stored["failed_attempts"] == 0


@pytest.mark.asyncio
async def test_retry_planning_returns_a_parked_story_to_the_architect_once(
    async_client: AsyncClient, redis_client: Redis, _tasks_project
):
    story_id = await _started_story(async_client, _tasks_project)
    await _report(async_client, story_id, retriable=False)

    resp = await async_client.post(
        f"/api/stories/{story_id}/retry-planning", json={"actor": "operator"}
    )

    assert resp.status_code == HTTPStatus.OK, resp.text
    body = resp.json()
    assert body["status"] == StoryStatus.IN_PROGRESS
    assert body["waiting_on"] == "none"
    assert body["quarantine_reason"] is None
    assert body["planning"] is None
    [message] = await _architect_messages(redis_client, story_id)
    assert message["is_reopen"] is False

    # The count starts again: the next failure is the first of a fresh bound.
    again = await _report(async_client, story_id)
    assert again["planning"]["state"] == "retrying"
    assert again["planning"]["failed_attempts"] == 1


@pytest.mark.asyncio
async def test_retry_planning_refuses_a_story_not_parked_by_a_planning_failure(
    async_client: AsyncClient, redis_client: Redis, _tasks_project
):
    in_progress = await _started_story(async_client, _tasks_project)
    scaffold_parked = await _started_story(async_client, _tasks_project)
    parked = await async_client.post(
        f"/api/stories/{scaffold_parked}/human-review",
        json={
            "actor": "architect",
            "failure": {"code": "scaffold_timeout", "source": "architect", "detail": "slow"},
        },
    )
    assert parked.status_code == HTTPStatus.OK, parked.text

    for story_id in (in_progress, scaffold_parked):
        resp = await async_client.post(
            f"/api/stories/{story_id}/retry-planning", json={"actor": "operator"}
        )
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, resp.text
        assert await _architect_messages(redis_client, story_id) == []


@pytest.mark.asyncio
async def test_a_stale_failure_does_not_park_a_story_that_moved_on(
    async_client: AsyncClient, _tasks_project
):
    story_id = await _started_story(async_client, _tasks_project)
    moved = await async_client.post(f"/api/stories/{story_id}/deploy", json={"actor": "test"})
    assert moved.status_code == HTTPStatus.OK, moved.text

    resp = await async_client.post(
        f"/api/stories/{story_id}/planning-outcome",
        json={"outcome": "failed", "failure": FAILURE},
    )

    assert resp.status_code == HTTPStatus.CONFLICT
    stored = (await async_client.get(f"/api/stories/{story_id}")).json()
    assert stored["status"] == StoryStatus.DEPLOYING
    assert stored["planning"] is None
