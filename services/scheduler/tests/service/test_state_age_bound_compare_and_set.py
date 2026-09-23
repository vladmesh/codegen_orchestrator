"""The state-age watchdog ends a wait only if the story is still where it saw it.

One watchdog pass reads its stories and their anchors first and ends a wait
afterwards. Routing that moves a story on in between — the deploy supervisor
handing a finished deploy to QA, a re-dispatch making a new Run, a merge, a
saved secret — must win, whichever of the two runs first or if they run at the
same moment. Until now only the watchdog's place at the end of the dispatcher
tick held that. These tests drive the real watchdog and the real routing
against the real API and Postgres, in both orders and concurrently, and read
what the story and ``po:input`` show afterwards.

Every move is set up the same way: an aged wait the watchdog has already read
(its reads are replayed from before the move), then the event that lets routing
move the story on, then routing and the watchdog's ending in each order. A wait
nothing moves on still ends exactly once, with its outcome and one notification,
in the same three orders.

What the tests do to the world that no production path does: age an anchor
directly in Postgres (a Run's ``created_at``, an ask's ``delivered_at``), and
stand in for GitHub's view of the pull request.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import os
import uuid

import asyncpg
import httpx
import pytest

from shared.contracts.dto.owner_notification import OWNER_NOTIFICATION_KEY
from shared.contracts.dto.qa_handoff import QA_HANDOFF_KEY, QAHandoffPlan
from shared.contracts.dto.run import RunType
from shared.contracts.dto.state_wait import STATE_AGE_BOUND_REASON
from shared.contracts.dto.story import StoryStatus
from shared.contracts.queues.qa import QAMessage
from shared.contracts.vocab import OwnerNotificationEvent
from shared.queues import PO_INPUT_QUEUE
from shared.redis import RedisStreamClient
from shared.tests.ssh_key_fixtures import fleet_private_key
from src.tasks.supervisor import (
    supervise_deploying_stories,
    supervise_state_age_bounds,
    supervise_testing_stories,
    supervise_waiting_user_secret_stories,
)

HEAD_SHA = "a" * 40
BUILT_SHA = "b" * 40
PR_NUMBER = 42
#: Ages past every bound in `tests/conftest.py`: deploy 30, QA 60, PR 220 and
#: user secret 1440 minutes.
AGED = {
    StoryStatus.DEPLOYING: timedelta(minutes=45),
    StoryStatus.TESTING: timedelta(minutes=90),
    StoryStatus.PR_REVIEW: timedelta(minutes=300),
    StoryStatus.WAITING_USER_SECRET: timedelta(minutes=1500),
}
ORDERS = ("watchdog_after_routing", "watchdog_before_routing", "concurrent")
_ENDING_EVENTS = {
    OwnerNotificationEvent.STORY_BLOCKED.value,
    OwnerNotificationEvent.STORY_FAILED.value,
}


# --- the world around the watchdog ----------------------------------------


class _PoInput:
    """``po:input`` as the watchdog's delivery meets it: every publish is kept."""

    def __init__(self) -> None:
        self.published: list[dict] = []

    async def publish_flat(self, queue: str, fields: dict) -> None:
        assert queue == PO_INPUT_QUEUE
        self.published.append(fields)

    def events_for(self, story_id: str) -> list[str]:
        return [fields["event"] for fields in self.published if fields["story_id"] == story_id]


class _Admins:
    """The watchdog's administrator notices, by story."""

    def __init__(self) -> None:
        self.stories: list[str | None] = []

    async def __call__(self, message: str, **fields: object) -> None:
        self.stories.append(fields.get("story_id"))


