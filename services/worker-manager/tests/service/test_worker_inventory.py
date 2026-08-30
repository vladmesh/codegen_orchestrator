"""Service proof that worker inventory reads the records it presents."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import os
import secrets

import docker
import httpx
from redis.asyncio import Redis

from shared.contracts.worker_turn import WorkerActiveTurn, active_turn_key
from shared.queues import STORY_WORKERS_KEY

REDIS_URL = os.environ["REDIS_URL"]
WORKER_MANAGER_URL = os.environ["WORKER_MANAGER_URL"].rstrip("/")
TEST_WORKER_IMAGE = os.environ["TEST_WORKER_IMAGE"]


async def _write_inventory_records(orphan_id: str, attached_id: str, unknown_id: str) -> None:
    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        for worker_id in (orphan_id, attached_id, unknown_id):
            await redis.hset(f"worker:status:{worker_id}", mapping={"status": "RUNNING"})
            await redis.hset(f"worker:meta:{worker_id}", mapping={"attempt_id": f"attempt-{worker_id}"})
        await redis.hset(STORY_WORKERS_KEY, mapping={f"story-{attached_id}": attached_id})
        now = datetime.now(UTC)
        await redis.hset(
            active_turn_key(attached_id),
            mapping=WorkerActiveTurn(
                worker_id=attached_id,
                attempt_id=f"attempt-{attached_id}",
                request_id=f"request-{attached_id}",
                lease_id=f"lease-{attached_id}",
                started_at=now,
                deadline_at=now + timedelta(minutes=5),
            ).as_redis_fields(),
        )
        await redis.hset(active_turn_key(unknown_id), mapping={"not": "a valid lease"})
    finally:
        await redis.aclose()


async def _clear_inventory_records(orphan_id: str, attached_id: str, unknown_id: str) -> None:
    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        await redis.hdel(STORY_WORKERS_KEY, f"story-{attached_id}")
        await redis.delete(
            *[
                key
                for worker_id in (orphan_id, attached_id, unknown_id)
                for key in (
                    f"worker:status:{worker_id}",
                    f"worker:meta:{worker_id}",
                    active_turn_key(worker_id),
                )
            ],
        )
    finally:
        await redis.aclose()


def test_inventory_separates_real_redis_container_lease_binding_and_waiter_facts():
    suffix = secrets.token_hex(4)
    orphan_id = f"inventory-orphan-{suffix}"
    attached_id = f"inventory-attached-{suffix}"
    unknown_id = f"inventory-unknown-{suffix}"
    container_name = f"worker-test-{orphan_id}"
    daemon = docker.from_env()
    container = None
    try:
        container = daemon.containers.run(
            TEST_WORKER_IMAGE,
            command=["python", "-c", "import time; time.sleep(60)"],
            detach=True,
            name=container_name,
        )
        asyncio.run(_write_inventory_records(orphan_id, attached_id, unknown_id))

        response = httpx.get(f"{WORKER_MANAGER_URL}/api/introspect/workers/", timeout=10)
        assert response.status_code == 200, response.text
        workers = {worker["id"]: worker for worker in response.json()}

        orphan = workers[orphan_id]
        assert orphan["container"]["state"] == "running"
        assert orphan["agent_process_status"] == "RUNNING"
        assert orphan["active_turn_lease"] is None
        assert orphan["active_turn_lease_error"] is None
        assert orphan["story_bindings"] == []
        assert orphan["story_bindings_error"] is None
        assert orphan["waiting_attempt"] is None
        assert orphan["waiting_attempt_error"] is None

        attached = workers[attached_id]
        assert attached["active_turn_lease"]["request_id"] == f"request-{attached_id}"
        assert attached["story_bindings"] == [f"story-{attached_id}"]

        unknown = workers[unknown_id]
        assert unknown["active_turn_lease"] is None
        assert unknown["active_turn_lease_error"] == "active turn lease is invalid or unreadable"
    finally:
        asyncio.run(_clear_inventory_records(orphan_id, attached_id, unknown_id))
        if container is not None:
            container.remove(force=True)
