"""A committed story Task change to done or cancelled publishes the story-completion hint."""

from _api_calls import call_api
from httpx import AsyncClient
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.queues.scheduler_wake import SchedulerWakeTarget
from shared.models import Task

STORY_COMPLETION = [SchedulerWakeTarget.STORY_COMPLETION]


async def _story_task(client: AsyncClient, project_id: str, *, in_dev: bool = True) -> str:
    story = await call_api(
        client, "POST", "/api/stories/", 201, json={"project_id": project_id, "title": "Wake"}
    )
    await call_api(client, "POST", f"/api/stories/{story['id']}/start", json={"actor": "test"})
    task = await call_api(
        client,
        "POST",
        "/api/tasks/",
        201,
        json={
            "project_id": project_id,
            "story_id": story["id"],
            "title": "Wake engineering",
            "status": "todo",
        },
    )
    if in_dev:
        await _transition(client, task["id"], "in_dev")
    return task["id"]


async def _transition(client: AsyncClient, task_id: str, to_status: str, status: int = 200):
    return await call_api(
        client,
        "POST",
        f"/api/tasks/{task_id}/transition",
        status,
        params={"to_status": to_status},
        json={"actor": "test"},
    )


async def _committed_status(db_engine, task_id: str) -> str:
    async with AsyncSession(db_engine) as session:
        return await session.scalar(select(Task.status).where(Task.id == task_id))


@pytest.mark.asyncio
async def test_complete_publishes_one_hint_after_committing_done(
    async_client, _tasks_project, wake_hints, db_engine
):
    task_id = await _story_task(async_client, _tasks_project)
    assert await wake_hints.published() == []

    completed = await call_api(async_client, "POST", f"/api/tasks/{task_id}/complete")

    assert completed["status"] == "done"
    assert await wake_hints.published() == STORY_COMPLETION
    assert await _committed_status(db_engine, task_id) == "done"


@pytest.mark.asyncio
async def test_only_the_final_transition_to_done_publishes(
    async_client, _tasks_project, wake_hints, db_engine
):
    task_id = await _story_task(async_client, _tasks_project)

    for hop in ("in_ci", "testing"):
        await _transition(async_client, task_id, hop)
    assert await wake_hints.published() == []

    await _transition(async_client, task_id, "done")
    assert await wake_hints.published() == STORY_COMPLETION
    assert await _committed_status(db_engine, task_id) == "done"


@pytest.mark.asyncio
async def test_a_transition_to_cancelled_publishes(
    async_client, _tasks_project, wake_hints, db_engine
):
    task_id = await _story_task(async_client, _tasks_project)
    await wake_hints.published()

    await _transition(async_client, task_id, "cancelled")

    assert await wake_hints.published() == STORY_COMPLETION
    assert await _committed_status(db_engine, task_id) == "cancelled"


@pytest.mark.asyncio
async def test_delete_publishes_once_and_a_repeated_delete_is_silent(
    async_client, _tasks_project, wake_hints, db_engine
):
    task_id = await _story_task(async_client, _tasks_project, in_dev=False)
    await wake_hints.published()

    await call_api(async_client, "DELETE", f"/api/tasks/{task_id}")
    assert await wake_hints.published() == STORY_COMPLETION

    repeated = await call_api(async_client, "DELETE", f"/api/tasks/{task_id}")
    assert repeated["status"] == "cancelled"
    assert await wake_hints.published() == []
    assert await _committed_status(db_engine, task_id) == "cancelled"


@pytest.mark.asyncio
async def test_refused_and_storyless_changes_publish_nothing(
    async_client, _tasks_project, wake_hints, db_engine
):
    task_id = await _story_task(async_client, _tasks_project, in_dev=False)
    await wake_hints.published()

    await _transition(async_client, task_id, "done", status=422)
    await call_api(async_client, "POST", f"/api/tasks/{task_id}/complete", 422)
    assert await wake_hints.published() == []
    assert await _committed_status(db_engine, task_id) == "todo"

    storyless = await call_api(
        async_client,
        "POST",
        "/api/tasks/",
        201,
        json={"project_id": _tasks_project, "title": "Storyless"},
    )
    await call_api(async_client, "POST", f"/api/tasks/{storyless['id']}/start")
    await call_api(async_client, "POST", f"/api/tasks/{storyless['id']}/complete")
    assert await wake_hints.published() == []
    assert await _committed_status(db_engine, storyless["id"]) == "done"
