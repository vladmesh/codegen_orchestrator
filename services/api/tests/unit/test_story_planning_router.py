"""`POST /stories/{id}/planning-outcome` and `POST /stories/{id}/retry-planning`.

The handlers run over a real `Story` row object and a session double; the
database-backed proofs (row lock, owed notices, one commit) are in
`tests/service/test_story_planning_failure.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock
import uuid

from fakeredis import aioredis
from fastapi import status
from httpx import ASGITransport, AsyncClient
from internal_caller import INTERNAL_HEADERS
import pytest

from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.story_planning import (
    PLANNING_MAX_RETRIES_CONFIG_KEY,
    StoryPlanning,
    StoryPlanningState,
)
from shared.models import Story, SystemConfig
from src.database import get_async_session
from src.dependencies import get_redis_client
from src.main import app

PROJECT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
CAUSE = "LLMChannelsExhausted: every LLM channel failed: openrouter:payment_required"
FAILURE = {"code": "planning_failed", "source": "architect", "detail": CAUSE}


def _story(status: StoryStatus = StoryStatus.IN_PROGRESS, **fields) -> Story:
    now = datetime.now(UTC)
    base = {
        "id": "story-92b433c8",
        "project_id": PROJECT_ID,
        "title": "Recipe bot",
        "type": "product",
        "status": status.value,
        "waiting_on": "human_review" if status is StoryStatus.WAITING_HUMAN_REVIEW else "none",
        "priority": 0,
        "created_by": "po",
        "unverified_decisions": [],
        "created_at": now,
        "updated_at": now,
    }
    return Story(**(base | fields))


def _session(story: Story, max_retries: int = 3) -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = story
    session.execute = AsyncMock(return_value=result)

    async def get(model, key, **_kwargs):
        assert model is SystemConfig
        assert key == PLANNING_MAX_RETRIES_CONFIG_KEY
        return SystemConfig(key=key, value=max_retries, category="supervisor")

    session.get = get
    session.refresh = AsyncMock()

    async def override():
        yield session

    app.dependency_overrides[get_async_session] = override
    return session


@pytest.fixture(autouse=True)
def _cleanup_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def redis():
    """The stream client the API holds, over fakeredis, to prove nothing touches it."""
    client = AsyncMock()
    client.redis = aioredis.FakeRedis(decode_responses=True)
    app.dependency_overrides[get_redis_client] = lambda: client
    return client


async def _post(path: str, body: dict | None = None):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        return await client.post(path, json=body)


def _failed(retriable: bool = True, **fields) -> dict:
    return {"outcome": "failed", "failure": FAILURE, "retriable": retriable} | fields


# --- planning-outcome -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_retriable_failure_keeps_the_story_in_progress_and_schedules_a_retry():
    story = _story()
    session = _session(story)

    resp = await _post(
        f"/api/stories/{story.id}/planning-outcome",
        _failed(channel_failures=["codex:rate_limited"]),
    )

    assert resp.status_code == HTTPStatus.OK, resp.text
    body = resp.json()
    assert body["status"] == "in_progress"
    assert body["quarantine_reason"] is None
    assert body["planning"]["state"] == "retrying"
    assert body["planning"]["failed_attempts"] == 1
    assert body["planning"]["max_retries"] == 3
    assert body["planning"]["next_attempt_at"] is not None
    assert body["planning"]["last_failure"]["detail"] == CAUSE
    assert body["planning"]["channel_failures"] == ["codex:rate_limited"]
    assert story.owner_notification is None
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_failure_past_the_bound_parks_the_story_with_its_reason_and_notices():
    story = _story()
    _session(story, max_retries=1)

    first = await _post(f"/api/stories/{story.id}/planning-outcome", _failed())
    second = await _post(f"/api/stories/{story.id}/planning-outcome", _failed())

    assert first.json()["planning"]["state"] == "retrying"
    body = second.json()
    assert body["status"] == "waiting_human_review"
    assert body["waiting_on"] == "human_review"
    assert body["planning"]["state"] == "parked"
    assert body["planning"]["failed_attempts"] == 2
    assert body["quarantine_reason"]["code"] == "planning_failed"
    assert body["quarantine_reason"]["detail"] == CAUSE
    notice = story.owner_notification
    assert notice["event"] == "story_blocked"
    assert notice["state"] == "owed"
    assert notice["admin_state"] == "owed"
    assert "could not plan the work" in notice["text"]
    assert CAUSE in notice["admin_text"]


@pytest.mark.asyncio
async def test_an_unretriable_failure_parks_at_once():
    story = _story()
    _session(story)

    resp = await _post(f"/api/stories/{story.id}/planning-outcome", _failed(retriable=False))

    assert resp.json()["status"] == "waiting_human_review"
    assert resp.json()["planning"]["failed_attempts"] == 1


@pytest.mark.asyncio
async def test_a_reopen_that_failed_before_it_started_is_parked_through_in_progress():
    story = _story(StoryStatus.REOPENED)
    _session(story)

    resp = await _post(
        f"/api/stories/{story.id}/planning-outcome", _failed(retriable=False, reopen=True)
    )

    assert resp.json()["status"] == "waiting_human_review"
    assert resp.json()["planning"]["reopen"] is True


@pytest.mark.asyncio
async def test_a_stale_failure_of_a_story_that_moved_on_is_refused():
    story = _story(StoryStatus.DEPLOYING)
    session = _session(story)

    resp = await _post(f"/api/stories/{story.id}/planning-outcome", _failed())

    assert resp.status_code == HTTPStatus.CONFLICT
    assert story.planning is None
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_success_records_the_channels_and_clears_the_retry():
    story = _story()
    _session(story)
    await _post(f"/api/stories/{story.id}/planning-outcome", _failed())

    resp = await _post(
        f"/api/stories/{story.id}/planning-outcome",
        {"outcome": "succeeded", "channels": ["claude"], "planning_attempt_id": "plan-2"},
    )

    planning = resp.json()["planning"]
    assert planning["state"] == "planned"
    assert planning["failed_attempts"] == 0
    assert planning["channels"] == ["claude"]
    assert planning["planning_attempt_id"] == "plan-2"


@pytest.mark.asyncio
async def test_a_failure_of_another_code_is_refused_by_the_contract():
    story = _story()
    _session(story)

    resp = await _post(
        f"/api/stories/{story.id}/planning-outcome",
        {"outcome": "failed", "failure": FAILURE | {"code": "scaffold_failed"}},
    )

    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


# --- retry-planning -------------------------------------------------------------


@pytest.mark.asyncio
async def _parked_story() -> Story:
    story = _story(user_report="still broken")
    _session(story)
    parked = await _post(
        f"/api/stories/{story.id}/planning-outcome", _failed(retriable=False, reopen=True)
    )
    assert parked.json()["status"] == "waiting_human_review"
    return story


async def _retry(story: Story):
    return await _post(f"/api/stories/{story.id}/retry-planning", {"actor": "admin"})


@pytest.mark.asyncio
async def test_retry_planning_writes_the_due_record_and_publishes_nothing(redis):
    """The record is the whole handoff; the supervisor is the one publisher."""
    story = await _parked_story()

    resp = await _retry(story)

    assert resp.status_code == HTTPStatus.OK, resp.text
    body = resp.json()
    assert body["status"] == "in_progress"
    assert body["waiting_on"] == "none"
    assert body["quarantine_reason"] is None
    planning = StoryPlanning.model_validate(body["planning"])
    assert planning.state is StoryPlanningState.RETRYING
    assert planning.failed_attempts == 0
    assert planning.next_attempt_at <= datetime.now(UTC)
    assert planning.last_failure.detail == CAUSE
    assert planning.reopen is True
    # Nothing published, and no Redis key: not even the supervisor's throttle.
    redis.publish_message.assert_not_awaited()
    assert await redis.redis.keys("*") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "story",
    [
        # Not parked at all.
        _story(StoryStatus.IN_PROGRESS),
        # Parked, but by something that is not a planning failure.
        _story(
            StoryStatus.WAITING_HUMAN_REVIEW,
            quarantine_reason={"reason": "story_failure", **FAILURE, "code": "scaffold_timeout"},
        ),
        _story(StoryStatus.WAITING_HUMAN_REVIEW, quarantine_reason={"qa_failure": {}}),
        # A planning failure that already failed the story for good.
        _story(StoryStatus.FAILED, quarantine_reason={"reason": "story_failure", **FAILURE}),
    ],
    ids=["in_progress", "scaffold_timeout", "qa_quarantine", "failed"],
)
async def test_retry_planning_refuses_any_other_state(redis, story):
    session = _session(story)

    resp = await _post(f"/api/stories/{story.id}/retry-planning", {"actor": "admin"})

    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert "planning_failed" in resp.json()["detail"]
    session.commit.assert_not_awaited()
    redis.publish_message.assert_not_awaited()
