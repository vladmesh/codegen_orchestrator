"""Native broker Lua expiry and canonical owned teardown, on the CI Redis stack."""

import asyncio
import json
import os
import secrets

import docker
import httpx
import pytest
from redis.asyncio import Redis

from shared.contracts.dto.commit_publication import publication_pending_key
from shared.contracts.queues.worker import WorkerOwnership
from shared.contracts.worker_evidence import removed_worker_evidence_key
from shared.worker_output_receipts import OUTPUT_RECEIPT_TTL_SECONDS
from src.config import settings
from src.manager import WorkerManager


async def _assert_native_replay(client, redis, worker_id, submission, worker_headers):
    output_url = f"/v1/workers/{worker_id}/output"
    accepted = await client.post(output_url, headers=worker_headers, json=submission)
    accepted.raise_for_status()
    receipt = f"worker:output-receipt:{worker_id}:{submission['lease_id']}"
    assert OUTPUT_RECEIPT_TTL_SECONDS - 100 < await redis.ttl(receipt) <= OUTPUT_RECEIPT_TTL_SECONDS
    assert (await redis.xpending(f"worker:{worker_id}:input", "worker_group"))["pending"] == 0
    assert await redis.xlen(f"worker:{worker_id}:output") == 1
    # Native expiry near the end of the window; replay must not renew it.
    await redis.expire(receipt, 180)
    replay = await client.post(output_url, headers=worker_headers, json=submission)
    replay.raise_for_status()
    assert 0 < await redis.ttl(receipt) <= 180
    changed = await client.post(
        output_url,
        headers=worker_headers,
        json={**submission, "result": {"status": "failed", "error": "changed"}},
    )
    assert changed.status_code == 409
    assert await redis.xlen(f"worker:{worker_id}:output") == 1
    return receipt


@pytest.mark.asyncio
@pytest.mark.parametrize("teardown", ["unregister", "manager"])
async def test_native_receipt_expiry_replay_and_owned_teardown(teardown):
    worker_id = f"receipt-{secrets.token_hex(8)}"
    token = secrets.token_urlsafe(32)
    ownership = WorkerOwnership(
        project_id=f"project-{worker_id}",
        run_id=f"run-{worker_id}",
        attempt_id=f"attempt-{worker_id}",
    )
    input_stream = f"worker:{worker_id}:input"
    output_stream = f"worker:{worker_id}:output"
    legacy = [f"worker:output-receipt:{worker_id}:legacy-{index}" for index in range(205)]
    retained = [
        f"worker:output-receipt:{worker_id}-other:1-0",
        publication_pending_key(ownership.attempt_id),
        f"engineering:turn-receipt:{worker_id}",
    ]
    evidence = removed_worker_evidence_key(ownership.run_id)
    redis = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    manager = WorkerManager(redis)
    container = None
    receipt = None
    internal_headers = {"X-Broker-Internal-Token": settings.WORKER_BROKER_INTERNAL_TOKEN}
    worker_headers = {"X-Worker-Broker-Token": token}
    async with httpx.AsyncClient(base_url=os.environ["WORKER_BROKER_URL"], timeout=15) as client:
        try:
            registration = await client.post(
                "/internal/workers",
                headers=internal_headers,
                json={
                    "worker_id": worker_id,
                    "token": token,
                    "worker_type": "developer",
                    "input_stream": input_stream,
                    "output_stream": output_stream,
                },
            )
            registration.raise_for_status()
            await redis.xadd(
                input_stream,
                {"data": json.dumps({"request_id": worker_id, "prompt": "no-model fixture"})},
            )
            lease = await client.post(
                f"/v1/workers/{worker_id}/input/lease", headers=worker_headers
            )
            lease.raise_for_status()
            lease_id = lease.json()["lease_id"]
            submission = {"lease_id": lease_id, "result": {"status": "failed", "error": "fixture"}}
            receipt = await _assert_native_replay(
                client, redis, worker_id, submission, worker_headers
            )
            for index, key in enumerate(legacy):
                await redis.set(key, "signature", ex=86400 if index else None)
            assert await redis.ttl(legacy[0]) == -1
            for key in retained:
                await redis.set(key, "retained")
            await redis.hset(evidence, "previous-worker", "retained")
            if teardown == "manager":
                daemon = docker.from_env()
                try:
                    container = await asyncio.to_thread(
                        daemon.containers.create,
                        os.environ["TEST_WORKER_IMAGE"],
                        name=f"{settings.WORKER_IMAGE_PREFIX}-{worker_id}",
                    )
                    await redis.hset(
                        f"worker:meta:{worker_id}",
                        mapping={**ownership.as_redis_meta(), "worker_type": "developer"},
                    )
                    await manager.delete_worker(worker_id, "completed")
                    with pytest.raises(docker.errors.NotFound):
                        await asyncio.to_thread(container.reload)
                finally:
                    daemon.close()
                assert await redis.hget(evidence, worker_id) is not None
                assert not await redis.exists(f"worker:meta:{worker_id}")
                assert not await redis.exists(input_stream, output_stream)
            else:
                response = await client.delete(
                    f"/internal/workers/{worker_id}", headers=internal_headers
                )
                response.raise_for_status()
            assert not await redis.exists(receipt, *legacy)
            assert await redis.exists(*retained) == len(retained)
            assert await redis.hget(evidence, "previous-worker") == "retained"
            denied = await client.post(
                f"/v1/workers/{worker_id}/output", headers=worker_headers, json=submission
            )
            assert denied.status_code == 403
        finally:
            await manager._unregister_broker_worker(worker_id)
            await redis.delete(
                input_stream,
                output_stream,
                f"worker:meta:{worker_id}",
                evidence,
                *retained,
                *legacy,
                *([receipt] if receipt else []),
            )
            if container is not None:
                try:
                    await asyncio.to_thread(container.remove, force=True)
                except docker.errors.NotFound:
                    pass
            await redis.aclose()
