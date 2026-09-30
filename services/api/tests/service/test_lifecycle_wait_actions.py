"""Entering and leaving a lifecycle wait commits with the owner notice it owes.

A task parked in ``waiting_resources``, the same task resumed, and a story
parked in ``waiting_user_secret`` are each one API action: the state change and
the owed ``OwnerNotification`` on the Run the move was decided on commit in one
transaction, or neither does. These tests go through the real transaction and
read the committed rows back, so a move without its record, or a record without
its move, is observable here.
"""

from datetime import UTC, datetime
import uuid

from httpx import AsyncClient
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.owner_notification import (
    OWNER_NOTIFICATION_ATTEMPT_SUPERSEDED,
    OWNER_NOTIFICATION_KEY,
    OwnerNotification,
    OwnerNotificationState,
)
from shared.models import Run

PROJECT_ID = "00000000-0000-0000-0000-000000000001"
WAIT_TEXT = "Engineering is waiting for server capacity."
RESUME_TEXT = "Server capacity is available again."
ASK_TEXT = "Ask the user for STRIPE_KEY."


# --- state -----------------------------------------------------------------


async def _story_task(client: AsyncClient, *hops: str) -> tuple[str, str]:
    """A started story with one task, walked through ``hops``."""
    story = await client.post(
        "/api/stories/", json={"project_id": PROJECT_ID, "title": "Resource wait"}
    )
    assert story.status_code == 201, story.text
    story_id = story.json()["id"]
    task = await client.post(
        "/api/tasks/",
        json={"project_id": PROJECT_ID, "story_id": story_id, "title": "Wait", "status": "todo"},
    )
    assert task.status_code == 201, task.text
    task_id = task.json()["id"]
    started = await client.post(f"/api/stories/{story_id}/start", json={"actor": "test"})
    assert started.status_code == 200, started.text
    await _walk(client, task_id, *hops)
    return story_id, task_id


async def _walk(client: AsyncClient, task_id: str, *hops: str) -> None:
    for hop in hops:
        moved = await client.post(
            f"/api/tasks/{task_id}/transition?to_status={hop}", json={"actor": "test"}
        )
        assert moved.status_code == 200, moved.text


async def _refused_run(db_session: AsyncSession, story_id: str, task_id: str) -> str:
    """The failed engineering Run a placement refusal leaves; it holds no paid slot."""
    run_id = f"eng-wait-{uuid.uuid4().hex[:10]}"
    db_session.add(
        Run(
            id=run_id,
            type="engineering",
            status="failed",
            project_id=uuid.UUID(PROJECT_ID),
            task_id=task_id,
            story_id=story_id,
            run_metadata={"iteration": 0},
            result={
                "engineering_status": "failed",
                "allocation_failure_reason": "insufficient_free_memory",
                "allocation_required_ram_mb": 768,
                "allocation_min_disk_mb": 1024,
            },
        )
    )
    await db_session.commit()
    return run_id


def _park(run_id: str, **changes) -> dict:
    return {
        "run_id": run_id,
        "allocation_failure_reason": "insufficient_free_memory",
        "allocation_required_ram_mb": 768,
        "allocation_min_disk_mb": 1024,
        "event": "task_waiting_resources",
        "text": WAIT_TEXT,
        "actor": "supervisor",
        **changes,
    }


async def _task(client: AsyncClient, task_id: str) -> dict:
    response = await client.get(f"/api/tasks/{task_id}")
    assert response.status_code == 200, response.text
    return response.json()


async def _status_events(client: AsyncClient, task_id: str) -> list[tuple[str, str, str | None]]:
    response = await client.get(f"/api/tasks/{task_id}/events")
    assert response.status_code == 200, response.text
    return [
        (event["from_status"], event["to_status"], (event["details"] or {}).get("action"))
        for event in response.json()
        if event["event_type"] == "status_change"
    ]


async def _record(client: AsyncClient, run_id: str) -> OwnerNotification | None:
    response = await client.get(f"/api/runs/{run_id}")
    assert response.status_code == 200, response.text
    stored = response.json()["run_metadata"].get(OWNER_NOTIFICATION_KEY)
    return None if stored is None else OwnerNotification.model_validate(stored)


