"""Durability tests for application-owned deploy handoffs."""

from http import HTTPStatus
from unittest.mock import AsyncMock, patch

from httpx import AsyncClient
import pytest
from test_application_undeploy_allocations import _application, _project_with_repo, _server

from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.queues.deploy import (
    DEPLOY_HANDOFF_DISPATCHED_AT_KEY,
    DEPLOY_HANDOFF_MESSAGE_KEY,
    DeployAction,
)
from shared.redis.client import RedisStreamClient

async def _deploy_run_for_application(
    client: AsyncClient, repo_id: str, application_id: int
) -> dict:
    repo = await client.get(f"/api/repositories/{repo_id}")
    assert repo.status_code == HTTPStatus.OK, repo.text
    project_id = repo.json()["project_id"]
    runs = await client.get(
        "/api/runs/",
        params={"project_id": project_id, "run_type": "deploy"},
    )
    assert runs.status_code == HTTPStatus.OK, runs.text
    matches = [
        run for run in runs.json() if run["run_metadata"].get("application_id") == application_id
    ]
    assert len(matches) == 1, matches
    return matches[0]


@pytest.mark.asyncio
async def test_stop_publish_failure_leaves_recoverable_queued_handoff(async_client: AsyncClient):
    repo_id = await _project_with_repo(async_client)
    app_id = await _application(async_client, repo_id, await _server(async_client))

    failed_publish = AsyncMock(side_effect=RuntimeError("redis unavailable"))
    with patch.object(RedisStreamClient, "publish_message", failed_publish):
        stopped = await async_client.post(f"/api/applications/{app_id}/stop", json={})

    assert stopped.status_code == HTTPStatus.SERVICE_UNAVAILABLE, stopped.text
    application = await async_client.get(f"/api/applications/{app_id}")
    assert application.json()["status"] == ApplicationStatus.STOPPING.value

    run = await _deploy_run_for_application(async_client, repo_id, app_id)
    assert run["status"] == "queued"
    metadata = run["run_metadata"]
    assert DEPLOY_HANDOFF_DISPATCHED_AT_KEY not in metadata
    message = metadata[DEPLOY_HANDOFF_MESSAGE_KEY]
    assert message["task_id"] == run["id"]
    assert message["application_id"] == app_id
    assert message["action"] == DeployAction.STOP.value


@pytest.mark.asyncio
async def test_successful_stop_stamps_handoff_and_is_visible_in_application_runs(
    async_client: AsyncClient,
):
    repo_id = await _project_with_repo(async_client)
    app_id = await _application(async_client, repo_id, await _server(async_client))

    stopped = await async_client.post(f"/api/applications/{app_id}/stop", json={})
    assert stopped.status_code == HTTPStatus.OK, stopped.text

    run = await _deploy_run_for_application(async_client, repo_id, app_id)
    assert DEPLOY_HANDOFF_DISPATCHED_AT_KEY in run["run_metadata"]

    application_runs = await async_client.get(f"/api/applications/{app_id}/runs")
    assert application_runs.status_code == HTTPStatus.OK, application_runs.text
    assert [row["id"] for row in application_runs.json()] == [run["id"]]
