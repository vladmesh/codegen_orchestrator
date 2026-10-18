"""A committed QA verdict wakes the real-Redis QA routing and temporary-access loops early.

Each loop sees only this test's story and grant: the service database is shared, and routing a
foreign story or revoking a foreign grant here would act on work another test owns.
"""

import asyncio
import os

from _scheduler_wake_service import BACKSTOP_SECONDS, WAIT_SECONDS, wake_channel_subscribed
from _story_scope import OnlyStory
from _temporary_access_grants import (
    OnlyGrant,
    RawAPI,
    complete_operation,
    finish_qa_run,
    grant_for,
    start_grant,
    story_target_with_qa_run,
)
import httpx
import pytest

from shared.contracts.dto.temporary_access import TemporaryAccessRevokeReason, TemporaryAccessStatus
from shared.contracts.queues.scheduler_wake import SchedulerWakeTarget
from shared.redis import RedisStreamClient
from src.tasks.temporary_access import supervise_temporary_access

QA_ROUTING = SchedulerWakeTarget.QA_ROUTING
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


async def _read(client: RawAPI, path: str) -> dict:
    response = await client.get(path)
    assert response.status_code == httpx.codes.OK, response.text
    return response.json()


async def _granted_testing_story(client: RawAPI, api_client, redis_client) -> tuple:
    """A TESTING story whose running QA run holds proved, released temporary access."""
    project_id, application_id, story_id, qa_run_id = await story_target_with_qa_run(client)
    await start_grant(api_client, redis_client, project_id, application_id, qa_run_id)
    grant = await grant_for(client, qa_run_id)
    await complete_operation(client, grant["grant_run_id"], application_id, None)
    counts = await supervise_temporary_access(OnlyGrant(api_client, grant["id"]), redis_client)
    assert counts["released"] == 1
    running = await client.patch(f"/api/runs/{qa_run_id}", json={"status": "running"})
    assert running.status_code == httpx.codes.OK, running.text
    return application_id, story_id, qa_run_id, grant["id"]


@pytest.mark.asyncio
async def test_a_passed_verdict_routes_the_story_and_starts_cleanup_before_the_backstop(
    client, api_client, redis_client, monkeypatch
):
    from src.clients import api as api_module
    from src.tasks import qa_routing_loop, scheduler_wake_listener, temporary_access_loop

    application_id, story_id, qa_run_id, grant_id = await _granted_testing_story(
        client, api_client, redis_client
    )
    try:
        monkeypatch.setattr(
            api_module, "api_client", OnlyStory(OnlyGrant(api_client, grant_id), story_id)
        )
        monkeypatch.setattr(qa_routing_loop, "_qa_routing_interval", lambda: BACKSTOP_SECONDS)
        monkeypatch.setattr(
            temporary_access_loop, "_temporary_access_interval", lambda: BACKSTOP_SECONDS
        )
        monkeypatch.setattr(
            scheduler_wake_listener, "_reconnect_interval", lambda: BACKSTOP_SECONDS
        )
        routings: asyncio.Queue[dict[str, int]] = asyncio.Queue()
        sweeps: asyncio.Queue[dict[str, int]] = asyncio.Queue()
        route = qa_routing_loop.supervise_testing_stories
        sweep = temporary_access_loop.supervise_temporary_access

        async def observed_routing(api, redis):
            counts = await route(api, redis)
            routings.put_nowait(counts)
            return counts

        async def observed_sweep(api, redis):
            counts = await sweep(api, redis)
            sweeps.put_nowait(counts)
            return counts

        monkeypatch.setattr(qa_routing_loop, "supervise_testing_stories", observed_routing)
        monkeypatch.setattr(temporary_access_loop, "supervise_temporary_access", observed_sweep)

        wakes = {target: asyncio.Event() for target in SchedulerWakeTarget}
        listener = asyncio.create_task(scheduler_wake_listener.scheduler_wake_listener(wakes))
        loops: list[asyncio.Task] = []
        try:
            await wake_channel_subscribed(redis_client)
            loops = [
                asyncio.create_task(qa_routing_loop.qa_routing_loop(wake=wakes[QA_ROUTING])),
                asyncio.create_task(
                    temporary_access_loop.temporary_access_loop(wake=wakes[TEMPORARY_ACCESS])
                ),
            ]
            initial_routing = await asyncio.wait_for(routings.get(), WAIT_SECONDS)
            initial_sweep = await asyncio.wait_for(sweeps.get(), WAIT_SECONDS)
            assert initial_routing["completed"] == 0
            assert initial_sweep["dispatched"] == 0
            assert (await _read(client, f"/api/stories/{story_id}"))["status"] == "testing"
            granted = await grant_for(client, qa_run_id)
            assert granted["status"] == TemporaryAccessStatus.GRANTED.value
            assert granted["revoke_run_id"] is None

            await finish_qa_run(client, qa_run_id)
            routed = await asyncio.wait_for(routings.get(), WAIT_SECONDS)
            swept = await asyncio.wait_for(sweeps.get(), WAIT_SECONDS)

            assert routed["completed"] == 1
            assert swept["dispatched"] == 1
            assert (await _read(client, f"/api/stories/{story_id}"))["status"] == "completed"
            assert (await _read(client, f"/api/runs/{qa_run_id}"))["qa_routed_at"] is not None
            revoking = await grant_for(client, qa_run_id)
            assert revoking["status"] == TemporaryAccessStatus.REVOKING.value
            assert revoking["revoke_reason"] == TemporaryAccessRevokeReason.RUN_TERMINAL.value
            assert revoking["revoked_at"] is None
            revoke_run = await _read(client, f"/api/runs/{revoking['revoke_run_id']}")
            assert revoke_run["status"] == "queued"
            assert revoke_run["run_metadata"]["temporary_access_grant_id"] == grant_id
            assert revoke_run["run_metadata"]["temporary_access_operation"] == "revoke"
            assert [target for target, wake in wakes.items() if wake.is_set()] == []
        finally:
            tasks = [*loops, listener]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        # Close the test's grant so no later sweep inherits its revoke.
        await complete_operation(client, revoking["revoke_run_id"], application_id, None)
        await supervise_temporary_access(OnlyGrant(api_client, grant_id), redis_client)
        assert (await grant_for(client, qa_run_id))["status"] == TemporaryAccessStatus.REVOKED.value
    finally:
        await finish_qa_run(client, qa_run_id)
