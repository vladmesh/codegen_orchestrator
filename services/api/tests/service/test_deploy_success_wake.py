"""A committed first story deploy success publishes the lifecycle-supervision hint."""

import uuid

from httpx import AsyncClient
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.run_result import DeployRunResult
from shared.contracts.queues.deploy import DeployOutcome
from shared.contracts.queues.scheduler_wake import SchedulerWakeTarget
from shared.models import Run

TASK_TEST_PROJECT_ID = "00000000-0000-0000-0000-000000000001"
LIFECYCLE_SUPERVISION = [SchedulerWakeTarget.LIFECYCLE_SUPERVISION]
SUCCESS = DeployRunResult(
    deploy_outcome=DeployOutcome.SUCCESS, deployed_url="https://wake.example"
).model_dump(mode="json")


async def _call(client: AsyncClient, method: str, path: str, status: int = 200, **kwargs):
    response = await client.request(method, path, **kwargs)
    assert response.status_code == status, response.text
    return response.json()


async def _running_deploy(
    client: AsyncClient, *, story: bool = True, metadata: dict | None = None
) -> str:
    story_id = None
    if story:
        created = await _call(
            client,
            "POST",
            "/api/stories/",
            201,
            json={"project_id": TASK_TEST_PROJECT_ID, "title": "Deploy wake"},
        )
        story_id = created["id"]
    run = await _call(
        client,
        "POST",
        "/api/runs/",
        201,
        json={
            "id": f"deploy-wake-{uuid.uuid4().hex[:12]}",
            "type": "deploy",
            "project_id": TASK_TEST_PROJECT_ID,
            "story_id": story_id,
            "run_metadata": metadata or {},
        },
    )
    await _call(client, "PATCH", f"/api/runs/{run['id']}", json={"status": "running"})
    return run["id"]


async def _succeed(client: AsyncClient, run_id: str) -> dict:
    return await _call(
        client, "PATCH", f"/api/runs/{run_id}", json={"status": "completed", "result": SUCCESS}
    )


async def _committed(db_engine, run_id: str) -> tuple[str, dict | None]:
    async with AsyncSession(db_engine) as session:
        row = (await session.execute(select(Run.status, Run.result).where(Run.id == run_id))).one()
        return row.status, row.result


@pytest.mark.asyncio
async def test_the_first_success_publishes_one_hint_after_its_proof_committed(
    async_client, _tasks_project, wake_hints, db_engine
):
    run_id = await _running_deploy(async_client)
    assert await wake_hints.published() == []

    await _succeed(async_client, run_id)

    assert await wake_hints.published() == LIFECYCLE_SUPERVISION
    assert await _committed(db_engine, run_id) == ("completed", SUCCESS)

    await _succeed(async_client, run_id)
    await _call(
        async_client,
        "PATCH",
        f"/api/runs/{run_id}",
        json={"run_metadata": {"deployed_by": "deployer"}},
    )
    assert await wake_hints.published() == []


@pytest.mark.asyncio
async def test_a_late_success_of_a_cancelled_deploy_publishes(
    async_client, _tasks_project, wake_hints, db_engine
):
    run_id = await _running_deploy(async_client)
    await _call(async_client, "PATCH", f"/api/runs/{run_id}", json={"status": "cancelled"})
    assert await wake_hints.published() == []

    await _call(async_client, "PATCH", f"/api/runs/{run_id}", json={"result": SUCCESS})

    assert await wake_hints.published() == LIFECYCLE_SUPERVISION
    assert await _committed(db_engine, run_id) == ("cancelled", SUCCESS)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("story", "metadata"),
    [
        (False, None),
        (True, {"temporary_access_grant_id": None}),
        (True, {"temporary_access_operation": "grant"}),
    ],
    ids=["storyless", "null_grant_key", "grant_operation"],
)
async def test_storyless_and_temporary_access_successes_publish_nothing(
    async_client, _tasks_project, wake_hints, db_engine, story, metadata
):
    run_id = await _running_deploy(async_client, story=story, metadata=metadata)
    await wake_hints.published()

    await _succeed(async_client, run_id)

    assert await wake_hints.published() == []
    assert await _committed(db_engine, run_id) == ("completed", SUCCESS)
