"""A temporary-access operation acts on an existing deployment and never allocates.

These drive `process_deploy_job` end to end against the real allocation code in
`src.allocations`, backed by an in-memory allocation table instead of the API.
What the table ends up holding is the assertion: on 2026-09-25 a QA revoke that
arrived after the suite undeployed its target re-created all four ports, failed
its precheck, and left the application owning allocations nothing runs on.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.temporary_access import (
    TemporaryAccessGrantDTO,
    TemporaryAccessRevokeReason,
    TemporaryAccessStatus,
)
from shared.contracts.queues.deploy import DeployOutcome, DeployTrigger
from tests.unit.factories import (
    make_project,
    make_repository,
    make_run,
    make_run_start,
    make_server,
)

TASK_ID = "temporary-access-op-1"
PROJECT_ID = "proj-1"
REPO_ID = "repo-1"
TARGET_APPLICATION_ID = 17
HEAD_SHA = "a" * 40
SERVER = make_server(last_health_check=datetime.now(UTC))


class AllocationTable:
    """The slice of the API the allocator reads and writes, kept in memory."""

    def __init__(self, *, applications=(), allocations=()):
        self.applications = [dict(app) for app in applications]
        self.allocations = [dict(alloc) for alloc in allocations]
        self.get_or_create_application = AsyncMock(side_effect=self._get_or_create_application)
        self.allocate_next_port = AsyncMock(side_effect=self._allocate_next_port)

    async def list_applications(self, filters):
        return [
            app
            for app in self.applications
            if all(app.get(key) == value for key, value in filters.items())
        ]

    async def list_servers(self, *, is_managed):
        return [SERVER]

    async def get_server(self, handle):
        return SERVER

    async def list_active_incidents(self):
        return []

    async def get_application_allocations(self, application_id):
        return [alloc for alloc in self.allocations if alloc["application_id"] == application_id]

    async def _get_or_create_application(
        self, *, repo_id, server_handle, service_name, reserved_ram_mb
    ):
        app = {
            "id": TARGET_APPLICATION_ID,
            "repo_id": repo_id,
            "server_handle": server_handle,
            "service_name": service_name,
            "status": "not_deployed",
            "reserved_ram_mb": reserved_ram_mb,
        }
        self.applications.append(app)
        return app

    async def _allocate_next_port(self, server_handle, body):
        port = 8000 + len(self.allocations)
        self.allocations.append(
            {
                "server_handle": server_handle,
                "port": port,
                "server_ip": SERVER.public_ip,
                **body,
            }
        )
        return {"port": port}


def _undeployed_application():
    """What undeploy leaves behind: the application row, and no allocations."""
    return {
        "id": TARGET_APPLICATION_ID,
        "repo_id": REPO_ID,
        "server_handle": SERVER.handle,
        "service_name": "test-project-0000",
        "status": "not_deployed",
        "reserved_ram_mb": 512,
    }


def _deployed_allocations():
    return [
        {
            "server_handle": SERVER.handle,
            "port": 8000 + index,
            "server_ip": SERVER.public_ip,
            "service_name": module,
            "application_id": TARGET_APPLICATION_ID,
        }
        for index, module in enumerate(["backend", "postgres", "redis"])
    ]


def _grant(operation: str) -> TemporaryAccessGrantDTO:
    now = datetime.now(UTC)
    return TemporaryAccessGrantDTO(
        id="tempaccess-qa-1",
        project_id=PROJECT_ID,
        channel="telegram",
        external_id="8202532144",
        target_application_id=TARGET_APPLICATION_ID,
        target_base_url="https://exact.example.com",
        head_sha=HEAD_SHA,
        qa_run_id="qa-1",
        grant_run_id=TASK_ID if operation == "grant" else "temporary-access-grant-0",
        revoke_run_id=TASK_ID if operation == "revoke" else None,
        revoke_reason=TemporaryAccessRevokeReason.RUN_TERMINAL if operation == "revoke" else None,
        qa_message={
            "story_id": "story-1",
            "project_id": PROJECT_ID,
            "initiating_run_id": "deploy-1",
            "telegram_chat_id": "",
            "deployed_url": "https://exact.example.com",
            "application_id": TARGET_APPLICATION_ID,
            "acceptance_criteria": "bot admission",
            "run_id": "qa-1",
        },
        status=(
            TemporaryAccessStatus.GRANTING
            if operation == "grant"
            else TemporaryAccessStatus.REVOKING
        ),
        granted_at=now,
        created_at=now,
    )


def _job(action: str = "feature") -> dict:
    return {
        "task_id": TASK_ID,
        "project_id": PROJECT_ID,
        "unaddressed_reason": "temporary QA capability operation",
        "callback_stream": "",
        "story_id": "",
        "triggered_by": DeployTrigger.ADMIN.value,
        "action": action,
        "head_sha": HEAD_SHA,
    }


@pytest.fixture
def redis():
    client = AsyncMock()
    client.redis = AsyncMock()
    client.redis.set = AsyncMock(return_value=True)
    client.redis.delete = AsyncMock()
    client.redis.exists = AsyncMock(return_value=False)
    return client


@pytest.fixture
def deploy_api():
    """The consumer-side API; allocations go through `AllocationTable` instead."""
    with (
        patch("src.consumers.deploy.api_client") as api,
        patch("src.consumers.deploy_result_handler.api_client", api),
        patch("src.consumers.deploy_failure_handler.api_client", api),
        patch("src.consumers.deploy_precheck.api_client", api),
    ):
        api.patch = AsyncMock()
        api.get = AsyncMock(return_value=[])
        api.get_run = AsyncMock(return_value=make_run(id=TASK_ID))
        api.start_run = AsyncMock(return_value=make_run_start(run_id=TASK_ID))
        api.get_project = AsyncMock(
            return_value=make_project(config={"modules": ["backend"], "estimated_ram_mb": 512})
        )
        api.get_primary_repository = AsyncMock(
            return_value=make_repository(id=REPO_ID, git_url="https://github.com/org/p")
        )
        api.get_project_initial_settings_brief = AsyncMock(return_value=None)
        yield api


@pytest.fixture
def precheck():
    with patch("src.consumers.deploy._run_deploy_precheck", AsyncMock(return_value=None)) as spy:
        yield spy


@pytest.fixture
def devops():
    with patch("src.consumers.deploy.create_devops_subgraph") as factory:
        factory.return_value.ainvoke = AsyncMock(
            return_value={
                "deployed_url": "https://exact.example.com",
                "application_id": TARGET_APPLICATION_ID,
                "deployment_result": {},
                "secret_values": {"USERS_GRANT_CAPABILITY": "capability-value"},
            }
        )
        yield factory


def _use_access_operation(deploy_api, operation: str) -> None:
    grant = _grant(operation)
    deploy_api.get_run = AsyncMock(
        return_value=make_run(
            id=TASK_ID,
            run_metadata={
                "temporary_access_grant_id": grant.id,
                "temporary_access_operation": operation,
            },
        )
    )
    deploy_api.get_temporary_access_grant = AsyncMock(return_value=grant)


def _run_patches(deploy_api) -> list[dict]:
    return [
        call.kwargs["json"]
        for call in deploy_api.patch.call_args_list
        if call.args[0] == f"runs/{TASK_ID}"
    ]


async def _process(table: AllocationTable, redis, action: str = "feature") -> dict:
    from src.consumers.deploy import process_deploy_job

    settings = SimpleNamespace(
        allocation_ram_reserve_mb=256, allocation_metrics_freshness_seconds=300
    )
    with (
        patch("src.allocations.api_client", table),
        patch("src.allocations.get_settings", return_value=settings),
        patch("src.consumers.deploy_result_handler.GeneratedServiceGrantClient") as client,
    ):
        client.return_value.grant_and_resolve = AsyncMock(
            return_value=SimpleNamespace(active=True, failure=None)
        )
        client.return_value.revoke_and_resolve = AsyncMock(
            return_value=SimpleNamespace(active=False, failure=None)
        )
        return await process_deploy_job(_job(action), redis)


@pytest.mark.asyncio
async def test_revoke_after_undeploy_settles_as_revoked_without_allocating(
    redis, deploy_api, precheck, devops
):
    """The stand run's revoke: nothing allocated, no precheck or SSH, a proved revoke."""
    _use_access_operation(deploy_api, "revoke")
    table = AllocationTable(applications=[_undeployed_application()])

    result = await _process(table, redis)

    assert table.allocations == []
    table.allocate_next_port.assert_not_awaited()
    table.get_or_create_application.assert_not_awaited()
    precheck.assert_not_awaited()
    devops.assert_not_called()
    assert result["status"] == "success"
    [recorded] = _run_patches(deploy_api)
    assert recorded["status"] == RunStatus.COMPLETED.value
    assert recorded["result"]["deploy_outcome"] == DeployOutcome.SUCCESS.value
    # The supervisor and the API refuse a skipped run as revoke proof; this is none.
    assert recorded["result"]["skipped_reason"] is None


