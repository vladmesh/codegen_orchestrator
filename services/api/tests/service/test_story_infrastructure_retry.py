"""Service tests for the one-action recovery of a pre-agent infrastructure park."""

import asyncio

from httpx import AsyncClient
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.engineering_execution import EngineeringInfrastructureRefusal
from shared.contracts.dto.run import RunType
from shared.models import Project, Run

PROJECT_ID = "00000000-0000-0000-0000-000000000001"


async def _parked(
    async_client: AsyncClient, *, infrastructure: bool = True
) -> tuple[str, str, dict]:
    story = (
        await async_client.post(
            "/api/stories/",
            json={"project_id": PROJECT_ID, "title": "Infrastructure recovery"},
        )
    ).json()
    story_id = story["id"]
    task = (
        await async_client.post(
            "/api/tasks/",
            json={"project_id": PROJECT_ID, "story_id": story_id, "title": "Refused task"},
        )
    ).json()
    task_id = task["id"]
    await async_client.post(f"/api/stories/{story_id}/start", json={"actor": "test"})
    await async_client.post(f"/api/tasks/{task_id}/start", json={"actor": "test"})

    evidence = {
        "engineering_infrastructure": {
            "execution_phase": "pre_agent_refused",
            "refusal": "project_locked",
            "task_id": task_id,
            "attempt_id": "eng-refused-1",
            "detail": "Engineering worker creation was refused: project locked.",
        }
    }
    stored = evidence if infrastructure else {"reason": "product decision needs review"}
    await async_client.patch(f"/api/tasks/{task_id}", json={"failure_metadata": stored})
    await async_client.patch(f"/api/stories/{story_id}", json={"quarantine_reason": stored})
    await async_client.post(
        f"/api/tasks/{task_id}/transition?to_status=waiting_human_review",
        json={"actor": "test"},
    )
    await async_client.post(f"/api/stories/{story_id}/human-review", json={"actor": "test"})
    return story_id, task_id, evidence


def _command(task_id: str, *, refusal: str = "project_locked") -> dict:
    return {
        "task_id": task_id,
        "attempt_id": "eng-refused-1",
        "refusal": refusal,
        "actor": "admin",
    }


@pytest.mark.asyncio
async def test_retry_infrastructure_attempt_is_atomic_and_preserves_iteration(
    async_client: AsyncClient, _tasks_project
) -> None:
    story_id, task_id, _ = await _parked(async_client)

    response = await async_client.post(
        f"/api/stories/{story_id}/retry-infrastructure-attempt", json=_command(task_id)
    )

    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "retried"
    task = (await async_client.get(f"/api/tasks/{task_id}")).json()
    story = (await async_client.get(f"/api/stories/{story_id}")).json()
    assert (task["status"], task["current_iteration"], task["failure_metadata"]) == (
        "todo",
        0,
        None,
    )
    assert (story["status"], story["quarantine_reason"]) == ("in_progress", None)


@pytest.mark.asyncio
async def test_retry_rejects_stale_reason_without_partial_change(
    async_client: AsyncClient, _tasks_project
) -> None:
    story_id, task_id, evidence = await _parked(async_client)

    response = await async_client.post(
        f"/api/stories/{story_id}/retry-infrastructure-attempt",
        json=_command(task_id, refusal="worker_profile_unavailable"),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "stale_infrastructure_reason"
    assert (await async_client.get(f"/api/tasks/{task_id}")).json()["failure_metadata"] == evidence


@pytest.mark.asyncio
async def test_retry_rejects_non_infrastructure_human_review(
    async_client: AsyncClient, _tasks_project
) -> None:
    story_id, task_id, _ = await _parked(async_client, infrastructure=False)

    response = await async_client.post(
        f"/api/stories/{story_id}/retry-infrastructure-attempt", json=_command(task_id)
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "not_infrastructure_park"


@pytest.mark.asyncio
async def test_retry_rejects_a_task_that_left_the_parked_status(
    async_client: AsyncClient, _tasks_project
) -> None:
    story_id, task_id, evidence = await _parked(async_client)
    moved = await async_client.post(
        f"/api/tasks/{task_id}/transition?to_status=backlog", json={"actor": "test"}
    )
    assert moved.status_code == 200, moved.text

    response = await async_client.post(
        f"/api/stories/{story_id}/retry-infrastructure-attempt", json=_command(task_id)
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "wrong_status"
    assert (await async_client.get(f"/api/tasks/{task_id}")).json()["failure_metadata"] == evidence


@pytest.mark.asyncio
async def test_repeated_retry_is_a_typed_noop(async_client: AsyncClient, _tasks_project) -> None:
    story_id, task_id, _ = await _parked(async_client)
    path = f"/api/stories/{story_id}/retry-infrastructure-attempt"

    first = await async_client.post(path, json=_command(task_id))
    repeated = await async_client.post(path, json=_command(task_id))

    assert first.json()["outcome"] == "retried"
    assert repeated.status_code == 200
    assert repeated.json()["outcome"] == "already_retried"


@pytest.mark.asyncio
async def test_concurrent_retry_creates_one_fresh_attempt_state(
    async_client: AsyncClient, _tasks_project
) -> None:
    story_id, task_id, _ = await _parked(async_client)
    path = f"/api/stories/{story_id}/retry-infrastructure-attempt"

    responses = await asyncio.gather(
        async_client.post(path, json=_command(task_id)),
        async_client.post(path, json=_command(task_id)),
    )

    assert sorted(response.json()["outcome"] for response in responses) == [
        "already_retried",
        "retried",
    ]
    events = (await async_client.get(f"/api/tasks/{task_id}/events")).json()
    retry_events = [
        event
        for event in events
        if event["details"].get("action") == "retry_infrastructure_attempt"
    ]
    assert len(retry_events) == 2
    assert {event["to_status"] for event in retry_events} == {"backlog", "todo"}


@pytest.mark.asyncio
async def test_next_dispatch_tick_creates_exactly_one_fresh_attempt(
    async_client: AsyncClient, db_session: AsyncSession, _tasks_project
) -> None:
    project = await db_session.get(Project, PROJECT_ID)
    original_config = project.config
    project.config = {**original_config, "workspace_ready": True}
    await db_session.commit()
    story_id, task_id, _ = await _parked(async_client)

    recovered = await async_client.post(
        f"/api/stories/{story_id}/retry-infrastructure-attempt", json=_command(task_id)
    )
    dispatched = await async_client.post(
        "/api/work-admission/engineering-dispatches", json={"task_id": task_id}
    )
    repeated_tick = await async_client.post(
        "/api/work-admission/engineering-dispatches", json={"task_id": task_id}
    )

    assert recovered.json()["outcome"] == "retried"
    assert dispatched.status_code == 200, dispatched.text
    assert dispatched.json()["outcome"] == "admitted"
    assert repeated_tick.json()["outcome"] == "repair"
    runs = (
        await db_session.scalars(
            select(Run).where(Run.task_id == task_id, Run.type == RunType.ENGINEERING.value)
        )
    ).all()
    assert [run.id for run in runs] == [dispatched.json()["run_id"]]
    assert (await async_client.get(f"/api/tasks/{task_id}")).json()["current_iteration"] == 0

    project = await db_session.get(Project, PROJECT_ID)
    project.config = original_config
    await db_session.commit()


def test_refusal_enum_covers_the_api_fixture() -> None:
    assert EngineeringInfrastructureRefusal.PROJECT_LOCKED == "project_locked"
