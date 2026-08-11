"""A deploy that cannot be placed must not be recorded as a failed project.

These drive the real `process_deploy_job` path: the allocator refuses, and what
the consumer writes to the run is what the scheduler will route on. The expected
result comes from `shared.tests.allocation_routing_cases`, which the scheduler's
routing test feeds to the supervisor — so the two ends of the boundary are pinned
to one shape.

The consumer records the *same* outcome for every reason on purpose: the reason
and the admission budget travel inside the result, and the scheduler decides what
each disposition does with them. That decision is what must differ per
disposition, and `services/scheduler/tests/unit/test_supervisor_run_routing.py`
is where each one's behaviour is pinned. One typed outcome carrying its reason is
not the collapse; one behaviour for every reason was.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.contracts.dto.run_result import AllocationFailureReason, DeployRunResult
from shared.contracts.queues.deploy import DeployOutcome
from shared.tests.allocation_routing_cases import (
    REFUSED_DEPLOY_MIN_DISK_MB,
    REFUSED_DEPLOY_REASONS,
    REFUSED_DEPLOY_REQUIRED_RAM_MB,
    refused_deploy_result,
)
from shared.tests.server_admission_cases import (
    ADMISSION_CASES,
    admission_case_incidents,
    admission_case_server,
)
from tests.unit.factories import make_project, make_repository, make_run, make_run_start


@pytest.fixture
def mock_redis():
    r = AsyncMock()
    r.redis = AsyncMock()
    r.redis.xadd = AsyncMock()
    r.redis.set = AsyncMock(return_value=True)  # deploy lock acquired
    r.redis.delete = AsyncMock()
    r.redis.incr = AsyncMock(return_value=1)
    r.redis.expire = AsyncMock()
    r.publish_flat = AsyncMock()
    return r


@pytest.fixture
def mock_api():
    with (
        patch("src.consumers.deploy.api_client") as api,
        patch("src.consumers.deploy_precheck.api_client", api),
        patch("src.consumers.deploy_failure_handler.api_client", api),
        patch("src.consumers.deploy_result_handler.api_client", api),
    ):
        api.patch = AsyncMock()
        api.post = AsyncMock()
        api.get = AsyncMock(return_value=[])
        api.get_run = AsyncMock(return_value=make_run())
        api.start_run = AsyncMock(return_value=make_run_start())
        api.get_project = AsyncMock(return_value=make_project(config={"modules": ["backend"]}))
        api.get_primary_repository = AsyncMock(return_value=make_repository())
        api.get_server = AsyncMock(return_value=MagicMock(ssh_user="dev"))
        yield api


def _job() -> dict:
    return {
        "task_id": "deploy-1",
        "project_id": "proj-1",
        "telegram_chat_id": "u1",
        "action": "create",
        "head_sha": "a" * 40,
    }


def _refusal(reason: AllocationFailureReason):
    from src.allocations import AllocationError

    return AllocationError(
        reason,
        required_ram_mb=REFUSED_DEPLOY_REQUIRED_RAM_MB,
        min_disk_mb=REFUSED_DEPLOY_MIN_DISK_MB,
    )


def _recorded_result(api) -> DeployRunResult:
    """Parse what the consumer actually wrote, through the real contract."""
    payload = api.patch.await_args.kwargs["json"]
    return DeployRunResult.model_validate(payload["result"])


@pytest.mark.asyncio
@patch("src.consumers.deploy.create_devops_subgraph")
async def test_unprovisioned_target_is_recorded_as_an_infrastructure_wait(
    mock_devops, mock_redis, mock_api
):
    """The blocker this round exists for: never GIVE_UP, which fails the story."""
    from src.consumers.deploy import process_deploy_job

    with patch(
        "src.allocations.ensure_project_allocations",
        AsyncMock(side_effect=_refusal(AllocationFailureReason.SERVER_NOT_PROVISIONED)),
    ):
        result = await process_deploy_job(_job(), mock_redis)

    recorded = _recorded_result(mock_api)
    expected = refused_deploy_result(AllocationFailureReason.SERVER_NOT_PROVISIONED)
    assert recorded.deploy_outcome is not DeployOutcome.GIVE_UP
    # Everything the scheduler routes and resumes on, compared to the shared
    # shape; `error_details` is a human diagnostic that nothing routes on.
    assert recorded.model_dump(exclude={"error_details"}) == expected.model_dump(
        exclude={"error_details"}
    )
    assert recorded.error_details
    assert result["status"] == "waiting_infrastructure"
    mock_devops.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", REFUSED_DEPLOY_REASONS, ids=lambda r: r.value)
@patch("src.consumers.deploy.create_devops_subgraph")
async def test_every_allocation_refusal_keeps_its_type_across_the_boundary(
    mock_devops, mock_redis, mock_api, reason
):
    """No allocation reason may be flattened into a string on the way out."""
    from src.consumers.deploy import process_deploy_job

    with patch(
        "src.allocations.ensure_project_allocations",
        AsyncMock(side_effect=_refusal(reason)),
    ):
        await process_deploy_job(_job(), mock_redis)

    recorded = _recorded_result(mock_api)
    assert recorded.deploy_outcome is DeployOutcome.WAITING_INFRASTRUCTURE
    assert recorded.allocation_failure_reason is reason
    assert recorded.allocation_required_ram_mb == REFUSED_DEPLOY_REQUIRED_RAM_MB
    assert recorded.allocation_min_disk_mb == REFUSED_DEPLOY_MIN_DISK_MB


@pytest.mark.asyncio
@patch("src.consumers.deploy.create_devops_subgraph")
async def test_redeploy_onto_an_unready_bound_server_waits_instead_of_failing(
    mock_devops, mock_redis, mock_api
):
    """The reuse path's refusal, through the real allocator rather than a stub.

    A project already placed on a host whose provisioning restarted is the
    scenario the admission rule exists for. What matters here is the *outcome*
    the consumer records: an infrastructure wait carrying its reason, not a
    GIVE_UP that would tell this project's owner their project failed.
    """
    from src.consumers.deploy import process_deploy_job

    now = datetime.now(UTC)
    case = next(
        candidate
        for candidate in ADMISSION_CASES
        if candidate.name == "active_while_installing_software"
    )
    server = admission_case_server(case, last_health_check=now)
    allocations_api = AsyncMock()
    allocations_api.list_applications.return_value = [
        {"id": 42, "repo_id": "repo-1", "server_handle": server.handle, "status": "running"}
    ]
    allocations_api.get_server.return_value = server
    allocations_api.list_active_incidents.return_value = admission_case_incidents(
        case, detected_at=now
    )
    settings = SimpleNamespace(
        allocation_ram_reserve_mb=256, allocation_metrics_freshness_seconds=300
    )

    with (
        patch("src.allocations.api_client", allocations_api),
        patch("src.allocations.get_settings", return_value=settings),
    ):
        result = await process_deploy_job(_job(), mock_redis)

    recorded = _recorded_result(mock_api)
    assert recorded.deploy_outcome is DeployOutcome.WAITING_INFRASTRUCTURE
    assert recorded.allocation_failure_reason is AllocationFailureReason.SERVER_NOT_PROVISIONED
    assert result["status"] == "waiting_infrastructure"
    # The refusal happened before anything was handed to the deploy.
    allocations_api.get_application_allocations.assert_not_awaited()
    allocations_api.allocate_next_port.assert_not_awaited()
    mock_devops.assert_not_called()


@pytest.mark.asyncio
@patch("src.consumers.deploy.create_devops_subgraph")
async def test_a_failure_that_is_not_an_allocation_refusal_still_gives_up(
    mock_devops, mock_redis, mock_api
):
    """The wait is for infrastructure only; a missing repository is not that."""
    from src.consumers.deploy import process_deploy_job

    mock_api.get_primary_repository = AsyncMock(return_value=None)

    result = await process_deploy_job(_job(), mock_redis)

    assert _recorded_result(mock_api).deploy_outcome is DeployOutcome.GIVE_UP
    assert result["status"] == "failed"
