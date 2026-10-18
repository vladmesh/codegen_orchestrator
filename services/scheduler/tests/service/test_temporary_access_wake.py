"""A committed grant or revoke proof wakes the real-Redis temporary-access sweep early.

The sweep sees only this test's grant: the service database is shared, and reconciling
a foreign grant here would dispatch operations or release QA another test owns.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import os

from _qa_queue import qa_messages_for
from _scheduler_wake_service import BACKSTOP_SECONDS, WAIT_SECONDS, wake_channel_subscribed
from _temporary_access_grants import (
    OnlyGrant,
    RawAPI,
    complete_operation,
    finish_qa_run,
    grant_for,
    start_grant,
    target_with_qa_run,
)
import httpx
import pytest

from shared.contracts.dto.temporary_access import TemporaryAccessRevokeReason, TemporaryAccessStatus
from shared.contracts.queues.scheduler_wake import SchedulerWakeTarget
from shared.redis import RedisStreamClient
from src.tasks.temporary_access import supervise_temporary_access

TEMPORARY_ACCESS = SchedulerWakeTarget.TEMPORARY_ACCESS


@pytest.fixture
def client(api_client) -> RawAPI:
    return RawAPI(api_client)


@pytest.fixture
async def redis_client():
    redis = RedisStreamClient(os.environ["REDIS_URL"])
    await redis.connect()
    try:
        yield redis
    finally:
        await redis.close()


async def _set_status(client: RawAPI, run_id: str, status: str) -> None:
    updated = await client.patch(f"/api/runs/{run_id}", json={"status": status})
    assert updated.status_code == httpx.codes.OK, updated.text


@asynccontextmanager
async def _woken_sweep(
    api_client, redis_client, grant_id: str, monkeypatch
) -> AsyncIterator[tuple[asyncio.Queue, dict[SchedulerWakeTarget, asyncio.Event]]]:
    """The real listener and temporary-access loop, each sweep's counts queued as it ends."""
    from src.clients import api as api_module
    from src.tasks import scheduler_wake_listener, temporary_access_loop

    monkeypatch.setattr(api_module, "api_client", OnlyGrant(api_client, grant_id))
    monkeypatch.setattr(
        temporary_access_loop, "_temporary_access_interval", lambda: BACKSTOP_SECONDS
    )
    monkeypatch.setattr(scheduler_wake_listener, "_reconnect_interval", lambda: BACKSTOP_SECONDS)
    cycles: asyncio.Queue[dict[str, int]] = asyncio.Queue()
    sweep = temporary_access_loop.supervise_temporary_access

    async def observed_sweep(api, redis):
        counts = await sweep(api, redis)
        cycles.put_nowait(counts)
        return counts

    monkeypatch.setattr(temporary_access_loop, "supervise_temporary_access", observed_sweep)
    wakes = {target: asyncio.Event() for target in SchedulerWakeTarget}
    listener = asyncio.create_task(scheduler_wake_listener.scheduler_wake_listener(wakes))
    loop = None
    try:
        await wake_channel_subscribed(redis_client)
        loop = asyncio.create_task(
            temporary_access_loop.temporary_access_loop(wake=wakes[TEMPORARY_ACCESS])
        )
        yield cycles, wakes
    finally:
        tasks = [task for task in (loop, listener) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _others_woken(wakes: dict[SchedulerWakeTarget, asyncio.Event]) -> list:
    return [
        target for target, wake in wakes.items() if target is not TEMPORARY_ACCESS and wake.is_set()
    ]


@pytest.mark.asyncio
async def test_a_grant_proof_releases_qa_before_the_backstop(
    client, api_client, redis_client, monkeypatch
):
    project_id, application_id, qa_run_id = await target_with_qa_run(client)
    try:
        await start_grant(api_client, redis_client, project_id, application_id, qa_run_id)
        grant = await grant_for(client, qa_run_id)
        await _set_status(client, grant["grant_run_id"], "running")

        async with _woken_sweep(api_client, redis_client, grant["id"], monkeypatch) as (
            cycles,
            wakes,
        ):
            initial = await asyncio.wait_for(cycles.get(), WAIT_SECONDS)
            assert initial["released"] == 0
            granting = await grant_for(client, qa_run_id)
            assert granting["status"] == TemporaryAccessStatus.GRANTING.value
            assert granting["qa_dispatched_at"] is None
            assert await qa_messages_for(redis_client, qa_run_id) == 0

            await complete_operation(client, grant["grant_run_id"], application_id, None)
            woken = await asyncio.wait_for(cycles.get(), WAIT_SECONDS)

            assert woken["released"] == 1
            granted = await grant_for(client, qa_run_id)
            assert granted["status"] == TemporaryAccessStatus.GRANTED.value
            assert granted["qa_dispatched_at"] is not None
            assert await qa_messages_for(redis_client, qa_run_id) == 1
            assert _others_woken(wakes) == []
    finally:
        await finish_qa_run(client, qa_run_id)


@pytest.mark.asyncio
async def test_a_revoke_proof_closes_the_grant_before_the_backstop(
    client, api_client, redis_client, monkeypatch
):
    project_id, application_id, qa_run_id = await target_with_qa_run(client)
    await start_grant(api_client, redis_client, project_id, application_id, qa_run_id)
    grant = await grant_for(client, qa_run_id)
    scoped = OnlyGrant(api_client, grant["id"])
    await complete_operation(client, grant["grant_run_id"], application_id, None)
    await supervise_temporary_access(scoped, redis_client)
    await finish_qa_run(client, qa_run_id)
    await supervise_temporary_access(scoped, redis_client)
    revoking = await grant_for(client, qa_run_id)
    assert revoking["status"] == TemporaryAccessStatus.REVOKING.value
    assert revoking["revoke_reason"] == TemporaryAccessRevokeReason.RUN_TERMINAL.value
    await _set_status(client, revoking["revoke_run_id"], "running")
    assert await qa_messages_for(redis_client, qa_run_id) == 1

    async with _woken_sweep(api_client, redis_client, grant["id"], monkeypatch) as (
        cycles,
        wakes,
    ):
        initial = await asyncio.wait_for(cycles.get(), WAIT_SECONDS)
        assert initial["revoked"] == 0
        still = await grant_for(client, qa_run_id)
        assert still["status"] == TemporaryAccessStatus.REVOKING.value
        assert still["revoked_at"] is None

        await complete_operation(client, revoking["revoke_run_id"], application_id, None)
        woken = await asyncio.wait_for(cycles.get(), WAIT_SECONDS)

        assert woken["revoked"] == 1
        revoked = await grant_for(client, qa_run_id)
        assert revoked["status"] == TemporaryAccessStatus.REVOKED.value
        assert revoked["revoked_at"] is not None
        assert revoked["revoke_run_id"] == revoking["revoke_run_id"]
        assert await qa_messages_for(redis_client, qa_run_id) == 1
        assert _others_woken(wakes) == []


@pytest.mark.asyncio
async def test_a_predecessor_proof_wakes_the_sweep_without_settling_the_current_grant(
    client, api_client, redis_client, monkeypatch
):
    project_id, application_id, qa_run_id = await target_with_qa_run(client)
    try:
        await start_grant(api_client, redis_client, project_id, application_id, qa_run_id)
        grant = await grant_for(client, qa_run_id)
        predecessor = grant["grant_run_id"]
        # A deploy-lock cancellation: the sweep replaces the operation without effect.
        await _set_status(client, predecessor, "cancelled")

        async with _woken_sweep(api_client, redis_client, grant["id"], monkeypatch) as (
            cycles,
            _wakes,
        ):
            initial = await asyncio.wait_for(cycles.get(), WAIT_SECONDS)
            assert initial["dispatched"] == 1
            current = (await grant_for(client, qa_run_id))["grant_run_id"]
            assert current != predecessor

            # The cancelled operation's worker reports a late success all the same.
            await complete_operation(client, predecessor, application_id, None)
            woken = await asyncio.wait_for(cycles.get(), WAIT_SECONDS)

            assert woken["released"] == 0
            assert woken["dispatched"] == 0
            unsettled = await grant_for(client, qa_run_id)
            assert unsettled["status"] == TemporaryAccessStatus.GRANTING.value
            assert unsettled["grant_run_id"] == current
            assert unsettled["qa_dispatched_at"] is None
            assert await qa_messages_for(redis_client, qa_run_id) == 0
    finally:
        await finish_qa_run(client, qa_run_id)
