"""Run vocabulary is enforced at HTTP ingress without partial database writes."""

from http import HTTPStatus
import uuid

from httpx import AsyncClient
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.run import RunStatus, RunType
from shared.models import Run


async def _deploy_run(client: AsyncClient) -> str:
    run_id = f"contract-{uuid.uuid4().hex}"
    response = await client.post("/api/runs/", json={"id": run_id, "type": "deploy"})
    assert response.status_code == HTTPStatus.CREATED, response.text
    return run_id


async def test_unknown_create_type_is_refused_before_persistence(
    async_client: AsyncClient, db_session: AsyncSession
) -> None:
    run_id = f"invalid-{uuid.uuid4().hex}"

    response = await async_client.post("/api/runs/", json={"id": run_id, "type": "build"})

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, response.text
    assert await db_session.get(Run, run_id) is None


@pytest.mark.parametrize("run_type", [RunType.ENGINEERING, RunType.QA])
async def test_known_paid_type_still_uses_the_paid_admission_gate(
    async_client: AsyncClient, db_session: AsyncSession, run_type: RunType
) -> None:
    run_id = f"paid-{uuid.uuid4().hex}"

    response = await async_client.post("/api/runs/", json={"id": run_id, "type": run_type.value})

    assert response.status_code == HTTPStatus.CONFLICT, response.text
    assert await db_session.get(Run, run_id) is None


@pytest.mark.parametrize("status", [None, "", "done", "RUNNING"])
async def test_invalid_status_refuses_the_entire_patch(
    async_client: AsyncClient, status: str | None
) -> None:
    run_id = await _deploy_run(async_client)

    response = await async_client.patch(
        f"/api/runs/{run_id}",
        json={"status": status, "run_metadata": {"must_not_be_written": True}},
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, response.text
    stored = await async_client.get(f"/api/runs/{run_id}")
    assert stored.status_code == HTTPStatus.OK, stored.text
    assert stored.json()["status"] == "queued"
    assert stored.json()["run_metadata"] == {}


async def test_metadata_patch_preserves_an_omitted_status(async_client: AsyncClient) -> None:
    run_id = await _deploy_run(async_client)
    started = await async_client.patch(f"/api/runs/{run_id}", json={"status": "running"})
    assert started.status_code == HTTPStatus.OK, started.text

    response = await async_client.patch(
        f"/api/runs/{run_id}", json={"run_metadata": {"iteration": 2}}
    )

    assert response.status_code == HTTPStatus.OK, response.text
    stored = await async_client.get(f"/api/runs/{run_id}")
    assert stored.status_code == HTTPStatus.OK, stored.text
    assert stored.json()["status"] == "running"
    assert stored.json()["run_metadata"] == {"iteration": 2}


@pytest.mark.parametrize("status", list(RunStatus))
async def test_canonical_status_round_trips_without_requiring_project_or_result(
    async_client: AsyncClient, status: RunStatus
) -> None:
    run_id = await _deploy_run(async_client)

    response = await async_client.patch(f"/api/runs/{run_id}", json={"status": status.value})

    assert response.status_code == HTTPStatus.OK, response.text
    stored = await async_client.get(f"/api/runs/{run_id}")
    assert stored.status_code == HTTPStatus.OK, stored.text
    assert stored.json()["type"] == "deploy"
    assert stored.json()["status"] == status.value
    assert stored.json()["project_id"] is None
    assert stored.json()["result"] is None


@pytest.mark.parametrize(
    "path,params",
    [
        ("/api/runs/", {"run_type": "build"}),
        ("/api/runs/", {"status": "done"}),
        ("/api/applications/1/runs", {"run_type": "build"}),
    ],
)
async def test_invalid_run_filters_are_refused(
    async_client: AsyncClient, path: str, params: dict[str, str]
) -> None:
    response = await async_client.get(path, params=params)

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, response.text


async def test_both_list_routes_filter_the_canonical_run_vocabulary(
    async_client: AsyncClient, db_session: AsyncSession
) -> None:
    application_id = uuid.uuid4().int % 1_000_000_000 + 1
    run_ids = {}
    for run_type in RunType:
        for status in RunStatus:
            run_id = f"filter-{uuid.uuid4().hex}"
            run_ids[run_type, status] = run_id
            db_session.add(
                Run(
                    id=run_id,
                    type=run_type.value,
                    status=status.value,
                    run_metadata={"application_id": application_id},
                )
            )
    await db_session.commit()

    for (run_type, status), run_id in run_ids.items():
        response = await async_client.get(
            "/api/runs/", params={"run_type": run_type.value, "status": status.value}
        )
        assert response.status_code == HTTPStatus.OK, response.text
        rows = response.json()
        assert run_id in {row["id"] for row in rows}
        assert all(row["type"] == run_type.value and row["status"] == status.value for row in rows)

    for run_type in RunType:
        response = await async_client.get(
            f"/api/applications/{application_id}/runs", params={"run_type": run_type.value}
        )
        assert response.status_code == HTTPStatus.OK, response.text
        assert {row["id"] for row in response.json()} == {
            run_ids[run_type, status] for status in RunStatus
        }
