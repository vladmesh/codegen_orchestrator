"""A stage notice names the stage the story is in when it is sent, not when it was scanned.

The notice sweep scans the in-work stages first and publishes afterwards.
Routing that moves a story on in between — the deploy supervisor handing a
finished deploy to QA, the QA supervisor completing a passed story — must not
be announced over, whichever of the two runs first or if they run at the same
moment. Until now only the sweep's place at the end of the dispatcher tick held
that. These tests drive the real sweep and the real routing against the real
API, Postgres and Redis, in both orders and concurrently, and read what reached
``po:input`` and the marker Redis holds afterwards.

Every move is set up the same way: a story the sweep has already scanned (its
scan is replayed from before the move), then the report that lets routing move
the story on, then routing and the rest of the sweep in each order. Its re-read
goes to the real API and is recorded, because it is what decides: a notice for
the scanned stage goes out only while the story is still in it. Notices before
routing find it still there and announce it truthfully; notices after routing
find it gone and say nothing; concurrently, whichever the re-read saw. The next
sweep announces the stage the story moved to. A story nothing moves on is
announced once, and not again inside the quiet interval, in the same three
orders.

What the tests do to the world that no production path does: read the story's
stage notices and marker directly, and after each test cancel the QA runs it
left queued or running: the paid-work limit counts every live QA and engineering
Run in the shared database, so a run left in flight here would refuse the next
test's admission.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import os
import uuid

import httpx
import pytest

from shared.contracts.dto.qa_handoff import QA_HANDOFF_KEY, QAHandoffPlan
from shared.contracts.dto.run import RunType
from shared.contracts.dto.story import STAGE_NOTICE_STATUSES, StoryStageNoticeKind, StoryStatus
from shared.contracts.queues.po import POSystemEvent
from shared.contracts.queues.qa import QAMessage
from shared.contracts.vocab import OwnerNotificationEvent
from shared.queues import PO_INPUT_QUEUE
from shared.redis import RedisStreamClient
from shared.tests.ssh_key_fixtures import fleet_private_key
from src.tasks.supervisor import supervise_deploying_stories, supervise_testing_stories
from src.tasks.supervisor.stage_notices import read_stage_notice_marker, supervise_stage_notices

HEAD_SHA = "a" * 40
BUILT_SHA = "b" * 40
DEPLOYED_URL = "http://10.9.0.22:8000"
ORDERS = ("notices_after_routing", "notices_before_routing", "concurrent")
_PAID_RUN_TYPES = frozenset({RunType.QA.value, RunType.ENGINEERING.value})
_LIVE_RUN_STATUSES = frozenset({"queued", "running"})
#: Stories the running test created, so its teardown can settle their paid runs.
_CREATED_STORIES: list[str] = []
_NOTHING = {"entered": 0, "still_there": 0, "unaddressable": 0}


# --- the world around the sweep -------------------------------------------


class _OnlyStory:
    """The real API client, with every status scan narrowed to one story.

    The service database is shared by every test in the session, so a
    supervisor that scans a status must not reach another test's stories.
    Every other call is the real client's.
    """

    def __init__(self, api_client, story_id: str) -> None:
        self._api = api_client
        self._story_id = story_id

    async def get_stories_by_status(self, status) -> list:
        stories = await self._api.get_stories_by_status(status)
        return [story for story in stories if story.id == self._story_id]

    def __getattr__(self, name: str):
        return getattr(self._api, name)


class _ScannedBeforeTheMove:
    """The sweep's scan, replayed from before routing moved the story on.

    The scan found the story in its stage; that answer is what the sweep
    decides on, however much later it announces. The re-read before the
    announcement, and everything the announcement reads, goes to the real API;
    what the re-read answered is kept, since it is what the notice must follow.
    """

    def __init__(self, api_client, story) -> None:
        self._api = api_client
        self._story = story
        self.re_read: list[StoryStatus] = []

    @classmethod
    async def take(cls, api_client, story_id: str, stage: StoryStatus) -> _ScannedBeforeTheMove:
        story = await api_client.get_story(story_id)
        assert story.status is stage
        return cls(api_client, story)

    async def get_stories_by_status(self, status) -> list:
        return [self._story] if StoryStatus(status) is self._story.status else []

    async def get_story(self, story_id: str):
        story = await self._api.get_story(story_id)
        if story_id == self._story.id:
            self.re_read.append(story.status)
        return story

    def __getattr__(self, name: str):
        return getattr(self._api, name)


@dataclass
class _Stay:
    """One story in a stage, the report that moves it on, and routing's pass over it."""

    story_id: str
    stage: StoryStatus
    routing: Callable[[], Awaitable[object]]
    move: Callable[[], Awaitable[object]] | None = None
    moved_to: StoryStatus | None = None