class _PullRequest:
    """GitHub's view of the story's pull request, as the watchdog reads it.

    The first read of a pass is the watchdog's observation and answers what the
    pull request looked like when the pass began; every later read answers what
    it looks like now. A merge in between is exactly what the watchdog's re-read
    before the ending is there to see.
    """

    def __init__(self, updated_at: datetime) -> None:
        self.observed = self._payload(updated_at)
        self.current = dict(self.observed)
        self._reads = 0

    @staticmethod
    def _payload(updated_at: datetime, *, merged_at: datetime | None = None) -> dict:
        stamp = updated_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        return {
            "number": PR_NUMBER,
            "state": "open" if merged_at is None else "closed",
            "merged_at": None if merged_at is None else merged_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "updated_at": stamp,
            "created_at": stamp,
            "head": {"sha": HEAD_SHA},
            "merge_commit_sha": None if merged_at is None else BUILT_SHA,
        }

    def merge(self) -> None:
        now = datetime.now(UTC)
        self.current = self._payload(now, merged_at=now)

    @property
    def merged(self) -> bool:
        return self.current["merged_at"] is not None

    async def get_pull_request(self, owner: str, repo: str, number: int) -> dict:
        assert number == PR_NUMBER
        self._reads += 1
        return dict(self.observed if self._reads == 1 else self.current)


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


class _ReadBeforeTheMove:
    """The watchdog's reads, replayed from before routing moved the story on.

    The pass read the story in its waiting status and the latest Run of the
    anchor type; those two answers are what the pass decides on, however much
    later it asks for the ending. The ending itself, and everything it reads to
    deliver, goes to the real API.
    """

    def __init__(self, api_client, story, run) -> None:
        self._api = api_client
        self._story = story
        self._run = run

    @classmethod
    async def take(cls, api_client, wait: _Wait) -> _ReadBeforeTheMove:
        story = await api_client.get_story(wait.story_id)
        assert story.status == wait.status.value
        run = (
            await api_client.get_latest_run_by_story(wait.story_id, run_type=wait.run_type.value)
            if wait.run_type is not None
            else None
        )
        return cls(api_client, story, run)

    async def get_stories_by_status(self, status) -> list:
        return [self._story] if status == self._story.status else []

    async def get_latest_run_by_story(self, story_id: str, run_type: str | None = None):
        assert story_id == self._story.id
        return self._run

    def __getattr__(self, name: str):
        return getattr(self._api, name)


@dataclass
class _Wait:
    """One aged wait, the event that moves it on, and routing's pass over it."""

    story_id: str
    status: StoryStatus
    run_type: RunType | None
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


@pytest.fixture
def admins(monkeypatch) -> _Admins:
    alerts = _Admins()
    monkeypatch.setattr("src.tasks.supervisor.state_age.notify_admins_best_effort", alerts)
    return alerts


@pytest.fixture
def github(monkeypatch) -> _PullRequest:
    pull_request = _PullRequest(datetime.now(UTC) - AGED[StoryStatus.PR_REVIEW])
    monkeypatch.setattr(
        "src.tasks.supervisor.state_age.GitHubAppClient", lambda *args, **kwargs: pull_request
    )
    return pull_request


async def _call(api_client, method: str, path: str, **kwargs) -> httpx.Response:
    response = await api_client.request_raw(method, path, **kwargs)
    assert response.is_success, f"{method} {path}: {response.status_code} {response.text}"
    return response


async def _sql(statement: str, *args) -> None:
    connection = await asyncpg.connect(os.environ["TEST_DATABASE_URL"])
    try:
        await connection.execute(statement, *args)
    finally:
        await connection.close()


async def _age_run(run_id: str, by: timedelta) -> None:
    """Move a Run's ``created_at`` into the past: the anchor of a Run-bounded wait."""
    await _sql("UPDATE runs SET created_at = created_at - $2::interval WHERE id = $1", run_id, by)


async def _age_ask_delivery(run_id: str, by: timedelta) -> None:
    """Move a delivered ask's ``delivered_at`` into the past: a secret wait's anchor."""
    delivered = (datetime.now(UTC) - by).isoformat()
    await _sql(
        "UPDATE runs SET metadata = jsonb_set(metadata::jsonb, "
        "'{owner_notification,delivered_at}', $2::jsonb)::json WHERE id = $1",
        run_id,
        json.dumps(delivered),
    )


# --- one project, one story ------------------------------------------------