# --- the task park -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_park_commits_the_wait_facts_the_hop_and_the_owed_notice_together(
    async_client: AsyncClient, db_session: AsyncSession, _tasks_project
):
    story_id, task_id = await _story_task(async_client, "in_dev", "failed")
    run_id = await _refused_run(db_session, story_id, task_id)

    parked = await async_client.post(
        f"/api/tasks/{task_id}/park-waiting-resources", json=_park(run_id)
    )

    assert parked.status_code == 200, parked.text
    body = parked.json()
    assert body["disposition"] == "parked"
    assert body["new_wait"] is True
    assert body["run_id"] == run_id
    task = await _task(async_client, task_id)
    assert task["status"] == "waiting_resources"
    metadata = task["failure_metadata"]
    assert metadata["resource_wait_started_at"]
    assert metadata["allocation_required_ram_mb"] == 768
    assert metadata["allocation_min_disk_mb"] == 1024
    assert metadata["allocation_failure_reason"] == "insufficient_free_memory"
    assert (await _status_events(async_client, task_id))[-1] == (
        "failed",
        "waiting_resources",
        "park_waiting_resources",
    )
    record = await _record(async_client, run_id)
    assert record == OwnerNotification.model_validate(body["owner_notification"])
    assert record.event == "task_waiting_resources"
    assert record.text == WAIT_TEXT
    assert record.state is OwnerNotificationState.OWED
    assert record.story_id == story_id
    assert record.task_id == task_id
    assert record.project_id == PROJECT_ID
    assert record.terminal_status.value == "in_progress"
    assert [s.value for s in record.expected_task_statuses] == ["waiting_resources"]
    assert record.last_attempt_at is None
    # The rest of the Run's metadata is untouched.
    run = (await async_client.get(f"/api/runs/{run_id}")).json()
    assert run["run_metadata"]["iteration"] == 0


@pytest.mark.asyncio
async def test_a_park_whose_hop_is_illegal_writes_neither_the_move_nor_the_notice(
    async_client: AsyncClient, db_session: AsyncSession, _tasks_project
):
    # todo → waiting_resources is not a legal hop.
    story_id, task_id = await _story_task(async_client)
    run_id = await _refused_run(db_session, story_id, task_id)

    parked = await async_client.post(
        f"/api/tasks/{task_id}/park-waiting-resources", json=_park(run_id)
    )

    assert parked.status_code == 422, parked.text
    task = await _task(async_client, task_id)
    assert task["status"] == "todo"
    assert task["failure_metadata"] is None
    assert await _record(async_client, run_id) is None


@pytest.mark.asyncio
async def test_a_park_naming_another_tasks_run_writes_nothing(
    async_client: AsyncClient, db_session: AsyncSession, _tasks_project
):
    story_id, task_id = await _story_task(async_client, "in_dev", "failed")
    other_story, other_task = await _story_task(async_client, "in_dev", "failed")
    foreign_run = await _refused_run(db_session, other_story, other_task)

    parked = await async_client.post(
        f"/api/tasks/{task_id}/park-waiting-resources", json=_park(foreign_run)
    )

    assert parked.status_code == 409, parked.text
    assert parked.json()["detail"]["code"] == "stale_attempt_fence"
    assert (await _task(async_client, task_id))["status"] == "failed"
    assert await _record(async_client, foreign_run) is None


@pytest.mark.asyncio
async def test_a_repeated_park_is_a_typed_noop_that_owes_nothing_twice(
    async_client: AsyncClient, db_session: AsyncSession, _tasks_project
):
    story_id, task_id = await _story_task(async_client, "in_dev", "failed")
    run_id = await _refused_run(db_session, story_id, task_id)
    url = f"/api/tasks/{task_id}/park-waiting-resources"
    first = await async_client.post(url, json=_park(run_id))

    again = await async_client.post(url, json=_park(run_id))

    assert again.status_code == 200, again.text
    assert again.json()["disposition"] == "already_waiting"
    assert again.json()["owner_notification"] == first.json()["owner_notification"]
    parks = [
        event
        for event in await _status_events(async_client, task_id)
        if event[1] == "waiting_resources"
    ]
    assert len(parks) == 1