@pytest.mark.asyncio
async def test_revoke_for_a_project_without_any_application_creates_none(
    redis, deploy_api, precheck, devops
):
    """Not even an Application row is created to answer a revoke."""
    _use_access_operation(deploy_api, "revoke")
    table = AllocationTable()

    result = await _process(table, redis)

    assert table.applications == []
    assert table.allocations == []
    precheck.assert_not_awaited()
    devops.assert_not_called()
    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_grant_without_deployment_fails_through_grant_failure_without_allocating(
    redis, deploy_api, precheck, devops
):
    _use_access_operation(deploy_api, "grant")
    table = AllocationTable(applications=[_undeployed_application()])

    result = await _process(table, redis)

    assert table.allocations == []
    table.allocate_next_port.assert_not_awaited()
    table.get_or_create_application.assert_not_awaited()
    precheck.assert_not_awaited()
    devops.assert_not_called()
    assert result["status"] == "failed"
    [recorded] = _run_patches(deploy_api)
    assert recorded["status"] == RunStatus.FAILED.value
    assert recorded["result"]["deploy_outcome"] == DeployOutcome.OWNER_ACCESS_PROOF_FAILED.value


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["grant", "revoke"])
async def test_access_operation_on_a_deployment_reads_its_allocations_and_proceeds(
    redis, deploy_api, precheck, devops, operation
):
    _use_access_operation(deploy_api, operation)
    table = AllocationTable(
        applications=[{**_undeployed_application(), "status": "running"}],
        allocations=_deployed_allocations(),
    )

    result = await _process(table, redis)

    assert table.allocations == _deployed_allocations()
    table.allocate_next_port.assert_not_awaited()
    precheck.assert_awaited_once()
    allocated_resources = precheck.await_args.args[0]
    assert sorted(entry["port"] for entry in allocated_resources.values()) == [8000, 8001, 8002]
    devops.return_value.ainvoke.assert_awaited_once()
    assert result["status"] == "success"
    [recorded] = _run_patches(deploy_api)
    assert recorded["status"] == RunStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_access_operation_never_fills_in_a_module_the_deployment_lacks(
    redis, deploy_api, precheck, devops
):
    """Read means read: a missing module port is a product deploy's to create."""
    _use_access_operation(deploy_api, "revoke")
    deployed = [alloc for alloc in _deployed_allocations() if alloc["service_name"] != "redis"]
    table = AllocationTable(
        applications=[{**_undeployed_application(), "status": "running"}],
        allocations=deployed,
    )

    await _process(table, redis)

    assert table.allocations == deployed
    table.allocate_next_port.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["create", "feature"])
async def test_product_deploy_after_undeploy_still_allocates(
    redis, deploy_api, precheck, devops, action
):
    table = AllocationTable(applications=[_undeployed_application()])

    result = await _process(table, redis, action)

    assert sorted(alloc["service_name"] for alloc in table.allocations) == [
        "backend",
        "postgres",
        "redis",
    ]
    precheck.assert_awaited_once()
    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_first_product_deploy_still_places_the_application(
    redis, deploy_api, precheck, devops
):
    table = AllocationTable()

    result = await _process(table, redis, "create")

    table.get_or_create_application.assert_awaited_once()
    assert len(table.allocations) == 3
    assert result["status"] == "success"
