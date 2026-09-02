"""Recovery of a story stranded behind an incomplete Product Brief plan.

These tests drive the real surfaces on both sides of `supervise_stuck_stories`:
a real `SchedulerAPIClient` over an `httpx.MockTransport` that answers the API
routes `codegen-orchestrator-1241` built, and a real `RedisStreamClient` over
`fakeredis`. So the URL the scheduler asks for, the DTO it parses, the retry
counter it persists and the architect message it publishes are all the ones
production would use — nothing here asserts on a double's call list.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from uuid import UUID

from _run_routing_factories import _make_project, _make_story, _make_task
from fakeredis import aioredis
import httpx
import pytest

from shared.contracts.dto.product_brief import (
    PLANNING_ATTEMPT_HEARTBEAT_TIMEOUT_SECONDS,
    MustRequirement,
    ProductBriefContent,
    ProductBriefRead,
)
from shared.contracts.dto.user import UserDTO
from shared.queues import ARCHITECT_QUEUE
from shared.redis_client import RedisStreamClient

PROJECT_ID = "00000000-0000-0000-0000-000000000001"
STORY_ID = "story-brief-1"
STUCK_AGE = timedelta(minutes=10)


def _make_brief(
    *,
    story_id: str = STORY_ID,
    planning_attempt_active: bool,
    heartbeat_age_seconds: float | None,
    coverage_admitted_at: datetime | None = None,
) -> ProductBriefRead:
    """A brief as `GET /api/product-briefs/by-story/{story_id}` returns it."""
    heartbeat = (
        None
        if heartbeat_age_seconds is None
        else datetime.now(UTC) - timedelta(seconds=heartbeat_age_seconds)
    )
    return ProductBriefRead(
        id="brief-1",
        project_id=UUID(PROJECT_ID),
        story_id=story_id,
        revision=1,
        title="Test brief",
        content=ProductBriefContent(
            summary="A product",
            must_requirements=[MustRequirement(id="r1", text="it must work")],
        ),
        confirmed_at=datetime.now(UTC) - timedelta(hours=1),
        confirmation_request_id="confirm-1",
        coverage_admitted_at=coverage_admitted_at,
        planning_attempt_id="plan-abc" if planning_attempt_active else None,
        planning_attempt_active=planning_attempt_active,
        planning_attempt_heartbeat_at=heartbeat,
    )


class _FakeAPI:
    """The API routes `supervise_stuck_stories` reads, served over httpx."""

    def __init__(self, *, stories, tasks, brief: ProductBriefRead | None) -> None:
        self._stories = stories
        self._tasks = tasks
        self._brief = brief
        #: Every path the scheduler actually asked for, in order.
        self.paths: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        params = request.url.params
        if path == "/api/stories/":
            status = params.get("status")
            return httpx.Response(
                200,
                json=[s.model_dump(mode="json") for s in self._stories if s.status == status],
            )
        if path == "/api/tasks/":
            story_id = params.get("story_id")
            return httpx.Response(
                200,
                json=[t.model_dump(mode="json") for t in self._tasks if t.story_id == story_id],
            )
        if path.startswith("/api/product-briefs/by-story/"):
            story_id = path.rsplit("/", 1)[-1]
            if self._brief is None or self._brief.story_id != story_id:
                return httpx.Response(
                    404, json={"detail": f"Story {story_id} is not backed by a Product Brief"}
                )
            return httpx.Response(200, json=self._brief.model_dump(mode="json"))
        if path == f"/api/projects/{PROJECT_ID}":
            return httpx.Response(200, json=_make_project().model_dump(mode="json"))
        if path == "/api/users/1":
            return httpx.Response(
                200,
                json=UserDTO(id=1, telegram_id=4242, created_at=datetime.now(UTC)).model_dump(
                    mode="json"
                ),
            )
        raise AssertionError(f"unexpected API request: {request.method} {request.url}")


@pytest.fixture
def api_factory(monkeypatch):
    """Build a real SchedulerAPIClient bound to a `_FakeAPI`."""
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setenv("API_BASE_URL", "http://api.test")

    def build(*, stories, tasks, brief):
        from src.clients.api import SchedulerAPIClient

        fake = _FakeAPI(stories=stories, tasks=tasks, brief=brief)
        client = SchedulerAPIClient()
        client._client = httpx.AsyncClient(
            base_url="http://api.test", transport=httpx.MockTransport(fake.handle)
        )
        return client, fake

    return build


@pytest.fixture
def redis_client():
    client = RedisStreamClient(redis_url="redis://localhost:6379/0")
    client._redis = aioredis.FakeRedis(decode_responses=True)
    return client


async def _architect_story_ids(redis_client: RedisStreamClient) -> list[str]:
    """The stories actually published to the architect queue, read off the stream."""
    entries = await redis_client._redis.xrange(ARCHITECT_QUEUE)
    return [json.loads(fields["data"])["story_id"] for _, fields in entries]


def _stuck_story():
    return _make_story(id=STORY_ID, project_id=PROJECT_ID, created_at=datetime.now(UTC) - STUCK_AGE)


def _unadmitted_task(task_id: str = "task-1"):
    """A task planned under an attempt that never reached admission."""
    return _make_task(
        id=task_id,
        project_id=PROJECT_ID,
        story_id=STORY_ID,
        status="backlog",
        dispatch_admitted=False,
        planning_attempt_id="plan-abc",
    )


@pytest.mark.asyncio
async def test_recovers_story_whose_planner_died_mid_plan(api_factory, redis_client):
    """All tasks unadmitted + no live planning attempt -> retried through architect."""
    from src.tasks.supervisor import supervise_stuck_stories

    api_client, fake = api_factory(
        stories=[_stuck_story()],
        tasks=[_unadmitted_task("task-1"), _unadmitted_task("task-2")],
        brief=_make_brief(
            planning_attempt_active=True,
            heartbeat_age_seconds=PLANNING_ATTEMPT_HEARTBEAT_TIMEOUT_SECONDS + 30,
        ),
    )

    result = await supervise_stuck_stories(api_client, redis_client)

    assert result == {"retried": 1, "failed": 0}
    assert await _architect_story_ids(redis_client) == [STORY_ID]
    assert f"/api/product-briefs/by-story/{STORY_ID}" in fake.paths
    # The existing retry budget is the one that applies: the same Redis key, so
    # a recovered story cannot be retried for ever.
    assert await redis_client._redis.get(f"story:architect_retries:{STORY_ID}") == "1"


@pytest.mark.asyncio
async def test_leaves_a_story_with_a_live_planner_alone(api_factory, redis_client):
    """An active attempt with a heartbeat inside the timeout owns the plan."""
    from src.tasks.supervisor import supervise_stuck_stories

    api_client, _ = api_factory(
        stories=[_stuck_story()],
        tasks=[_unadmitted_task()],
        brief=_make_brief(
            planning_attempt_active=True,
            heartbeat_age_seconds=PLANNING_ATTEMPT_HEARTBEAT_TIMEOUT_SECONDS - 10,
        ),
    )

    result = await supervise_stuck_stories(api_client, redis_client)

    assert result == {"retried": 0, "failed": 0}
    assert await _architect_story_ids(redis_client) == []


@pytest.mark.asyncio
async def test_recovers_when_the_attempt_was_closed_without_admission(api_factory, redis_client):
    """A planner that finished its attempt without admitting owns nothing."""
    from src.tasks.supervisor import supervise_stuck_stories

    api_client, _ = api_factory(
        stories=[_stuck_story()],
        tasks=[_unadmitted_task()],
        brief=_make_brief(planning_attempt_active=False, heartbeat_age_seconds=1),
    )

    result = await supervise_stuck_stories(api_client, redis_client)

    assert result == {"retried": 1, "failed": 0}
    assert await _architect_story_ids(redis_client) == [STORY_ID]


@pytest.mark.asyncio
async def test_leaves_a_story_with_an_admitted_task_alone(api_factory, redis_client):
    """One admitted task means the story has left the planning boundary.

    This is also why recovery can never re-dispatch an admitted plan: admission
    releases the whole release set in one transaction, so an admitted brief
    leaves no unadmitted task behind for this to mistake for a corpse.
    """
    from src.tasks.supervisor import supervise_stuck_stories

    admitted = _make_task(
        id="task-admitted",
        project_id=PROJECT_ID,
        story_id=STORY_ID,
        status="todo",
        dispatch_admitted=True,
        planning_attempt_id="plan-abc",
    )
    api_client, fake = api_factory(
        stories=[_stuck_story()],
        tasks=[admitted, _unadmitted_task("task-late")],
        brief=_make_brief(
            planning_attempt_active=False,
            heartbeat_age_seconds=None,
            coverage_admitted_at=datetime.now(UTC),
        ),
    )

    result = await supervise_stuck_stories(api_client, redis_client)

    assert result == {"retried": 0, "failed": 0}
    assert await _architect_story_ids(redis_client) == []
    # Decided from the tasks alone: the story never even asks about the brief.
    assert not [p for p in fake.paths if p.startswith("/api/product-briefs/")]


@pytest.mark.asyncio
async def test_story_without_a_brief_keeps_todays_skip(api_factory, redis_client):
    """An ordinary story with tasks is skipped exactly as it was before."""
    from src.tasks.supervisor import supervise_stuck_stories

    ordinary = _make_task(id="task-plain", project_id=PROJECT_ID, story_id=STORY_ID, status="todo")
    api_client, fake = api_factory(stories=[_stuck_story()], tasks=[ordinary], brief=None)

    result = await supervise_stuck_stories(api_client, redis_client)

    assert result == {"retried": 0, "failed": 0}
    assert await _architect_story_ids(redis_client) == []
    assert not [p for p in fake.paths if p.startswith("/api/product-briefs/")]


@pytest.mark.asyncio
async def test_unadmitted_tasks_without_a_brief_are_not_recovered(api_factory, redis_client):
    """A 404 from `by-story` is "no brief", not "no owner": nothing is retried."""
    from src.tasks.supervisor import supervise_stuck_stories

    api_client, fake = api_factory(stories=[_stuck_story()], tasks=[_unadmitted_task()], brief=None)

    result = await supervise_stuck_stories(api_client, redis_client)

    assert result == {"retried": 0, "failed": 0}
    assert await _architect_story_ids(redis_client) == []
    assert f"/api/product-briefs/by-story/{STORY_ID}" in fake.paths


@pytest.mark.asyncio
async def test_recovery_respects_the_sequential_project_skip(api_factory, redis_client):
    """A project already running a story is left alone, recoverable plan or not."""
    from src.tasks.supervisor import supervise_stuck_stories

    api_client, _ = api_factory(
        stories=[
            _stuck_story(),
            _make_story(id="story-active", project_id=PROJECT_ID, status="in_progress"),
        ],
        tasks=[_unadmitted_task()],
        brief=_make_brief(planning_attempt_active=False, heartbeat_age_seconds=None),
    )

    result = await supervise_stuck_stories(api_client, redis_client)

    assert result == {"retried": 0, "failed": 0}
    assert await _architect_story_ids(redis_client) == []
