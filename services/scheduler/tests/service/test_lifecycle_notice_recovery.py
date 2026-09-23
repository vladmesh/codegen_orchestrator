"""A lifecycle wait's announcement survives a failure after the state change.

Parking a task in ``waiting_resources``, resuming it, and parking a story in
``waiting_user_secret`` each used to publish the owner's message after the
transition committed, best-effort. A transient Redis or recipient failure there
lost it: nothing scans for a wait whose announcement is missing. The moves now
commit the owed record with the state change, the routing tick spends one
attempt through the owner-notification seam, and the ``owner_notifications``
loop recovers whatever that attempt missed.

These tests drive the real supervisors and the real sweep against the real API
and Postgres, with ``po:input`` and the owner lookup standing in so they can
fail on demand, and count what reached ``po:input`` for the test's own story.
A later cycle is reached by ageing the record's ``last_attempt_at`` by one
interval directly in Postgres. What else the tests do that production does not:
insert the refused engineering Run directly (the API refuses to create one
outside admission), and treat capacity as available when the resume asks. The
paid-work limit counts every live QA and engineering Run in the shared
database, so each test's teardown cancels any it left live.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
import uuid

from _owner_notification_clock import age_last_attempt_by_one_interval
import asyncpg
import httpx
import pytest

from shared.contracts.dto.owner_notification import (
    OWNER_NOTIFICATION_KEY,
    OwnerNotification,
    OwnerNotificationState,
)
from shared.contracts.dto.run import RunType
from shared.contracts.dto.task import TaskStatus
from shared.queues import PO_INPUT_QUEUE
from shared.tests.ssh_key_fixtures import fleet_private_key

_PAID_RUN_TYPES = frozenset({RunType.QA.value, RunType.ENGINEERING.value})
_LIVE_RUN_STATUSES = frozenset({"queued", "running"})
_SETTLED_TASK_STATUSES = frozenset({TaskStatus.DONE.value, TaskStatus.CANCELLED.value})
#: Stories the running test created, so its teardown can settle their paid runs.
_CREATED_STORIES: list[str] = []


class _PoInput:
    """``po:input``: refuses the next ``failures`` publishes, keeps the rest."""

    def __init__(self) -> None:
        self.failures = 0
        self.published: list[dict] = []
        # The deploy supervisor hands its raw client to retry routing, which a
        # secret wait never reaches.
        self._redis = None

    async def publish_flat(self, queue: str, fields: dict) -> None:
        assert queue == PO_INPUT_QUEUE
        if self.failures:
            self.failures -= 1
            raise ConnectionError("po:input is unreachable")
        self.published.append(fields)

    async def publish_message(self, queue: str, message: object) -> str:
        return "1-1"

    def events_for(self, story_id: str) -> list[str]:
        return [fields["event"] for fields in self.published if fields["story_id"] == story_id]


class _Scoped:
    """The real API client, narrowed to one story's rows, with a failing owner lookup.

    The service database is shared by every test in the session, so a
    supervisor that scans a status must not reach another test's stories or
    tasks. ``user_lookup_failures`` makes the next owner lookups raise, the
    recipient-resolution failure the seam must survive. Every other call is the
    real client's.
    """

    def __init__(self, api_client, story_id: str) -> None:
        self._api = api_client
        self._story_id = story_id
        self.user_lookup_failures = 0

    async def get_stories_by_status(self, status) -> list:
        stories = await self._api.get_stories_by_status(status)
        return [story for story in stories if story.id == self._story_id]

    async def get_tasks_by_status(self, status) -> list:
        tasks = await self._api.get_tasks_by_status(status)
        return [task for task in tasks if task.story_id == self._story_id]

    async def get_user(self, user_id):
        if self.user_lookup_failures:
            self.user_lookup_failures -= 1
            raise httpx.ConnectError("users API is unreachable")
        return await self._api.get_user(user_id)

    def __getattr__(self, name: str):
        return getattr(self._api, name)


@pytest.fixture(autouse=True)
async def _settle_paid_runs(api_client):
    """Take this test's tasks out of dispatch and cancel the paid runs they left live.

    A resumed task is left in ``todo``, and later suites run the real
    ``dispatch_todo_tasks`` against the shared database: a task left there would
    be admitted by their tick and hold a paid-work slot their own admission
    needs. So every task of a story this test created is cancelled first.
    """
    _CREATED_STORIES.clear()
    yield
    for story_id in _CREATED_STORIES:
        tasks = await _call(api_client, "GET", "tasks/", params={"story_id": story_id})
        for task in tasks.json():
            if task["status"] not in _SETTLED_TASK_STATUSES:
                await _call(api_client, "DELETE", f"tasks/{task['id']}")
        runs = await _call(api_client, "GET", "runs/", params={"story_id": story_id})
        for run in runs.json():
            if run["type"] in _PAID_RUN_TYPES and run["status"] in _LIVE_RUN_STATUSES:
                await _call(api_client, "PATCH", f"runs/{run['id']}", json={"status": "cancelled"})
    _CREATED_STORIES.clear()


@pytest.fixture(autouse=True)
def _capacity_is_available(monkeypatch):
    """The resume's admission re-check is not what these tests are about."""

    async def available(api_client, metadata):
        return True

    monkeypatch.setattr("src.tasks.supervisor.liveness._resources_available", available)


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