@pytest.fixture
async def redis_client():
    client = RedisStreamClient(os.environ["REDIS_URL"])
    await client.connect()
    try:
        yield client
    finally:
        await client.close()


@pytest.fixture(autouse=True)
async def _settle_paid_runs(api_client):
    """Cancel the live QA runs this test's stories left behind; they hold paid-work slots."""
    _CREATED_STORIES.clear()
    yield
    for story_id in _CREATED_STORIES:
        runs = await _call(api_client, "GET", "runs/", params={"story_id": story_id})
        for run in runs.json():
            if run["type"] in _PAID_RUN_TYPES and run["status"] in _LIVE_RUN_STATUSES:
                await _call(api_client, "PATCH", f"runs/{run['id']}", json={"status": "cancelled"})
    _CREATED_STORIES.clear()


async def _call(api_client, method: str, path: str, **kwargs) -> httpx.Response:
    response = await api_client.request_raw(method, path, **kwargs)
    assert response.is_success, f"{method} {path}: {response.status_code} {response.text}"
    return response


async def _notices(redis_client: RedisStreamClient, story_id: str) -> list[POSystemEvent]:
    """This story's stage notices on ``po:input``, parsed with the contract PO parses with."""
    entries = await redis_client.redis.xrange(PO_INPUT_QUEUE)
    return [
        POSystemEvent.model_validate(fields)
        for _, fields in entries
        if fields.get("event") == OwnerNotificationEvent.STORY_STAGE.value
        and fields.get("story_id") == story_id
    ]


def _said(notices: list[POSystemEvent]) -> list[tuple[StoryStatus, StoryStageNoticeKind]]:
    return [(notice.stage, notice.stage_notice) for notice in notices]


# --- one project, one story ------------------------------------------------


