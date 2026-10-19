"""The registry secrets lease and receipt as real Redis Lua, across clients and holders."""

import base64
from datetime import UTC, datetime, timedelta
import os
import random
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet
import httpx
from nacl import public
import pytest

from shared.clients.github import (
    GitHubAppClient,
    RegistrySecretsNotRefreshedError,
    RegistrySecretsRefusal,
    RegistrySecretsResult,
    registry_lease_key,
    registry_receipt_key,
)
from shared.redis import RedisStreamClient

OWNER, REPO = "fixture", "registry-receipt"
OTHER_HOLDER = "other-holder-token"
REGISTRY_ENV = {
    "ORCHESTRATOR_HOSTNAME": "registry.test",
    "REGISTRY_USER": "fixture",
    "REGISTRY_PASSWORD": "fixture-password",  # noqa: S105
    "SECRETS_ENCRYPTION_KEY": Fernet.generate_key().decode(),
}
REGISTRY_NAMES = ["REGISTRY_URL", "REGISTRY_USER", "REGISTRY_PASSWORD"]


class Repository:
    """A GitHub repository with a fresh id, its Actions secrets API over controlled HTTP."""

    def __init__(self) -> None:
        self.id = random.randrange(10**9, 10**10)  # noqa: S311
        self._key = base64.b64encode(bytes(public.PrivateKey.generate().public_key)).decode()
        self.puts: list[str] = []
        self.on_put = None
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix(f"/repos/{OWNER}/{REPO}")
        if path == "":
            return httpx.Response(
                200,
                json={
                    "id": self.id,
                    "name": REPO,
                    "full_name": f"{OWNER}/{REPO}",
                    "html_url": f"https://github.com/{OWNER}/{REPO}",
                    "clone_url": f"https://github.com/{OWNER}/{REPO}.git",
                },
            )
        if path == "/actions/secrets/public-key":
            return httpx.Response(200, json={"key": self._key, "key_id": "k1"})
        self.puts.append(path.rsplit("/", 1)[1])
        if self.on_put is not None:
            await self.on_put()
        return httpx.Response(204)

    def client(self) -> GitHubAppClient:
        with patch.dict(os.environ, {"GITHUB_APP_ID": "12345"}):
            github = GitHubAppClient()
        github._http_client = self.http
        github._token_cache[111] = ("token", datetime.now(UTC) + timedelta(hours=1))
        github.get_installation_id = AsyncMock(return_value=111)
        return github


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


async def _ensure(repository: Repository, redis) -> RegistrySecretsResult:
    return await repository.client().ensure_registry_secrets(
        OWNER, REPO, redis=redis, lease_seconds=60
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