async def _project(api_client) -> str:
    telegram_id = uuid.uuid4().int % 1_000_000_000
    await _call(
        api_client,
        "POST",
        "users/",
        json={"telegram_id": telegram_id, "username": f"lifecycle-{telegram_id}"},
    )
    project_id = str(uuid.uuid4())
    await _call(
        api_client,
        "POST",
        "projects/",
        json={
            "id": project_id,
            "title": "Lifecycle notice recovery",
            "initiating_run_id": f"init-{uuid.uuid4().hex[:8]}",
            "status": "active",
            "config": {"workspace_ready": True},
        },
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    return project_id


async def _story(api_client, project_id: str, *actions: str) -> str:
    story = await _call(
        api_client, "POST", "stories/", json={"project_id": project_id, "title": "Wait"}
    )
    story_id = story.json()["id"]
    _CREATED_STORIES.append(story_id)
    for action in actions:
        await _call(api_client, "POST", f"stories/{story_id}/{action}")
    return story_id


# --- the resource wait -------------------------------------------------------


async def _refused_task(api_client) -> tuple[str, str, str]:
    """A story task whose engineering Run was refused for capacity: (story, task, run)."""
    project_id = await _project(api_client)
    story_id = await _story(api_client, project_id)
    task = await _call(
        api_client,
        "POST",
        "tasks/",
        json={"project_id": project_id, "story_id": story_id, "title": "Wait", "status": "todo"},
    )
    task_id = task.json()["id"]
    await _call(api_client, "POST", f"stories/{story_id}/start", json={"actor": "test"})
    for hop in ("in_dev", "failed"):
        await _call(
            api_client,
            "POST",
            f"tasks/{task_id}/transition",
            params={"to_status": hop},
            json={"actor": "test"},
        )
    run_id = f"eng-refused-{uuid.uuid4().hex[:10]}"
    await _sql(
        "INSERT INTO runs (id, type, status, project_id, task_id, story_id, metadata, result) "
        "VALUES ($1, 'engineering', 'failed', $2, $3, $4, $5::json, $6::json)",
        run_id,
        uuid.UUID(project_id),
        task_id,
        story_id,
        json.dumps({"iteration": 0}),
        json.dumps(
            {
                "engineering_status": "failed",
                "allocation_failure_reason": "insufficient_free_memory",
                "allocation_required_ram_mb": 768,
                "allocation_min_disk_mb": 1024,
            }
        ),
    )
    return story_id, task_id, run_id


async def _run_record(api_client, run_id: str) -> OwnerNotification:
    run = await api_client.get_run(run_id)
    return OwnerNotification.model_validate(run.run_metadata[OWNER_NOTIFICATION_KEY])


async def _next_cycle(api_client, po: _PoInput, run_id: str) -> None:
    """One ``owner_notifications`` cycle, an attempt interval after the last attempt."""
    from src.tasks.owner_notifications import supervise_owed_owner_notifications

    stamp = (await _run_record(api_client, run_id)).last_attempt_at
    if stamp is not None:
        await age_last_attempt_by_one_interval(run_id, stamp, story_record=False)
    await supervise_owed_owner_notifications(api_client, po)


@pytest.mark.asyncio
async def test_a_redis_failure_after_the_park_is_recovered_by_a_later_cycle_exactly_once(
    api_client,
):
    from src.tasks.supervisor import supervise_failed_tasks

    story_id, task_id, run_id = await _refused_task(api_client)
    scoped = _Scoped(api_client, story_id)
    po = _PoInput()
    po.failures = 1

    await supervise_failed_tasks(scoped, po)

    assert (await api_client.get_task(task_id)).status is TaskStatus.WAITING_RESOURCES
    owed = await _run_record(api_client, run_id)
    assert owed.event == "task_waiting_resources"
    assert owed.state is OwnerNotificationState.OWED
    assert owed.attempts == 1
    assert po.events_for(story_id) == []

    await _next_cycle(api_client, po, run_id)
    await _next_cycle(api_client, po, run_id)

    assert po.events_for(story_id) == ["task_waiting_resources"]
    delivered = await _run_record(api_client, run_id)
    assert delivered.state is OwnerNotificationState.DELIVERED
    assert delivered.attempts == 2
    assert delivered.owed_at == owed.owed_at


@pytest.mark.asyncio
async def test_a_recipient_failure_after_the_resume_is_recovered_by_a_later_cycle_exactly_once(
    api_client,
):
    from src.tasks.supervisor import supervise_failed_tasks, supervise_waiting_resource_tasks

    story_id, task_id, run_id = await _refused_task(api_client)
    scoped = _Scoped(api_client, story_id)
    po = _PoInput()
    await supervise_failed_tasks(scoped, po)
    assert po.events_for(story_id) == ["task_waiting_resources"]

    scoped.user_lookup_failures = 1
    await supervise_waiting_resource_tasks(scoped, po)

    assert (await api_client.get_task(task_id)).status is TaskStatus.TODO
    owed = await _run_record(api_client, run_id)
    assert owed.event == "task_resources_resumed"
    assert owed.state is OwnerNotificationState.OWED
    assert owed.attempts == 1

    await _next_cycle(api_client, po, run_id)
    await _next_cycle(api_client, po, run_id)

    assert po.events_for(story_id) == ["task_waiting_resources", "task_resources_resumed"]
    assert (await _run_record(api_client, run_id)).state is OwnerNotificationState.DELIVERED


@pytest.mark.asyncio
async def test_a_wait_notice_still_owed_when_the_task_resumes_is_never_published(api_client):
    from src.tasks.supervisor import supervise_failed_tasks, supervise_waiting_resource_tasks

    story_id, _task_id, run_id = await _refused_task(api_client)
    scoped = _Scoped(api_client, story_id)
    po = _PoInput()
    po.failures = 1
    await supervise_failed_tasks(scoped, po)
    waiting = await _run_record(api_client, run_id)
    assert waiting.owed

    await supervise_waiting_resource_tasks(scoped, po)
    await _next_cycle(api_client, po, run_id)

    assert po.events_for(story_id) == ["task_resources_resumed"]
    resumed = await _run_record(api_client, run_id)
    assert resumed.state is OwnerNotificationState.DELIVERED
    assert resumed.owed_at > waiting.owed_at


@pytest.mark.asyncio
async def test_a_wait_notice_whose_task_left_the_wait_is_voided_not_published(api_client):
    from src.tasks.supervisor import supervise_failed_tasks

    story_id, task_id, run_id = await _refused_task(api_client)
    scoped = _Scoped(api_client, story_id)
    po = _PoInput()
    po.failures = 1
    await supervise_failed_tasks(scoped, po)
    # The wait ends without a resume: an operator sends the task to review.
    await api_client.transition_task(task_id, TaskStatus.WAITING_HUMAN_REVIEW, "operator")

    await _next_cycle(api_client, po, run_id)

    record = await _run_record(api_client, run_id)
    assert record.state is OwnerNotificationState.VOIDED
    assert record.attempts == 1
    assert po.events_for(story_id) == []


# --- the secret wait ---------------------------------------------------------


async def _secret_wait(api_client) -> tuple[str, str]:
    """A deploying story whose deploy Run reported a missing user secret: (story, run)."""
    project_id = await _project(api_client)
    suffix = uuid.uuid4().hex[:8]
    server = await _call(
        api_client,
        "POST",
        "servers/",
        json={
            "handle": f"lifecycle-{suffix}",
            "host": "lifecycle.test",
            "public_ip": "10.9.0.31",
            "ssh_key": fleet_private_key(),
        },
    )
    repository = await _call(
        api_client,
        "POST",
        "repositories/",
        json={
            "project_id": project_id,
            "name": f"lifecycle-{suffix}",
            "git_url": f"https://github.com/test/lifecycle-{suffix}.git",
        },
    )
    application = await _call(
        api_client,
        "POST",
        "applications/",
        json={
            "repo_id": repository.json()["id"],
            "server_handle": server.json()["handle"],
            "service_name": f"lifecycle-{suffix}",
            "status": "running",
        },
    )
    story_id = await _story(api_client, project_id, "start", "deploy")
    run_id = f"deploy-lifecycle-{uuid.uuid4().hex[:12]}"
    await _call(
        api_client,
        "POST",
        "runs/",
        json={
            "id": run_id,
            "type": RunType.DEPLOY.value,
            "project_id": project_id,
            "story_id": story_id,
            "run_metadata": {"application_id": application.json()["id"]},
        },
    )
    await _call(api_client, "PATCH", f"runs/{run_id}", json={"status": "running"})
    await _call(
        api_client,
        "PATCH",
        f"runs/{run_id}",
        json={
            "status": "completed",
            "result": {
                "deploy_outcome": "waiting_for_user_secret",
                "missing_user_secrets": [{"key": "STRIPE_KEY", "description": "Stripe secret key"}],
            },
        },
    )
    return story_id, run_id


@pytest.mark.asyncio
async def test_a_recipient_failure_after_the_secret_wait_is_recovered_exactly_once(api_client):
    """The ask is owed with the transition; its delivery still starts the watchdog's clock."""
    from src.tasks.supervisor import supervise_deploying_stories

    story_id, run_id = await _secret_wait(api_client)
    scoped = _Scoped(api_client, story_id)
    scoped.user_lookup_failures = 1
    po = _PoInput()

    await supervise_deploying_stories(scoped, po)

    assert (await api_client.get_story(story_id)).status.value == "waiting_user_secret"
    owed = await _run_record(api_client, run_id)
    assert owed.event == "story_waiting_user_secret"
    assert owed.state is OwnerNotificationState.OWED
    assert owed.attempts == 1
    assert owed.delivered_at is None
    assert po.events_for(story_id) == []

    swept_at = datetime.now(UTC)
    await _next_cycle(api_client, po, run_id)
    await _next_cycle(api_client, po, run_id)

    assert po.events_for(story_id) == ["story_waiting_user_secret"]
    delivered = await _run_record(api_client, run_id)
    assert delivered.state is OwnerNotificationState.DELIVERED
    assert delivered.delivered_at >= swept_at
    assert "STRIPE_KEY" in po.published[-1]["text"]


@pytest.mark.asyncio
async def test_a_redis_failure_after_the_secret_wait_is_recovered_exactly_once(api_client):
    from src.tasks.supervisor import supervise_deploying_stories

    story_id, run_id = await _secret_wait(api_client)
    po = _PoInput()
    po.failures = 1

    await supervise_deploying_stories(_Scoped(api_client, story_id), po)
    assert (await _run_record(api_client, run_id)).state is OwnerNotificationState.OWED

    await _next_cycle(api_client, po, run_id)
    await _next_cycle(api_client, po, run_id)

    assert po.events_for(story_id) == ["story_waiting_user_secret"]
    assert (await _run_record(api_client, run_id)).state is OwnerNotificationState.DELIVERED
