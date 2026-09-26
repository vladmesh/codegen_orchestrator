"""The supervisor re-queues a failed planning attempt once its backoff is over.

The API decided the retry when it recorded the failure, and wrote the count and
`next_attempt_at` on the story. The supervisor only waits that out and
publishes one architect message per failed attempt. These tests drive a real
`SchedulerAPIClient` over `httpx.MockTransport` and a real `RedisStreamClient`
over `fakeredis`, so the published message is read back off the stream.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json

from _run_routing_factories import _make_project, _make_story
from fakeredis import aioredis
import httpx
import pytest

from shared.contracts.dto.story_failure import StoryFailure, StoryFailureCode
from shared.contracts.dto.story_planning import StoryPlanning, StoryPlanningState
from shared.contracts.dto.user import UserDTO
from shared.queues import ARCHITECT_QUEUE
from shared.redis import RedisStreamClient

PROJECT_ID = "00000000-0000-0000-0000-000000000001"


def _planning(state: StoryPlanningState, *, due_in: timedelta, reopen: bool = False):
    return StoryPlanning(
        state=state,
        failed_attempts=1,
        max_retries=3,
        next_attempt_at=datetime.now(UTC) + due_in,
        last_failure=StoryFailure(
            code=StoryFailureCode.PLANNING_FAILED,
            source="architect",
            detail="LLMChannelsExhausted: every LLM channel failed: codex:rate_limited",
        ),
        reopen=reopen,
        recorded_at=datetime.now(UTC),
    )


def _story(story_id: str, status: str, planning: StoryPlanning | None, **kwargs):
    return _make_story(
        id=story_id, project_id=PROJECT_ID, status=status, planning=planning, **kwargs
    )


class _FakeAPI:
    def __init__(self, stories) -> None:
        self._stories = stories

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/stories/":
            status = request.url.params.get("status")
            return httpx.Response(
                200,
                json=[s.model_dump(mode="json") for s in self._stories if s.status == status],
            )
        if path == f"/api/projects/{PROJECT_ID}":
            return httpx.Response(200, json=_make_project().model_dump(mode="json"))
        if path == "/api/users/1":
            user = UserDTO(id=1, telegram_id=4242, created_at=datetime.now(UTC))
            return httpx.Response(200, json=user.model_dump(mode="json"))
        raise AssertionError(f"unexpected API request: {request.method} {request.url}")


@pytest.fixture
def api_factory(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setenv("API_BASE_URL", "http://api.test")

    def build(stories):
        from src.clients.api import SchedulerAPIClient

        client = SchedulerAPIClient()
        client._client = httpx.AsyncClient(
            base_url="http://api.test", transport=httpx.MockTransport(_FakeAPI(stories).handle)
        )
        return client

    return build


@pytest.fixture
def redis_client():
    client = RedisStreamClient(redis_url="redis://localhost:6379/0")
    client._redis = aioredis.FakeRedis(decode_responses=True)
    return client


async def _architect_messages(redis_client: RedisStreamClient) -> list[dict]:
    entries = await redis_client._redis.xrange(ARCHITECT_QUEUE)
    return [json.loads(fields["data"]) for _, fields in entries]


@pytest.mark.asyncio
async def test_a_due_retry_is_queued_once_per_failed_attempt(api_factory, redis_client):
    from src.tasks.supervisor import supervise_stuck_stories

    due = _planning(StoryPlanningState.RETRYING, due_in=timedelta(seconds=-1))
    api = api_factory([_story("story-due", "in_progress", due)])

    first = await supervise_stuck_stories(api, redis_client)
    second = await supervise_stuck_stories(api, redis_client)

    assert first == {"retried": 1, "failed": 0}
    assert second == {"retried": 0, "failed": 0}
    [message] = await _architect_messages(redis_client)
    assert message["story_id"] == "story-due"
    assert message["is_reopen"] is False
    assert message["telegram_chat_id"] == "4242"


@pytest.mark.asyncio
async def test_nothing_is_queued_before_the_backoff_is_over_or_for_other_states(
    api_factory, redis_client
):
    from src.tasks.supervisor import supervise_stuck_stories

    api = api_factory(
        [
            _story(
                "story-waiting",
                "in_progress",
                _planning(StoryPlanningState.RETRYING, due_in=timedelta(minutes=5)),
            ),
            _story(
                "story-planned",
                "in_progress",
                _planning(StoryPlanningState.PLANNED, due_in=timedelta(seconds=-1)),
            ),
            _story(
                "story-parked",
                "waiting_human_review",
                _planning(StoryPlanningState.PARKED, due_in=timedelta(seconds=-1)),
            ),
            _story("story-plain", "in_progress", None),
        ]
    )

    result = await supervise_stuck_stories(api, redis_client)

    assert result == {"retried": 0, "failed": 0}
    assert await _architect_messages(redis_client) == []


@pytest.mark.asyncio
async def test_a_reopen_is_retried_as_a_reopen(api_factory, redis_client):
    from src.tasks.supervisor import supervise_stuck_stories

    due = _planning(StoryPlanningState.RETRYING, due_in=timedelta(seconds=-1), reopen=True)
    api = api_factory([_story("story-reopen", "reopened", due, user_report="still broken")])

    await supervise_stuck_stories(api, redis_client)

    [message] = await _architect_messages(redis_client)
    assert message["story_id"] == "story-reopen"
    assert message["is_reopen"] is True
    assert message["user_report"] == "still broken"
