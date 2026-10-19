"""The registry secrets lease and receipt as real Redis Lua, across clients and holders."""

import os
from unittest.mock import patch

from cryptography.fernet import Fernet
import pytest

from shared.clients.github import (
    RegistrySecretsNotRefreshedError,
    RegistrySecretsRefusal,
    RegistrySecretsResult,
    registry_lease_key,
    registry_receipt_key,
)
from shared.redis import RedisStreamClient
from tests.service._github_secrets_repository import OWNER, REPO, Repository

OTHER_HOLDER = "other-holder-token"
REGISTRY_ENV = {
    "ORCHESTRATOR_HOSTNAME": "registry.test",
    "REGISTRY_USER": "fixture",
    "REGISTRY_PASSWORD": "fixture-password",  # noqa: S105
    "SECRETS_ENCRYPTION_KEY": Fernet.generate_key().decode(),
}
REGISTRY_NAMES = ["REGISTRY_URL", "REGISTRY_USER", "REGISTRY_PASSWORD"]


@pytest.fixture
async def stores(real_redis):
    stream = RedisStreamClient()
    await stream.connect()
    repository = Repository()
    with patch.dict(os.environ, REGISTRY_ENV):
        yield repository, stream.redis, real_redis
    await stream.redis.delete(
        registry_lease_key(repository.id), registry_receipt_key(repository.id)
    )
    await stream.close()
    await repository.http.aclose()


async def _ensure(repository: Repository, redis, caller_lock=None) -> RegistrySecretsResult:
    return await repository.client().ensure_registry_secrets(
        OWNER, REPO, redis=redis, lease_seconds=60, caller_lock=caller_lock
    )


async def test_a_receipt_written_through_one_client_is_a_hit_for_another(stores):
    repository, text_redis, bytes_redis = stores

    assert await _ensure(repository, text_redis) == RegistrySecretsResult(written=3)
    assert await _ensure(repository, bytes_redis) == RegistrySecretsResult(written=0)

    assert repository.puts == REGISTRY_NAMES
    assert await text_redis.get(registry_lease_key(repository.id)) is None


async def test_a_holder_that_lost_the_lease_is_refused_and_the_next_one_repairs(stores):
    repository, text_redis, bytes_redis = stores
    lease_key = registry_lease_key(repository.id)

    async def hand_over():
        await text_redis.set(lease_key, OTHER_HOLDER, ex=60)

    repository.on_put = hand_over
    with pytest.raises(RegistrySecretsNotRefreshedError) as refused:
        await _ensure(repository, bytes_redis)
    assert refused.value.reason is RegistrySecretsRefusal.LEASE_LOST

    repository.on_put = None
    with pytest.raises(RegistrySecretsNotRefreshedError) as busy:
        await _ensure(repository, bytes_redis)
    assert busy.value.reason is RegistrySecretsRefusal.LEASE_BUSY
    assert await text_redis.get(lease_key) == OTHER_HOLDER
    assert await text_redis.get(registry_receipt_key(repository.id)) is None

    await text_redis.delete(lease_key)
    assert await _ensure(repository, text_redis) == RegistrySecretsResult(written=3)
    assert await _ensure(repository, bytes_redis) == RegistrySecretsResult(written=0)
    assert repository.puts == ["REGISTRY_URL", *REGISTRY_NAMES]


@pytest.mark.parametrize("connection", ["text", "bytes"])
@pytest.mark.parametrize(
    "receipt",
    [
        b"\xff\xfe\x80gAAAA",
        Fernet(REGISTRY_ENV["SECRETS_ENCRYPTION_KEY"]).encrypt("é".encode() * 32),
    ],
    ids=["invalid-utf8", "same-key-non-digest"],
)
async def test_a_corrupt_receipt_is_a_miss_repaired_under_the_lease(stores, connection, receipt):
    repository, text_redis, bytes_redis = stores
    redis = text_redis if connection == "text" else bytes_redis
    assert await _ensure(repository, redis) == RegistrySecretsResult(written=3)
    await bytes_redis.set(registry_receipt_key(repository.id), receipt)

    assert await _ensure(repository, redis) == RegistrySecretsResult(written=3)
    assert await _ensure(repository, redis) == RegistrySecretsResult(written=0)
    assert repository.puts == [*REGISTRY_NAMES, *REGISTRY_NAMES]


@pytest.mark.parametrize("connection", ["text", "bytes"])
async def test_a_caller_lock_lost_mid_write_is_checked_with_the_lease_in_one_script(
    stores, connection
):
    repository, text_redis, bytes_redis = stores
    redis = text_redis if connection == "text" else bytes_redis
    caller_key = f"fixture:caller:{repository.id}"
    await text_redis.set(caller_key, "caller-token", ex=60)

    async def hand_over_caller():
        await text_redis.set(caller_key, OTHER_HOLDER, ex=60)

    repository.on_put = hand_over_caller
    try:
        with pytest.raises(RegistrySecretsNotRefreshedError) as refused:
            await _ensure(repository, redis, (caller_key, "caller-token"))
        assert refused.value.reason is RegistrySecretsRefusal.CALLER_LOCK_LOST
        assert repository.puts == ["REGISTRY_URL"]
        assert await text_redis.get(registry_receipt_key(repository.id)) is None
        assert await text_redis.get(registry_lease_key(repository.id)) is None
        assert await text_redis.get(caller_key) == OTHER_HOLDER

        repository.on_put = None
        successor = (caller_key, OTHER_HOLDER)
        assert await _ensure(repository, redis, successor) == RegistrySecretsResult(written=3)
        assert await _ensure(repository, redis, successor) == RegistrySecretsResult(written=0)
    finally:
        await text_redis.delete(caller_key)
