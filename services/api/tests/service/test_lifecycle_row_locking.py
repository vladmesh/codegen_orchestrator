"""Service tests: lifecycle writes serialize on the row, and no unvalidated hop.

Two callers that transition the same Story or Task from the same source status
must not both win: the write path reads its row with ``SELECT ... FOR UPDATE``,
so the loser re-reads the status the winner committed and is refused by
``VALID_TRANSITIONS``.  The complete-path shortcut for a Task validates every
hop it walks before it applies any of them.
"""

import asyncio
from http import HTTPStatus

from httpx import AsyncClient
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.task import TaskStatus
from shared.models import Story, Task

TASK_TEST_PROJECT_ID = "00000000-0000-0000-0000-000000000001"

# How long the two racing requests are given to reach their row read while a
# third session holds the lock.  Without the lock they run to completion here,
# which is exactly the failure the test is meant to show.
_RACE_WINDOW_SECONDS = 1.0


async def _hold_row_lock(session: AsyncSession, model, row_id: str) -> None:
    """Park a lock on one row so both racing requests queue behind it."""
    locked = (
        await session.execute(select(model).where(model.id == row_id).with_for_update())
    ).scalar_one_or_none()
    assert locked is not None, f"{model.__name__} {row_id} not visible to the locking session"


async def _race(
    async_client: AsyncClient,
    db_session: AsyncSession,
    model,
    row_id: str,
    url: str,
) -> list[int]:
    """Fire the same transition twice concurrently and return both status codes."""
    await _hold_row_lock(db_session, model, row_id)

    calls = [
        asyncio.create_task(async_client.post(url, json={"actor": f"racer-{i}"})) for i in range(2)
    ]
    try:
        await asyncio.sleep(_RACE_WINDOW_SECONDS)
    finally:
        # Release the parked lock; the first request through wins the row.
        await db_session.rollback()

    responses = await asyncio.gather(*calls)
    return sorted(response.status_code for response in responses)


async def _create_story(async_client: AsyncClient) -> str:
    resp = await async_client.post(
        "/api/stories/",
        json={
            "project_id": TASK_TEST_PROJECT_ID,
            "title": "Row lock race",
            "description": "Two callers start the same story",
        },
    )
    assert resp.status_code == HTTPStatus.CREATED, resp.text
    assert resp.json()["status"] == "created"
    return resp.json()["id"]


async def _create_task(async_client: AsyncClient, status_after_start: bool = True) -> str:
    resp = await async_client.post(
        "/api/tasks/",
        json={
            "project_id": TASK_TEST_PROJECT_ID,
            "title": "Row lock race task",
            "type": "feature",
        },
    )
    assert resp.status_code == HTTPStatus.CREATED, resp.text
    task_id = resp.json()["id"]
    if status_after_start:
        started = await async_client.post(f"/api/tasks/{task_id}/start", json={"actor": "po"})
        assert started.status_code == HTTPStatus.OK, started.text
        assert started.json()["status"] == TaskStatus.IN_DEV
    return task_id


@pytest.mark.asyncio
async def test_concurrent_story_start_lets_exactly_one_through(
    async_client: AsyncClient, db_session: AsyncSession, _tasks_project
) -> None:
    story_id = await _create_story(async_client)

    codes = await _race(async_client, db_session, Story, story_id, f"/api/stories/{story_id}/start")

    assert codes == [HTTPStatus.OK, HTTPStatus.UNPROCESSABLE_CONTENT], codes

    final = await async_client.get(f"/api/stories/{story_id}")
    assert final.status_code == HTTPStatus.OK, final.text
    assert final.json()["status"] == "in_progress"


@pytest.mark.asyncio
async def test_concurrent_task_transition_lets_exactly_one_through(
    async_client: AsyncClient, db_session: AsyncSession, _tasks_project
) -> None:
    task_id = await _create_task(async_client)

    codes = await _race(
        async_client,
        db_session,
        Task,
        task_id,
        f"/api/tasks/{task_id}/transition?to_status={TaskStatus.IN_CI.value}",
    )

    assert codes == [HTTPStatus.OK, HTTPStatus.UNPROCESSABLE_CONTENT], codes

    final = await async_client.get(f"/api/tasks/{task_id}")
    assert final.status_code == HTTPStatus.OK, final.text
    assert final.json()["status"] == TaskStatus.IN_CI

    events = await async_client.get(f"/api/tasks/{task_id}/events")
    to_in_ci = [
        event
        for event in events.json()
        if event["event_type"] == "status_change" and event["to_status"] == TaskStatus.IN_CI
    ]
    assert len(to_in_ci) == 1, to_in_ci


@pytest.mark.asyncio
async def test_complete_path_refuses_an_invalid_intermediate_step(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, _tasks_project
) -> None:
    """An illegal hop in the shortcut is rejected and writes nothing."""
    from src.routers import _task_actions

    task_id = await _create_task(async_client)

    # in_ci → backlog is not in VALID_TRANSITIONS; nothing on this path may run.
    monkeypatch.setitem(
        _task_actions._COMPLETE_PATH,
        TaskStatus.IN_DEV,
        [TaskStatus.IN_CI, TaskStatus.BACKLOG, TaskStatus.DONE],
    )

    resp = await async_client.post(f"/api/tasks/{task_id}/complete", json={"actor": "system"})
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_CONTENT, resp.text

    final = await async_client.get(f"/api/tasks/{task_id}")
    assert final.json()["status"] == TaskStatus.IN_DEV

    events = await async_client.get(f"/api/tasks/{task_id}/events")
    promotions = [
        event
        for event in events.json()
        if event["event_type"] == "status_change" and event["from_status"] == TaskStatus.IN_DEV
    ]
    assert promotions == [], promotions


@pytest.mark.asyncio
async def test_complete_path_still_promotes_a_legal_chain(
    async_client: AsyncClient, _tasks_project
) -> None:
    """The unchanged in_dev → in_ci → testing → done shortcut still succeeds."""
    task_id = await _create_task(async_client)

    resp = await async_client.post(f"/api/tasks/{task_id}/complete", json={"actor": "system"})
    assert resp.status_code == HTTPStatus.OK, resp.text
    assert resp.json()["status"] == TaskStatus.DONE

    events = await async_client.get(f"/api/tasks/{task_id}/events")
    hops = {
        (event["from_status"], event["to_status"])
        for event in events.json()
        if event["event_type"] == "status_change"
    }
    assert {
        (TaskStatus.IN_DEV, TaskStatus.IN_CI),
        (TaskStatus.IN_CI, TaskStatus.TESTING),
        (TaskStatus.TESTING, TaskStatus.DONE),
    } <= hops, hops