@pytest.mark.asyncio
async def test_a_park_inside_a_wait_under_way_keeps_its_start_and_is_not_announced(
    async_client: AsyncClient, db_session: AsyncSession, _tasks_project
):
    """Announce once per wait: a refused retry after a resume is the same wait."""
    story_id, task_id = await _story_task(async_client, "in_dev", "failed")
    first_run = await _refused_run(db_session, story_id, task_id)
    await async_client.post(f"/api/tasks/{task_id}/park-waiting-resources", json=_park(first_run))
    started = (await _task(async_client, task_id))["failure_metadata"]["resource_wait_started_at"]
    resumed = await async_client.post(
        f"/api/tasks/{task_id}/resume-from-resource-wait",
        json={"text": RESUME_TEXT, "actor": "supervisor"},
    )
    assert resumed.json()["disposition"] == "resumed"
    await _walk(async_client, task_id, "in_dev", "failed")
    second_run = await _refused_run(db_session, story_id, task_id)

    parked = await async_client.post(
        f"/api/tasks/{task_id}/park-waiting-resources",
        json=_park(second_run, event="task_waiting_infrastructure"),
    )

    assert parked.status_code == 200, parked.text
    assert parked.json()["disposition"] == "parked"
    assert parked.json()["new_wait"] is False
    assert parked.json()["owner_notification"] is None
    task = await _task(async_client, task_id)
    assert task["status"] == "waiting_resources"
    assert task["failure_metadata"]["resource_wait_started_at"] == started
    assert await _record(async_client, second_run) is None


# --- the resume --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_resume_moves_the_task_and_replaces_the_wait_notice_on_the_same_run(
    async_client: AsyncClient, db_session: AsyncSession, _tasks_project
):
    story_id, task_id = await _story_task(async_client, "in_dev", "failed")
    run_id = await _refused_run(db_session, story_id, task_id)
    await async_client.post(f"/api/tasks/{task_id}/park-waiting-resources", json=_park(run_id))
    waiting = await _record(async_client, run_id)

    resumed = await async_client.post(
        f"/api/tasks/{task_id}/resume-from-resource-wait",
        json={"text": RESUME_TEXT, "actor": "supervisor"},
    )

    assert resumed.status_code == 200, resumed.text
    body = resumed.json()
    assert body["disposition"] == "resumed"
    assert body["run_id"] == run_id
    assert (await _task(async_client, task_id))["status"] == "todo"
    assert (await _status_events(async_client, task_id))[-2:] == [
        ("waiting_resources", "backlog", "resume_from_resource_wait"),
        ("backlog", "todo", "resume_from_resource_wait"),
    ]
    record = await _record(async_client, run_id)
    assert record.event == "task_resources_resumed"
    assert record.text == RESUME_TEXT
    assert record.state is OwnerNotificationState.OWED
    assert [s.value for s in record.expected_task_statuses] == ["todo", "in_dev"]
    assert record.owed_at > waiting.owed_at


@pytest.mark.asyncio
async def test_a_resume_of_a_task_that_is_not_waiting_writes_nothing(
    async_client: AsyncClient, db_session: AsyncSession, _tasks_project
):
    story_id, task_id = await _story_task(async_client, "in_dev", "failed")
    run_id = await _refused_run(db_session, story_id, task_id)

    resumed = await async_client.post(
        f"/api/tasks/{task_id}/resume-from-resource-wait",
        json={"text": RESUME_TEXT, "actor": "supervisor"},
    )

    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["disposition"] == "not_waiting"
    assert resumed.json()["task_status"] == "failed"
    assert (await _task(async_client, task_id))["status"] == "failed"
    assert await _record(async_client, run_id) is None


@pytest.mark.asyncio
async def test_a_visit_to_the_replaced_wait_notice_cannot_write_it_back(
    async_client: AsyncClient, db_session: AsyncSession, _tasks_project
):
    """A sweep that claimed the "waiting" record before the resume replaced it.

    Its settlement names the older obligation, so the API refuses it instead of
    putting the stale message back over the "resumed" one it has to deliver.
    """
    story_id, task_id = await _story_task(async_client, "in_dev", "failed")
    run_id = await _refused_run(db_session, story_id, task_id)
    await async_client.post(f"/api/tasks/{task_id}/park-waiting-resources", json=_park(run_id))
    claim = await async_client.post(f"/api/runs/{run_id}/owner-notification/attempt")
    stamped = OwnerNotification.model_validate(claim.json()["notification"])
    await async_client.post(
        f"/api/tasks/{task_id}/resume-from-resource-wait",
        json={"text": RESUME_TEXT, "actor": "supervisor"},
    )

    stale = await async_client.patch(
        f"/api/runs/{run_id}",
        json={
            "run_metadata": {
                OWNER_NOTIFICATION_KEY: stamped.model_copy(
                    update={"state": OwnerNotificationState.DELIVERED, "attempts": 1}
                ).model_dump(mode="json")
            }
        },
    )

    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["code"] == OWNER_NOTIFICATION_ATTEMPT_SUPERSEDED
    record = await _record(async_client, run_id)
    assert record.event == "task_resources_resumed"
    assert record.owed


# --- the secret wait ---------------------------------------------------------


