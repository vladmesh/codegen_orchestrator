"""A failed temporary-access cleanup recovers on its retry and still stops the owed workflow.

Each case drives `process_deploy_job` twice for the same revoke record, the
second time as the retry publishes it, over the fake boundaries of
`tests.unit.temporary_access_doubles`. The deploy consumer reads its lock TTL
from the seeded system config.
"""

from __future__ import annotations

import pytest

from shared.contracts.dto.run import RunStatus
from tests.unit.temporary_access_doubles import (
    TARGET_APPLICATION_ID,
    assert_retryable_failure,
    dispatched_grant_run,
    grant_run,
    operation_env,
    process_operation,
    proves_operation,
)

#: The released grant operation the scheduler replaced; its workflow is still running.
PREDECESSOR_RUN_ID = "temporary-access-grant-a"


def _redeliver(env) -> None:
    """The next revoke Run of the same record, as a retry publishes it."""
    env.api.run_status = RunStatus.QUEUED
    env.api.run_writes.clear()
    env.api.events.clear()
    env.api.grant_reads = 0


@pytest.mark.asyncio
@pytest.mark.parametrize("deployed", [True, False], ids=["deployed", "undeployed"])
@pytest.mark.parametrize("first_attempt", ["unproven-stop", "lock-conflict"])
async def test_a_cleanup_retry_still_stops_the_earlier_grant_workflow(
    deploy_worker_config, deployed, first_attempt
):
    """The retry arrives without the message flag; the grant Run still owes the stop."""
    env = operation_env("revoke")
    if not deployed:
        env.api.allocations[TARGET_APPLICATION_ID] = []
    env.api.history = [dispatched_grant_run(env)]
    env.github.runs = {7: "in_progress"}
    if first_attempt == "unproven-stop":
        env.github.refuse_cancel = True
    else:
        env.stream.redis.value = "another-deploy"

    first = await process_operation(env, fence_active_deploys=True)

    assert first["status"] in {"failed", "cancelled"}
    assert not any(proves_operation(write) for write in env.api.run_writes if "result" in write)
    assert env.github.runs == {7: "in_progress"}
    assert env.product.requests == []

    _redeliver(env)
    env.github.refuse_cancel = False
    env.stream.redis.value = None

    result = await process_operation(env)

    assert result["status"] == "success"
    assert env.github.runs == {7: "completed"}
    if deployed:
        assert env.api.events.index("cancel:7") < env.api.events.index("/users/revoke")
    assert proves_operation(env.api.run_writes[-1])


@pytest.mark.asyncio
@pytest.mark.parametrize("deployed", [True, False], ids=["deployed", "undeployed"])
@pytest.mark.parametrize("replacement", ["unclaimed", "completed"])
async def test_a_cleanup_after_a_replaced_grant_still_stops_the_predecessors_workflow(
    deploy_worker_config, deployed, replacement
):
    """Replacement B is the record's grant Run now; admitted A's workflow can still act."""
    env = operation_env("revoke")
    if not deployed:
        env.api.allocations[TARGET_APPLICATION_ID] = []
    grant = env.api.grant
    current = (
        grant_run(grant, grant.grant_run_id, RunStatus.CANCELLED)
        if replacement == "unclaimed"
        else grant_run(grant, grant.grant_run_id, RunStatus.COMPLETED, claimed=True, succeeded=True)
    )
    env.api.history = [current, dispatched_grant_run(env, PREDECESSOR_RUN_ID)]
    env.github.runs = {7: "in_progress"}
    env.github.refuse_cancel = True

    first = await process_operation(env)

    assert env.github.cancels() == [7]
    assert env.github.runs == {7: "in_progress"}
    assert env.product.requests == []
    assert first["status"] == "failed"
    assert_retryable_failure(env, "could not be proven stopped")

    # A restart or retry recomputes the obligation from the same history.
    _redeliver(env)
    env.github.refuse_cancel = False

    result = await process_operation(env)

    assert result["status"] == "success"
    assert env.github.runs == {7: "completed"}
    if deployed:
        assert env.api.events.index("cancel:7") < env.api.events.index("/users/revoke")
    assert proves_operation(env.api.run_writes[-1])
