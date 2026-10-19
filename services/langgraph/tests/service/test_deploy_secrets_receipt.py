"""The deploy secrets receipt and the deploy lock as real Redis Lua, across deploy jobs."""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import uuid

import pytest

from shared.redis import RedisStreamClient
from src.deploy_fence import DeployFence, DeployFenceLost
from src.subgraphs.devops.deploy_secrets import receipt_key, write_deploy_secrets

REGISTRY_ENV = {
    "ORCHESTRATOR_HOSTNAME": "registry.test",
    "REGISTRY_USER": "fixture",
    "REGISTRY_PASSWORD": "fixture-password",  # noqa: S105
}
REGISTRY_ENV_NAMES = ["REGISTRY_URL", "REGISTRY_USER", "REGISTRY_PASSWORD"]
VALUES = {
    "DOTENV": "VE9LRU49c2VydmljZQo=",
    "DEPLOY_HOST": "192.0.2.20",
    "DEPLOY_USER": "deploy",
    "DEPLOY_SSH_KEY": "synthetic-service-key",
    "DEPLOY_PORT": "8080",
    "PROJECT_NAME": "receipt-service",
}


@pytest.fixture
async def redis():
    stream = RedisStreamClient()
    await stream.connect()
    project_id = f"receipt-{uuid.uuid4().hex}"
    with patch.dict(os.environ, REGISTRY_ENV):
        yield stream.redis, project_id
    await stream.redis.delete(f"deploy:{project_id}:lock", receipt_key(project_id))
    await stream.close()


def _github(written: list[list[str]], on_write=None) -> AsyncMock:
    async def write(owner, repo, values, *, guard):
        written.append(list(values))
        for _ in values:
            await guard()
        if on_write is not None:
            await on_write()
        return len(values)

    github = AsyncMock()
    github.get_repo = AsyncMock(return_value=SimpleNamespace(id=1001))
    github.set_repository_secrets = AsyncMock(side_effect=write)
    return github


async def _deploy(redis, project_id, task_id, values, github) -> bool:
    fence = DeployFence.for_job(redis, project_id, task_id)
    assert await fence.acquire(60)
    try:
        return await write_deploy_secrets(github, "fixture", "receipt", values, deploy_fence=fence)
    finally:
        await fence.release()


async def test_a_second_deploy_job_skips_the_six_certified_secrets(redis):
    redis, project_id = redis
    written: list[list[str]] = []

    assert await _deploy(redis, project_id, "deploy-a", VALUES, _github(written))
    assert await _deploy(redis, project_id, "deploy-b", VALUES, _github(written))

    assert written == [[*VALUES, *REGISTRY_ENV_NAMES], REGISTRY_ENV_NAMES]


async def test_a_holder_that_lost_the_lock_cannot_certify_its_payload(redis):
    redis, project_id = redis
    written: list[list[str]] = []
    assert await _deploy(redis, project_id, "deploy-a", VALUES, _github(written))
    changed = {**VALUES, "DOTENV": "VE9LRU49cm90YXRlZAo="}

    async def hand_over():
        await redis.set(f"deploy:{project_id}:lock", "deploy-c:other", ex=60)

    with pytest.raises(DeployFenceLost):
        await _deploy(redis, project_id, "deploy-b", changed, _github(written, hand_over))

    assert await redis.get(receipt_key(project_id)) is None
    await redis.delete(f"deploy:{project_id}:lock")
    assert await _deploy(redis, project_id, "deploy-d", changed, _github(written))
    assert written[-1] == [*VALUES, *REGISTRY_ENV_NAMES]