async def _project(api_client) -> tuple[str, int]:
    suffix = uuid.uuid4().hex[:8]
    telegram_id = uuid.uuid4().int % 1_000_000_000
    await _call(
        api_client,
        "POST",
        "users/",
        json={"telegram_id": telegram_id, "username": f"state-age-{telegram_id}"},
    )
    project_id = str(uuid.uuid4())
    await _call(
        api_client,
        "POST",
        "projects/",
        json={
            "id": project_id,
            "title": "State age compare-and-set",
            "initiating_run_id": f"init-{suffix}",
            "status": "active",
            "config": {"workspace_ready": True},
        },
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    return project_id, telegram_id


async def _application(api_client, project_id: str) -> int:
    """The repository and running application a deploy and its QA point at."""
    suffix = uuid.uuid4().hex[:8]
    server = await _call(
        api_client,
        "POST",
        "servers/",
        json={
            "handle": f"state-age-{suffix}",
            "host": "state-age.test",
            "public_ip": "10.9.0.21",
            "ssh_key": fleet_private_key(),
        },
    )
    repository = await _call(
        api_client,
        "POST",
        "repositories/",
        json={
            "project_id": project_id,
            "name": f"state-age-{suffix}",
            "git_url": f"https://github.com/test/state-age-{suffix}.git",
        },
    )
    application = await _call(
        api_client,
        "POST",
        "applications/",
        json={
            "repo_id": repository.json()["id"],
            "server_handle": server.json()["handle"],
            "service_name": f"state-age-{suffix}",
            "status": "running",
        },
    )
    return application.json()["id"]


async def _story(api_client, project_id: str, *actions: str) -> str:
    story = await _call(
        api_client, "POST", "stories/", json={"project_id": project_id, "title": "Wait"}
    )
    story_id = story.json()["id"]
    for action in actions:
        await _call(api_client, "POST", f"stories/{story_id}/{action}")
    return story_id


async def _running_deploy_run(api_client, project_id: str, story_id: str, app_id: int) -> str:
    run_id = f"deploy-state-age-{uuid.uuid4().hex[:12]}"
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
    await _age_run(run_id, AGED[StoryStatus.DEPLOYING])
    return run_id


async def _report(api_client, run_id: str, status: str, result: dict) -> None:
    await _call(api_client, "PATCH", f"runs/{run_id}", json={"status": status, "result": result})


# --- the waits, and what moves each of them on ----------------------------


async def _deploying(api_client, redis_client, *, outcome: str | None) -> _Wait:
    """A deploy Run that has been running past the bound.

    ``success`` hands the story to QA through the deploy supervisor (it leaves
    ``deploying``); ``retry`` has the deploy supervisor re-dispatch it (a new
    Run, the story stays). ``None`` is a deploy nothing reports on.
    """
    project_id, _ = await _project(api_client)
    app_id = await _application(api_client, project_id)
    story_id = await _story(api_client, project_id, "start", "deploy")
    run_id = await _running_deploy_run(api_client, project_id, story_id, app_id)

    async def routing():
        return await supervise_deploying_stories(_OnlyStory(api_client, story_id), redis_client)

    wait = _Wait(story_id, StoryStatus.DEPLOYING, RunType.DEPLOY, routing)
    if outcome == "success":
        wait.moved_to = StoryStatus.TESTING
        wait.move = lambda: _report(
            api_client,
            run_id,
            "completed",
            {
                "deploy_outcome": "success",
                "deployed_url": "http://10.9.0.21:8000",
                "application_id": app_id,
            },
        )
    elif outcome == "retry":
        wait.moved_to = StoryStatus.DEPLOYING
        wait.move = lambda: _report(
            api_client,
            run_id,
            "failed",
            {"deploy_outcome": "retry", "error_details": "the host dropped the connection"},
        )
    return wait


async def _testing(api_client, redis_client, *, passes: bool) -> _Wait:
    """A QA Run that has been running past the bound; a pass completes the story."""
    project_id, _ = await _project(api_client)
    app_id = await _application(api_client, project_id)
    story_id = await _story(api_client, project_id, "start", "deploy", "test")
    qa_run_id = f"qa-state-age-{uuid.uuid4().hex[:12]}"
    plan = QAHandoffPlan(
        qa_message=QAMessage(
            story_id=story_id,
            project_id=project_id,
            initiating_run_id="state-age-init",
            deployed_url="http://10.9.0.21:8000",
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
    await _age_run(qa_run_id, AGED[StoryStatus.TESTING])

    async def routing():
        return await supervise_testing_stories(_OnlyStory(api_client, story_id), redis_client)

    wait = _Wait(story_id, StoryStatus.TESTING, RunType.QA, routing)
    if passes:
        wait.moved_to = StoryStatus.COMPLETED
        wait.move = lambda: _report(
            api_client,
            qa_run_id,
            "completed",
            {"qa_outcome": "passed", "deployed_url": "http://10.9.0.21:8000"},
        )
    return wait


async def _pr_review(api_client, github: _PullRequest, *, merges: bool) -> _Wait:
    """A pull request that stopped moving past the bound; a merge sends it to deploy."""
    project_id, _ = await _project(api_client)
    await _application(api_client, project_id)
    story_id = await _story(api_client, project_id, "start", "pr_review")
    await _call(api_client, "PATCH", f"stories/{story_id}", json={"pr_number": PR_NUMBER})
    scoped = _OnlyStory(api_client, story_id)

    async def routing():
        # The PR poller's writes for a merged pull request whose images are
        # published: the story leaves PR_REVIEW, and the deploy Run is made.
        if not github.merged:
            return 0
        await scoped.transition_story(story_id, "deploy")
        await scoped.create_run(
            {
                "id": f"deploy-poll-{uuid.uuid4().hex[:8]}",
                "type": RunType.DEPLOY.value,
                "project_id": project_id,
                "story_id": story_id,
                "run_metadata": {
                    "triggered_by": "pr_poll",
                    "head_sha": HEAD_SHA,
                    "deployed_commit_sha": BUILT_SHA,
                },
            }
        )
        return 1

    wait = _Wait(story_id, StoryStatus.PR_REVIEW, None, routing)
    if merges:
        wait.moved_to = StoryStatus.DEPLOYING

        async def merge():
            github.merge()

        wait.move = merge
    return wait


async def _waiting_user_secret(api_client, redis_client, *, secret_saved: bool) -> _Wait:
    """A request for a secret delivered past the bound; saving it resumes the deploy."""
    project_id, _ = await _project(api_client)
    app_id = await _application(api_client, project_id)
    story_id = await _story(api_client, project_id, "start", "deploy")
    run_id = await _running_deploy_run(api_client, project_id, story_id, app_id)
    await _report(
        api_client,
        run_id,
        "completed",
        {
            "deploy_outcome": "waiting_for_user_secret",
            "missing_user_secrets": [{"key": "STRIPE_KEY", "description": "Stripe secret key"}],
        },
    )
    scoped = _OnlyStory(api_client, story_id)
    # The real entry into the wait: the ask is owed, the story moves, the ask
    # is delivered — and its delivery is then aged past the bound.
    await supervise_deploying_stories(scoped, redis_client)
    run = await api_client.get_run(run_id)
    assert run.run_metadata[OWNER_NOTIFICATION_KEY]["state"] == "delivered"
    await _age_ask_delivery(run_id, AGED[StoryStatus.WAITING_USER_SECRET])

    async def routing():
        return await supervise_waiting_user_secret_stories(scoped, redis_client)

    wait = _Wait(story_id, StoryStatus.WAITING_USER_SECRET, RunType.DEPLOY, routing)
    if secret_saved:
        wait.moved_to = StoryStatus.DEPLOYING
        wait.move = lambda: _call(
            api_client,
            "POST",
            f"projects/{project_id}/config/secrets",
            json={"secrets": {"STRIPE_KEY": "sk_test_state_age_compare_and_set"}},
        )
    return wait


async def _moved_on(name: str, api_client, redis_client, github) -> _Wait:
    if name == "deploying_to_testing":
        return await _deploying(api_client, redis_client, outcome="success")
    if name == "deploy_redispatched":
        return await _deploying(api_client, redis_client, outcome="retry")
    if name == "testing_to_completed":
        return await _testing(api_client, redis_client, passes=True)
    if name == "pr_review_merged":
        return await _pr_review(api_client, github, merges=True)
    assert name == "user_secret_saved"
    return await _waiting_user_secret(api_client, redis_client, secret_saved=True)


async def _left_waiting(status: StoryStatus, api_client, redis_client, github) -> _Wait:
    if status is StoryStatus.DEPLOYING:
        return await _deploying(api_client, redis_client, outcome=None)
    if status is StoryStatus.TESTING:
        return await _testing(api_client, redis_client, passes=False)
    if status is StoryStatus.PR_REVIEW:
        return await _pr_review(api_client, github, merges=False)
    return await _waiting_user_secret(api_client, redis_client, secret_saved=False)


async def _in_order(order: str, watchdog, routing) -> None:
    if order == "watchdog_after_routing":
        await routing()
        await watchdog()
    elif order == "watchdog_before_routing":
        await watchdog()
        await routing()
    else:
        await asyncio.gather(routing(), watchdog())


async def _story_record(api_client, story_id: str) -> dict | None:
    response = await api_client.request_raw("GET", f"stories/{story_id}/owner-notification")
    if response.status_code == httpx.codes.NOT_FOUND:
        return None
    assert response.is_success, response.text
    return response.json()


# --- the contract ----------------------------------------------------------


MOVES = (
    "deploying_to_testing",
    "deploy_redispatched",
    "testing_to_completed",
    "pr_review_merged",
    "user_secret_saved",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.parametrize("move", MOVES)
async def test_a_story_routing_moves_on_is_never_ended_by_the_watchdog(
    api_client, redis_client, admins, github, move, order
):
    wait = await _moved_on(move, api_client, redis_client, github)
    read_before = await _ReadBeforeTheMove.take(api_client, wait)
    await wait.move()
    po = _PoInput()
    counts: dict[str, int] = {}

    async def watchdog():
        counts.update(await supervise_state_age_bounds(read_before, po))

    await _in_order(order, watchdog, wait.routing)

    assert counts == {"parked": 0, "failed": 0, "skipped": 1}
    story = await api_client.get_story(wait.story_id)
    assert story.status == wait.moved_to.value
    assert (story.quarantine_reason or {}).get("reason") != STATE_AGE_BOUND_REASON
    record = await _story_record(api_client, wait.story_id)
    assert record is None or record["event"] not in _ENDING_EVENTS
    assert po.events_for(wait.story_id) == []
    assert admins.stories == []


_ENDINGS = {
    StoryStatus.DEPLOYING: (StoryStatus.WAITING_HUMAN_REVIEW, "story_blocked"),
    StoryStatus.TESTING: (StoryStatus.WAITING_HUMAN_REVIEW, "story_blocked"),
    StoryStatus.PR_REVIEW: (StoryStatus.WAITING_HUMAN_REVIEW, "story_blocked"),
    StoryStatus.WAITING_USER_SECRET: (StoryStatus.FAILED, "story_failed"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.parametrize("status", list(_ENDINGS))
async def test_a_genuinely_expired_wait_ends_exactly_once(
    api_client, redis_client, admins, github, status, order
):
    wait = await _left_waiting(status, api_client, redis_client, github)
    read_before = await _ReadBeforeTheMove.take(api_client, wait)
    po = _PoInput()
    counts: dict[str, int] = {}

    async def watchdog():
        counts.update(await supervise_state_age_bounds(_OnlyStory(api_client, wait.story_id), po))

    await _in_order(order, watchdog, wait.routing)
    # A later pass that still read the old status asks again: a typed no-op.
    repeat = await supervise_state_age_bounds(read_before, po)

    terminal, event = _ENDINGS[status]
    ending = "failed" if terminal is StoryStatus.FAILED else "parked"
    assert counts == {"parked": 0, "failed": 0, "skipped": 0, ending: 1}
    assert repeat == {"parked": 0, "failed": 0, "skipped": 0}
    story = await api_client.get_story(wait.story_id)
    assert story.status == terminal.value
    reason = story.quarantine_reason
    assert reason["reason"] == STATE_AGE_BOUND_REASON
    assert reason["status"] == status.value
    assert reason["ending"] == ("fail" if terminal is StoryStatus.FAILED else "park")
    record = await _story_record(api_client, wait.story_id)
    assert record["event"] == event
    assert record["terminal_status"] == terminal.value
    assert record["state"] == "delivered"
    assert po.events_for(wait.story_id) == [event]
    assert admins.stories == [wait.story_id]