async def _deploying_story(async_client: AsyncClient, *actions: str) -> tuple[str, str]:
    story = await async_client.post(
        "/api/stories/", json={"project_id": PROJECT_ID, "title": "Secret wait"}
    )
    assert story.status_code == 201, story.text
    story_id = story.json()["id"]
    for action in actions:
        moved = await async_client.post(f"/api/stories/{story_id}/{action}")
        assert moved.status_code == 200, moved.text
    run_id = f"deploy-secret-{uuid.uuid4().hex[:10]}"
    created = await async_client.post(
        "/api/runs/",
        json={
            "id": run_id,
            "type": "deploy",
            "project_id": PROJECT_ID,
            "story_id": story_id,
            "run_metadata": {},
        },
    )
    assert created.status_code == 201, created.text
    return story_id, run_id


def _ask(run_id: str) -> dict:
    return {"run_id": run_id, "text": ASK_TEXT, "actor": "supervisor"}


@pytest.mark.asyncio
async def test_a_secret_wait_commits_the_transition_and_the_ask_together(
    async_client: AsyncClient, _tasks_project
):
    story_id, run_id = await _deploying_story(async_client, "start", "deploy")

    parked = await async_client.post(
        f"/api/stories/{story_id}/park-waiting-user-secret", json=_ask(run_id)
    )

    assert parked.status_code == 200, parked.text
    assert parked.json()["disposition"] == "waiting"
    story = (await async_client.get(f"/api/stories/{story_id}")).json()
    assert story["status"] == "waiting_user_secret"
    record = await _record(async_client, run_id)
    assert record == OwnerNotification.model_validate(parked.json()["owner_notification"])
    assert record.event == "story_waiting_user_secret"
    assert record.terminal_status.value == "waiting_user_secret"
    assert record.text == ASK_TEXT
    assert record.state is OwnerNotificationState.OWED
    assert record.task_id is None
    assert record.expected_task_statuses is None
    # Owing is not telling: the watchdog's anchor is not set by the move.
    assert record.delivered_at is None


@pytest.mark.asyncio
async def test_a_secret_wait_whose_hop_is_illegal_owes_no_ask(
    async_client: AsyncClient, _tasks_project
):
    story_id, run_id = await _deploying_story(async_client, "start")

    parked = await async_client.post(
        f"/api/stories/{story_id}/park-waiting-user-secret", json=_ask(run_id)
    )

    assert parked.status_code == 422, parked.text
    story = (await async_client.get(f"/api/stories/{story_id}")).json()
    assert story["status"] == "in_progress"
    assert await _record(async_client, run_id) is None


@pytest.mark.asyncio
async def test_a_secret_wait_keeps_an_ask_the_run_already_owes_and_a_repeat_writes_nothing(
    async_client: AsyncClient, _tasks_project
):
    story_id, run_id = await _deploying_story(async_client, "start", "deploy")
    earlier = OwnerNotification(
        event="story_waiting_user_secret",
        text="An ask owed before the transition.",
        story_id=story_id,
        project_id=PROJECT_ID,
        terminal_status="waiting_user_secret",
        state=OwnerNotificationState.OWED,
        owed_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
    )
    written = await async_client.patch(
        f"/api/runs/{run_id}",
        json={"run_metadata": {OWNER_NOTIFICATION_KEY: earlier.model_dump(mode="json")}},
    )
    assert written.status_code == 200, written.text
    url = f"/api/stories/{story_id}/park-waiting-user-secret"

    parked = await async_client.post(url, json=_ask(run_id))
    again = await async_client.post(url, json=_ask(run_id))

    assert parked.json()["disposition"] == "waiting"
    assert again.json()["disposition"] == "already_waiting"
    record = await _record(async_client, run_id)
    assert record.owed_at == earlier.owed_at
    assert record.text == earlier.text
    assert OwnerNotification.model_validate(again.json()["owner_notification"]) == record


@pytest.mark.asyncio
async def test_a_secret_wait_naming_another_storys_run_writes_nothing(
    async_client: AsyncClient, _tasks_project
):
    story_id, _run_id = await _deploying_story(async_client, "start", "deploy")
    _other_story, foreign_run = await _deploying_story(async_client, "start", "deploy")

    parked = await async_client.post(
        f"/api/stories/{story_id}/park-waiting-user-secret", json=_ask(foreign_run)
    )

    assert parked.status_code == 409, parked.text
    assert parked.json()["detail"]["code"] == "stale_attempt_fence"
    story = (await async_client.get(f"/api/stories/{story_id}")).json()
    assert story["status"] == "deploying"
    assert await _record(async_client, foreign_run) is None
