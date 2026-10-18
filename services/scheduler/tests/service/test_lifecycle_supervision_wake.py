"""A committed deploy success wakes the real-Redis lifecycle sweep into its QA handoff early.

The loop's status scans see only this test's story: the service database is shared, and a
foreign deploying story handed to QA here would hold a paid-work slot another test needs.
"""

import asyncio
import json
import os
import uuid

from _scheduler_wake_service import (
    BACKSTOP_SECONDS,
    WAIT_SECONDS,
    call_api,
    wake_channel_subscribed,
)
from _story_scope import OnlyStory
import pytest

from shared.contracts.dto.qa_handoff import QA_DISPATCHED_AT_KEY
from shared.contracts.dto.run import RunType
from shared.contracts.dto.run_result import DeployRunResult
from shared.contracts.queues.deploy import DeployOutcome
from shared.contracts.queues.scheduler_wake import SchedulerWakeTarget
from shared.queues import QA_QUEUE
from shared.redis import RedisStreamClient
from shared.tests.ssh_key_fixtures import fleet_private_key

HEAD_SHA = "e" * 40
DEPLOYED_URL = "http://10.9.0.23:8000"


async def _deploying_story(api_client) -> tuple[str, str, int]:
    """A DEPLOYING story whose running deploy targets a running application."""
    suffix = uuid.uuid4().hex[:8]
    telegram_id = uuid.uuid4().int % 1_000_000_000
    await call_api(
        api_client,
        "POST",
        "users/",
        json={"telegram_id": telegram_id, "username": f"lifecycle-wake-{telegram_id}"},
    )
    project_id = str(uuid.uuid4())
    await call_api(
        api_client,
        "POST",
        "projects/",
        json={
            "id": project_id,
            "title": "Lifecycle wake",
            "initiating_run_id": f"init-{suffix}",
            "status": "active",
            "config": {"workspace_ready": True},
        },
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    server = await call_api(
        api_client,
        "POST",
        "servers/",
        json={
            "handle": f"lifecycle-wake-{suffix}",
            "host": "lifecycle-wake.test",
            "public_ip": "10.9.0.23",
            "ssh_key": fleet_private_key(),
        },
    )
    repository = await call_api(
        api_client,
        "POST",
        "repositories/",
        json={
            "project_id": project_id,
            "name": f"lifecycle-wake-{suffix}",
            "git_url": f"https://github.com/test/lifecycle-wake-{suffix}.git",
        },
    )
    application = await call_api(
        api_client,
        "POST",
        "applications/",
        json={
            "repo_id": repository["id"],
            "server_handle": server["handle"],
            "service_name": f"lifecycle-wake-{suffix}",
            "status": "running",
        },
    )
    story = await call_api(
        api_client, "POST", "stories/", json={"project_id": project_id, "title": "Wake"}
    )
    for action in ("start", "deploy"):
        await call_api(api_client, "POST", f"stories/{story['id']}/{action}")
    run_id = f"deploy-lifecycle-wake-{uuid.uuid4().hex[:12]}"
    await call_api(
        api_client,
        "POST",
        "runs/",
        json={
            "id": run_id,
            "type": RunType.DEPLOY.value,
            "project_id": project_id,
            "story_id": story["id"],
            "run_metadata": {"application_id": application["id"], "head_sha": HEAD_SHA},
        },
    )
    await call_api(api_client, "PATCH", f"runs/{run_id}", json={"status": "running"})
    return story["id"], run_id, application["id"]


async def _qa_runs(api_client, story_id: str) -> list[dict]:
    return await call_api(
        api_client, "GET", "runs/", params={"story_id": story_id, "run_type": RunType.QA.value}
    )


async def _qa_messages_for(redis_client: RedisStreamClient, qa_run_id: str) -> int:
    entries = await redis_client.redis.xrange(QA_QUEUE)
    return sum(1 for _, fields in entries if json.loads(fields["data"])["run_id"] == qa_run_id)


async def _settle_qa(api_client, story_id: str) -> None:
    """Cancel the QA Run the handoff started, so later suites keep their paid-work slot."""
    for run in await _qa_runs(api_client, story_id):
        if run["status"] in ("queued", "running"):
            await call_api(api_client, "PATCH", f"runs/{run['id']}", json={"status": "cancelled"})


@pytest.mark.asyncio
async def test_a_deploy_success_hands_its_story_to_qa_before_the_backstop(api_client, monkeypatch):
    from src.clients import api as api_module
    from src.tasks import lifecycle_supervision_loop, scheduler_wake_listener

    story_id, run_id, application_id = await _deploying_story(api_client)
    monkeypatch.setattr(api_module, "api_client", OnlyStory(api_client, story_id))
    monkeypatch.setattr(
        lifecycle_supervision_loop, "_lifecycle_supervision_interval", lambda: BACKSTOP_SECONDS
    )
    monkeypatch.setattr(scheduler_wake_listener, "_reconnect_interval", lambda: BACKSTOP_SECONDS)
    cycles: asyncio.Queue[dict[str, int]] = asyncio.Queue()
    supervise_lifecycle_once = lifecycle_supervision_loop.supervise_lifecycle_once

    async def observed_cycle(api, redis_client):
        counts = await supervise_lifecycle_once(api, redis_client)
        cycles.put_nowait(counts)
        return counts

    monkeypatch.setattr(lifecycle_supervision_loop, "supervise_lifecycle_once", observed_cycle)

    wakes = {target: asyncio.Event() for target in SchedulerWakeTarget}
    observer = RedisStreamClient(os.environ["REDIS_URL"])
    await observer.connect()
    listener = asyncio.create_task(scheduler_wake_listener.scheduler_wake_listener(wakes))
    loop = None
    try:
        await wake_channel_subscribed(observer)
        loop = asyncio.create_task(
            lifecycle_supervision_loop.lifecycle_supervision_loop(
                wake=wakes[SchedulerWakeTarget.LIFECYCLE_SUPERVISION]
            )
        )
        initial = await asyncio.wait_for(cycles.get(), WAIT_SECONDS)
        assert initial["deploying_tested"] == 0
        assert (await call_api(api_client, "GET", f"stories/{story_id}"))["status"] == "deploying"
        assert await _qa_runs(api_client, story_id) == []

        await call_api(
            api_client,
            "PATCH",
            f"runs/{run_id}",
            json={
                "status": "completed",
                "result": DeployRunResult(
                    deploy_outcome=DeployOutcome.SUCCESS,
                    deployed_url=DEPLOYED_URL,
                    application_id=application_id,
                ).model_dump(mode="json"),
            },
        )

        woken = await asyncio.wait_for(cycles.get(), WAIT_SECONDS)

        assert woken["deploying_tested"] == 1
        assert (await call_api(api_client, "GET", f"stories/{story_id}"))["status"] == "testing"
        [qa_run] = await _qa_runs(api_client, story_id)
        assert qa_run["run_metadata"]["deploy_run_id"] == run_id
        assert qa_run["run_metadata"][QA_DISPATCHED_AT_KEY]
        assert await _qa_messages_for(observer, qa_run["id"]) == 1
        assert not wakes[SchedulerWakeTarget.STORY_COMPLETION].is_set()
        assert not wakes[SchedulerWakeTarget.TASK_DISPATCHER].is_set()
    finally:
        tasks = [task for task in (loop, listener) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await observer.close()
        await _settle_qa(api_client, story_id)
