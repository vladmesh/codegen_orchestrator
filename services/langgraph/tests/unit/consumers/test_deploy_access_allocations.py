"""A temporary-access operation acts on its target's existing deployment and never allocates.

These drive `process_deploy_job` end to end against the real allocation code in
`src.allocations`, backed by an in-memory allocation table instead of the API.
What the table ends up holding is the assertion: on 2026-09-25 a QA revoke that
arrived after the suite undeployed its target re-created all four ports, failed
its precheck, and left the application owning allocations nothing runs on.

The allocations read are always those of the grant's recorded target
application. A repository can hold one application per server, and the product
allocator reuses whichever the API lists first; another row's allocations, empty
or not, say nothing about whether the target's access is gone.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from shared.contracts.dto.application import ApplicationDTO
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
#: A second application of the same repository, on another server. The API
#: lists it first, so the product allocator's repository lookup would pick it.
OTHER_APPLICATION_ID = 11
HEAD_SHA = "a" * 40
_NOW = datetime.now(UTC)
SERVERS = {
    handle: make_server(handle=handle, public_ip=ip, last_health_check=_NOW)
    for handle, ip in (("srv-1", "1.2.3.4"), ("srv-a", "5.6.7.8"))
}
SERVER = SERVERS["srv-1"]


def _not_found(path: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", f"http://api/{path}")
    return httpx.HTTPStatusError(
        "404 Not Found", request=request, response=httpx.Response(404, request=request)
    )


class AllocationTable:
    """The slice of the API the allocator reads and writes, kept in memory."""

    def __init__(self, *, applications=(), allocations=()):
        self.applications = [dict(app) for app in applications]
        self.allocations = [dict(alloc) for alloc in allocations]
        self.get_or_create_application = AsyncMock(side_effect=self._get_or_create_application)
        self.allocate_next_port = AsyncMock(side_effect=self._allocate_next_port)
        self.get_application_allocations = AsyncMock(side_effect=self._application_allocations)

    async def list_applications(self, filters):
        return [
            app
            for app in self.applications
            if all(app.get(key) == value for key, value in filters.items())
        ]

    async def get_application(self, application_id):
        for app in self.applications:
            if app["id"] == application_id:
                return ApplicationDTO(**app, created_at=_NOW, updated_at=_NOW)
        raise _not_found(f"applications/{application_id}")

    async def list_servers(self, *, is_managed):
        return [SERVER]

    async def get_server(self, handle):
        return SERVERS[handle]

    async def list_active_incidents(self):
        return []

    async def _application_allocations(self, application_id):
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
                "server_ip": SERVERS[server_handle].public_ip,
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


def _other_application(status: str):
    """The repository's other application, named so the API lists it first."""
    return {
        "id": OTHER_APPLICATION_ID,
        "repo_id": REPO_ID,
        "server_handle": "srv-a",
        "service_name": "a-test-project-0000",
        "status": status,
        "reserved_ram_mb": 512,
    }


