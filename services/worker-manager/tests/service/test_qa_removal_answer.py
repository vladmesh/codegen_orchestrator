"""A QA executor's delete answer, produced by the real consumer and manager.

The QA runtime keeps the shared QA Telegram identity held until worker-manager's
answer to an executor's delete proves the sandbox gone. This drives that answer's
real producer: `WorkerCommandConsumer` over the CI Redis, `WorkerManager` and
`WorkerRemoval` over the real Docker daemon, with real executor and egress-proxy
containers. The only thing injected is a Docker removal failure, as an unavailable
backend produces it; the answer must then say the removal is unproven, and the
container must in fact still exist.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets

import docker
import pytest
from redis.asyncio import Redis

from shared.contracts.queues.worker import DeleteWorkerCommand, WorkerOwnership
from shared.queues import WORKER_RESPONSES
from shared.redis import RedisStreamClient
from src import qa_egress
from src.config import settings
from src.consumer import WorkerCommandConsumer
from src.manager import WorkerManager


async def _answer(redis: Redis, group: str, request_id: str) -> dict:
    for _ in range(50):
        read = await redis.xreadgroup(
            group, "qa-test", {WORKER_RESPONSES: ">"}, count=10, block=200
        )
        for _, entries in read or []:
            for _, fields in entries:
                answer = json.loads(fields["data"])
                if answer.get("request_id") == request_id:
                    return answer
    raise AssertionError("worker-manager never answered the delete")


@pytest.mark.parametrize("fault", [None, "executor", "proxy"])
async def test_a_qa_executor_delete_succeeds_only_when_docker_shows_it_gone(fault):
    worker_id = f"qa-{secrets.token_hex(6)}"
    executor_name = f"{settings.WORKER_IMAGE_PREFIX}-{worker_id}"
    proxy_name = qa_egress.proxy_container_name(worker_id)
    failing = {"executor": executor_name, "proxy": proxy_name}.get(fault)
    ownership = WorkerOwnership(
        project_id=f"project-{worker_id}", run_id=f"run-{worker_id}", attempt_id=worker_id
    )
    redis = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    stream = RedisStreamClient(redis_url=os.environ["REDIS_URL"])
    await stream.connect()
    manager = WorkerManager(redis)
    consumer = WorkerCommandConsumer(client=stream, manager=manager)
    group = f"qa-removal-{secrets.token_hex(4)}"
    daemon = docker.from_env()
    created = []
    try:
        for name in (executor_name, proxy_name):
            created.append(
                await asyncio.to_thread(
                    daemon.containers.create, os.environ["TEST_WORKER_IMAGE"], name=name
                )
            )
        await redis.hset(
            f"worker:meta:{worker_id}",
            mapping={**ownership.as_redis_meta(), "worker_type": "qa"},
        )
        if failing is not None:
            remove = manager.docker.remove_container

            async def backend_unavailable(name, *args, **kwargs):
                if name == failing:
                    raise RuntimeError("Docker backend unavailable")
                return await remove(name, *args, **kwargs)

            manager.docker.remove_container = backend_unavailable
        await redis.xgroup_create(WORKER_RESPONSES, group, id="$", mkstream=True)
        command = DeleteWorkerCommand(
            request_id=f"cleanup-{worker_id}", worker_id=worker_id, reason="completed"
        )

        await consumer.publish_response(command, await consumer.handle_command(command))
        answer = await _answer(redis, group, command.request_id)

        assert answer["success"] is (fault is None)
        for container in created:
            if container.name == failing:
                await asyncio.to_thread(container.reload)  # still there: the answer said so
            else:
                with pytest.raises(docker.errors.NotFound):
                    await asyncio.to_thread(container.reload)
        if fault is not None:
            assert answer["error"].startswith("QA executor removal not proven")
    finally:
        await redis.xgroup_destroy(WORKER_RESPONSES, group)
        for container in created:
            try:
                await asyncio.to_thread(container.remove, force=True)
            except docker.errors.NotFound:
                pass
        daemon.close()
        await manager._unregister_broker_worker(worker_id)
        await redis.delete(f"worker:meta:{worker_id}")
        await stream.close()
        await redis.aclose()
