"""The deploy and registry receipts and the deploy lock as real Redis Lua, across writers."""

import os
from unittest.mock import patch
import uuid

from cryptography.fernet import Fernet
import pytest

from shared.clients.github import (
    RegistrySecretsResult,
    registry_lease_key,
    registry_receipt_key,
)
from shared.redis import RedisStreamClient
from src.deploy_fence import DeployFence, DeployFenceLost
from src.subgraphs.devops.deploy_secrets import receipt_key, write_deploy_secrets
from tests.service._github_secrets_repository import OWNER, REPO, Repository

WRITER_ENV = {
    "ORCHESTRATOR_HOSTNAME": "registry.test",
    "REGISTRY_USER": "fixture",
    "REGISTRY_PASSWORD": "fixture-password",  # noqa: S105
    "SECRETS_ENCRYPTION_KEY": Fernet.generate_key().decode(),
    "GITHUB_REGISTRY_SECRETS_LEASE_SECONDS": "180",
}
REGISTRY_NAMES = ["REGISTRY_URL", "REGISTRY_USER", "REGISTRY_PASSWORD"]
VALUES = {
    "DOTENV": "VE9LRU49c2VydmljZQo=",
    "DEPLOY_HOST": "192.0.2.20",
    "DEPLOY_USER": "deploy",
    "DEPLOY_SSH_KEY": "synthetic-service-key",
    "DEPLOY_PORT": "8080",
    "PROJECT_NAME": "receipt-service",
}


@pytest.fixture
async def stores():
    stream = RedisStreamClient()
    await stream.connect()
    project_id = f"receipt-{uuid.uuid4().hex}"
    repository = Repository()
    with patch.dict(os.environ, WRITER_ENV):
        yield stream.redis, project_id, repository
    await stream.redis.delete(
        f"deploy:{project_id}:lock",
        receipt_key(project_id),
        registry_lease_key(repository.id),
        registry_receipt_key(repository.id),
    )
    await stream.close()
    await repository.http.aclose()


async def _deploy(redis, project_id, task_id, values, repository) -> bool:
    fence = DeployFence.for_job(redis, project_id, task_id)
    assert await fence.acquire(60)
    try:
        return await write_deploy_secrets(
            repository.client(), OWNER, REPO, values, deploy_fence=fence
        )
    finally:
        await fence.release()


async def test_a_receipt_warmed_by_another_writer_leaves_an_unchanged_redeploy_no_put(stores):
    redis, project_id, repository = stores
    warmed = await repository.client().ensure_registry_secrets(
        OWNER, REPO, redis=redis, lease_seconds=180
    )
    assert warmed == RegistrySecretsResult(written=3)

    assert await _deploy(redis, project_id, "deploy-a", VALUES, repository)
    assert repository.puts == [*REGISTRY_NAMES, *VALUES]
    key_reads = repository.key_reads
    assert await _deploy(redis, project_id, "deploy-b", VALUES, repository)

    assert repository.puts == [*REGISTRY_NAMES, *VALUES]
    assert repository.key_reads == key_reads


async def test_a_deploy_that_lost_its_lock_mid_registry_write_certifies_nothing(stores):
    redis, project_id, repository = stores

    async def hand_over():
        await redis.set(f"deploy:{project_id}:lock", "deploy-c:other", ex=60)

    repository.on_put = hand_over
    with pytest.raises(DeployFenceLost):
        await _deploy(redis, project_id, "deploy-b", VALUES, repository)

    assert repository.puts == ["REGISTRY_URL"]
    assert await redis.get(registry_receipt_key(repository.id)) is None
    assert await redis.get(registry_lease_key(repository.id)) is None
    assert await redis.get(receipt_key(project_id)) is None
    assert await redis.get(f"deploy:{project_id}:lock") == "deploy-c:other"
    await redis.delete(f"deploy:{project_id}:lock")
    repository.on_put = None
    assert await _deploy(redis, project_id, "deploy-d", VALUES, repository)
    assert repository.puts == ["REGISTRY_URL", *REGISTRY_NAMES, *VALUES]


async def test_a_holder_that_lost_the_lock_cannot_certify_its_payload(stores):
    redis, project_id, repository = stores
    assert await _deploy(redis, project_id, "deploy-a", VALUES, repository)
    changed = {**VALUES, "DOTENV": "VE9LRU49cm90YXRlZAo="}

    async def hand_over():
        if repository.puts[-1] == "PROJECT_NAME":
            await redis.set(f"deploy:{project_id}:lock", "deploy-c:other", ex=60)

    repository.on_put = hand_over
    with pytest.raises(DeployFenceLost):
        await _deploy(redis, project_id, "deploy-b", changed, repository)

    assert await redis.get(receipt_key(project_id)) is None
    await redis.delete(f"deploy:{project_id}:lock")
    repository.on_put = None
    repository.puts.clear()
    assert await _deploy(redis, project_id, "deploy-d", changed, repository)
    assert repository.puts == list(VALUES)