def _deployed_allocations(application_id: int = TARGET_APPLICATION_ID, server=SERVER):
    first_port = 8000 if application_id == TARGET_APPLICATION_ID else 9000
    return [
        {
            "server_handle": server.handle,
            "port": first_port + index,
            "server_ip": server.public_ip,
            "service_name": module,
            "application_id": application_id,
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
    """The consumer-side API; allocations and applications come from `AllocationTable`."""
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


async def _process(table: AllocationTable, redis, deploy_api, action: str = "feature") -> dict:
    from src.consumers.deploy import process_deploy_job

    deploy_api.get_application = AsyncMock(side_effect=table.get_application)
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


def _assert_failed_closed(result, deploy_api, table, precheck, devops) -> None:
    """A failed access operation: nothing placed, nothing checked, never the no-deployment proof."""
    table.allocate_next_port.assert_not_awaited()
    table.get_or_create_application.assert_not_awaited()
    precheck.assert_not_awaited()
    devops.assert_not_called()
    assert result["status"] == "failed"
    [recorded] = _run_patches(deploy_api)
    assert recorded["status"] == RunStatus.FAILED.value
    assert recorded["result"]["deploy_outcome"] == DeployOutcome.OWNER_ACCESS_PROOF_FAILED.value


@pytest.mark.asyncio
async def test_revoke_after_undeploy_settles_as_revoked_without_allocating(
    redis, deploy_api, precheck, devops
):
    """The stand run's revoke: nothing allocated, no precheck or SSH, a proved revoke."""
    _use_access_operation(deploy_api, "revoke")
    table = AllocationTable(applications=[_undeployed_application()])

    result = await _process(table, redis, deploy_api)

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
async def test_grant_without_deployment_fails_through_grant_failure_without_allocating(
    redis, deploy_api, precheck, devops
):
    _use_access_operation(deploy_api, "grant")
    table = AllocationTable(applications=[_undeployed_application()])

    result = await _process(table, redis, deploy_api)

    assert table.allocations == []
    _assert_failed_closed(result, deploy_api, table, precheck, devops)


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

    result = await _process(table, redis, deploy_api)

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

    await _process(table, redis, deploy_api)

    assert table.allocations == deployed
    table.allocate_next_port.assert_not_awaited()


@pytest.mark.asyncio
async def test_revoke_of_a_deployed_target_ignores_an_undeployed_sibling_listed_first(
    redis, deploy_api, precheck, devops
):
    """Another application of the repository has no allocations; the target still does.

    Reading the sibling would call the target undeployed and close a grant whose
    access is live. The revoke has to run against the target's own deployment.
    """
    _use_access_operation(deploy_api, "revoke")
    table = AllocationTable(
        applications=[
            _other_application("not_deployed"),
            {**_undeployed_application(), "status": "running"},
        ],
        allocations=_deployed_allocations(),
    )

    result = await _process(table, redis, deploy_api)

    table.get_application_allocations.assert_awaited_once_with(TARGET_APPLICATION_ID)
    precheck.assert_awaited_once()
    allocated_resources = precheck.await_args.args[0]
    assert {entry["application_id"] for entry in allocated_resources.values()} == {
        TARGET_APPLICATION_ID
    }
    devops.return_value.ainvoke.assert_awaited_once()
    assert result.get("reason") != "not_deployed"
    table.allocate_next_port.assert_not_awaited()


@pytest.mark.asyncio
async def test_revoke_of_an_undeployed_target_ignores_a_live_sibling_listed_first(
    redis, deploy_api, precheck, devops
):
    """The sibling is live; the target is not, so its access went with its deployment."""
    _use_access_operation(deploy_api, "revoke")
    sibling_allocations = _deployed_allocations(OTHER_APPLICATION_ID, SERVERS["srv-a"])
    table = AllocationTable(
        applications=[_other_application("running"), _undeployed_application()],
        allocations=sibling_allocations,
    )

    result = await _process(table, redis, deploy_api)

    table.get_application_allocations.assert_awaited_once_with(TARGET_APPLICATION_ID)
    assert table.allocations == sibling_allocations
    table.allocate_next_port.assert_not_awaited()
    precheck.assert_not_awaited()
    devops.assert_not_called()
    assert result["status"] == "success"
    assert result["reason"] == "not_deployed"
    [recorded] = _run_patches(deploy_api)
    assert recorded["status"] == RunStatus.COMPLETED.value
    assert recorded["result"]["deploy_outcome"] == DeployOutcome.SUCCESS.value


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["grant", "revoke"])
async def test_access_operation_whose_target_row_is_gone_fails_closed(
    redis, deploy_api, precheck, devops, operation
):
    """A target that cannot be read is not a target proved undeployed."""
    _use_access_operation(deploy_api, operation)
    table = AllocationTable(applications=[_other_application("not_deployed")])

    result = await _process(table, redis, deploy_api)

    assert table.applications == [_other_application("not_deployed")]
    assert table.allocations == []
    table.get_application_allocations.assert_not_awaited()
    _assert_failed_closed(result, deploy_api, table, precheck, devops)


@pytest.mark.asyncio
async def test_revoke_whose_target_allocations_cannot_be_read_fails_closed(
    redis, deploy_api, precheck, devops
):
    _use_access_operation(deploy_api, "revoke")
    table = AllocationTable(applications=[_undeployed_application()])
    table.get_application_allocations.side_effect = httpx.ConnectError("api unreachable")

    result = await _process(table, redis, deploy_api)

    _assert_failed_closed(result, deploy_api, table, precheck, devops)


@pytest.mark.asyncio
async def test_revoke_whose_grant_cannot_be_read_fails_closed(redis, deploy_api, precheck, devops):
    _use_access_operation(deploy_api, "revoke")
    deploy_api.get_temporary_access_grant = AsyncMock(
        side_effect=_not_found("temporary-access-grants/tempaccess-qa-1")
    )
    table = AllocationTable(applications=[_undeployed_application()])

    result = await _process(table, redis, deploy_api)

    table.get_application_allocations.assert_not_awaited()
    _assert_failed_closed(result, deploy_api, table, precheck, devops)


@pytest.mark.asyncio
async def test_operation_without_a_grant_id_fails_closed_instead_of_deploying(
    redis, deploy_api, precheck, devops
):
    """A capability operation that names no grant never falls through to a placing deploy."""
    deploy_api.get_run = AsyncMock(
        return_value=make_run(id=TASK_ID, run_metadata={"temporary_access_operation": "revoke"})
    )
    table = AllocationTable(applications=[_undeployed_application()])

    result = await _process(table, redis, deploy_api)

    assert table.allocations == []
    table.get_application_allocations.assert_not_awaited()
    _assert_failed_closed(result, deploy_api, table, precheck, devops)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["create", "feature"])
async def test_product_deploy_after_undeploy_still_allocates(
    redis, deploy_api, precheck, devops, action
):
    table = AllocationTable(applications=[_undeployed_application()])

    result = await _process(table, redis, deploy_api, action)

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

    result = await _process(table, redis, deploy_api, "create")

    table.get_or_create_application.assert_awaited_once()
    assert len(table.allocations) == 3
    assert result["status"] == "success"