async def _project(api_client) -> str:
    """A project whose owner has a chat, so every due notice is published."""
    suffix = uuid.uuid4().hex[:8]
    telegram_id = uuid.uuid4().int % 1_000_000_000
    await _call(
        api_client,
        "POST",
        "users/",
        json={"telegram_id": telegram_id, "username": f"stage-notice-{telegram_id}"},
    )
    project_id = str(uuid.uuid4())
    await _call(
        api_client,
        "POST",
        "projects/",
        json={
            "id": project_id,
            "title": "Stage notice re-read",
            "initiating_run_id": f"init-{suffix}",
            "status": "active",
            "config": {"workspace_ready": True},
        },
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    return project_id


async def _application(api_client, project_id: str) -> int:
    """The repository and running application a deploy and its QA point at."""
    suffix = uuid.uuid4().hex[:8]
    server = await _call(
        api_client,
        "POST",
        "servers/",
        json={
            "handle": f"stage-notice-{suffix}",
            "host": "stage-notice.test",
            "public_ip": "10.9.0.22",
            "ssh_key": fleet_private_key(),
        },
    )
    repository = await _call(
        api_client,
        "POST",
        "repositories/",
        json={
            "project_id": project_id,
            "name": f"stage-notice-{suffix}",
            "git_url": f"https://github.com/test/stage-notice-{suffix}.git",
        },
    )
    application = await _call(
        api_client,
        "POST",
        "applications/",
        json={
            "repo_id": repository.json()["id"],
            "server_handle": server.json()["handle"],
            "service_name": f"stage-notice-{suffix}",
            "status": "running",
        },
    )
    return application.json()["id"]


async def _story(api_client, project_id: str, *actions: str) -> str:
    story = await _call(
        api_client, "POST", "stories/", json={"project_id": project_id, "title": "Stage"}
    )
    story_id = story.json()["id"]
    _CREATED_STORIES.append(story_id)
    for action in actions:
        await _call(api_client, "POST", f"stories/{story_id}/{action}")
    return story_id


async def _report(api_client, run_id: str, status: str, result: dict) -> None:
    await _call(api_client, "PATCH", f"runs/{run_id}", json={"status": status, "result": result})


# --- the stages, and what moves each of them on ---------------------------


async def _deploying(api_client, redis_client, *, succeeds: bool) -> _Stay:
    """A running deploy; its success hands the story to QA through the deploy supervisor."""
    project_id = await _project(api_client)
    app_id = await _application(api_client, project_id)
    story_id = await _story(api_client, project_id, "start", "deploy")
    run_id = f"deploy-stage-notice-{uuid.uuid4().hex[:12]}"
    await _call(
        api_client,
        "POST",
        "runs/",
        json={
            "id": run_id,
            "type": RunType.DEPLOY.value,
            "project_id": project_id,
            "story_id": story_id,
            "run_metadata": {
                "application_id": app_id,
                "head_sha": HEAD_SHA,
                "deployed_commit_sha": BUILT_SHA,
            },
        },
    )
    await _call(api_client, "PATCH", f"runs/{run_id}", json={"status": "running"})

    async def routing():
        return await supervise_deploying_stories(_OnlyStory(api_client, story_id), redis_client)

    stay = _Stay(story_id, StoryStatus.DEPLOYING, routing)
    if succeeds:
        stay.moved_to = StoryStatus.TESTING
        stay.move = lambda: _report(
            api_client,
            run_id,
            "completed",
            {"deploy_outcome": "success", "deployed_url": DEPLOYED_URL, "application_id": app_id},
        )
    return stay


async def _testing(api_client, redis_client, *, passes: bool) -> _Stay:
    """A running QA Run; a pass completes the story through the QA supervisor."""
    project_id = await _project(api_client)
    app_id = await _application(api_client, project_id)
    story_id = await _story(api_client, project_id, "start", "deploy", "test")
    qa_run_id = f"qa-stage-notice-{uuid.uuid4().hex[:12]}"
    plan = QAHandoffPlan(
        qa_message=QAMessage(
            story_id=story_id,
            project_id=project_id,
            initiating_run_id="stage-notice-init",
            deployed_url=DEPLOYED_URL,
            application_id=app_id,
            acceptance_criteria="the service responds",
            run_id=qa_run_id,
        )
    )
    started = await _call(
        api_client,
        "POST",
        "work-admission/paid-runs",
        json={
            "id": qa_run_id,
            "type": RunType.QA.value,
            "project_id": project_id,
            "story_id": story_id,
            "run_metadata": {
                "application_id": app_id,
                QA_HANDOFF_KEY: plan.model_dump(mode="json"),
            },
        },
    )
    assert started.json()["run_id"] == qa_run_id
    await _call(api_client, "PATCH", f"runs/{qa_run_id}", json={"status": "running"})

    async def routing():
        return await supervise_testing_stories(_OnlyStory(api_client, story_id), redis_client)

    stay = _Stay(story_id, StoryStatus.TESTING, routing)
    if passes:
        stay.moved_to = StoryStatus.COMPLETED
        stay.move = lambda: _report(
            api_client,
            qa_run_id,
            "completed",
            {"qa_outcome": "passed", "deployed_url": DEPLOYED_URL},
        )
    return stay


async def _in_order(order: str, notices, routing) -> None:
    if order == "notices_after_routing":
        await routing()
        await notices()
    elif order == "notices_before_routing":
        await notices()
        await routing()
    else:
        await asyncio.gather(routing(), notices())


# --- the contract ----------------------------------------------------------


MOVES = {
    "deploying_to_testing": lambda api, redis: _deploying(api, redis, succeeds=True),
    "testing_to_completed": lambda api, redis: _testing(api, redis, passes=True),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.parametrize("move", list(MOVES))
async def test_a_stage_the_story_has_left_is_never_announced(api_client, redis_client, move, order):
    stay = await MOVES[move](api_client, redis_client)
    scan = await _ScannedBeforeTheMove.take(api_client, stay.story_id, stay.stage)
    await stay.move()
    counts: dict[str, int] = {}

    async def notices():
        counts.update(await supervise_stage_notices(scan, redis_client))

    await _in_order(order, notices, stay.routing)

    assert (await api_client.get_story(stay.story_id)).status is stay.moved_to
    [seen] = scan.re_read
    if order == "notices_after_routing":
        assert seen is stay.moved_to
    elif order == "notices_before_routing":
        assert seen is stay.stage
    said = _said(await _notices(redis_client, stay.story_id))
    marker = await read_stage_notice_marker(redis_client, stay.story_id)
    if seen is stay.stage:
        # Still there when the notice went out: announcing it was the truth.
        assert counts == {**_NOTHING, "entered": 1}
        assert said == [(stay.stage, StoryStageNoticeKind.ENTERED)]
        assert marker.stage is stay.stage
    else:
        # Gone by the publish: nothing said, and no marker for the old stage.
        assert seen is stay.moved_to
        assert counts == _NOTHING
        assert said == []
        assert marker is None

    # The next sweep scans the story where it is now, and announces that.
    after = await supervise_stage_notices(_OnlyStory(api_client, stay.story_id), redis_client)

    now_said = _said(await _notices(redis_client, stay.story_id))
    marker = await read_stage_notice_marker(redis_client, stay.story_id)
    if stay.moved_to in STAGE_NOTICE_STATUSES:
        assert after == {**_NOTHING, "entered": 1}
        assert now_said == [*said, (stay.moved_to, StoryStageNoticeKind.ENTERED)]
        assert marker.stage is stay.moved_to
    else:
        assert after == _NOTHING
        assert now_said == said
        assert marker is None


STAYS = {
    StoryStatus.DEPLOYING: lambda api, redis: _deploying(api, redis, succeeds=False),
    StoryStatus.TESTING: lambda api, redis: _testing(api, redis, passes=False),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.parametrize("stage", list(STAYS))
async def test_a_story_that_stays_is_announced_once_per_interval(
    api_client, redis_client, stage, order
):
    stay = await STAYS[stage](api_client, redis_client)
    scan = await _ScannedBeforeTheMove.take(api_client, stay.story_id, stay.stage)
    counts: dict[str, int] = {}

    async def notices():
        counts.update(await supervise_stage_notices(scan, redis_client))

    await _in_order(order, notices, stay.routing)

    assert (await api_client.get_story(stay.story_id)).status is stage
    assert scan.re_read == [stage]
    assert counts == {**_NOTHING, "entered": 1}
    assert _said(await _notices(redis_client, stay.story_id)) == [
        (stage, StoryStageNoticeKind.ENTERED)
    ]
    marker = await read_stage_notice_marker(redis_client, stay.story_id)
    assert marker.stage is stage

    # Later sweeps inside the quiet interval, with the old scan and a fresh one alike.
    again = await supervise_stage_notices(scan, redis_client)
    fresh = await supervise_stage_notices(_OnlyStory(api_client, stay.story_id), redis_client)

    assert again == _NOTHING
    assert fresh == _NOTHING
    assert _said(await _notices(redis_client, stay.story_id)) == [
        (stage, StoryStageNoticeKind.ENTERED)
    ]
    assert await read_stage_notice_marker(redis_client, stay.story_id) == marker
