"""Registry secrets made current under a repository lease, skipped while a receipt vouches.

The GitHub client is the real one over controlled HTTP, sealing each value with
the repository key; the lease and receipt are the real Lua scripts on an
in-memory Redis shared by every client of the test.
"""

import asyncio
import base64
from datetime import UTC, datetime, timedelta
import json
import os
import re
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet
import fakeredis
import httpx
from nacl import public
import pytest
from redis import exceptions as redis_errors
from structlog.testing import capture_logs

from shared.clients.github import (
    GitHubAppClient,
    RegistrySecretsNotRefreshedError,
    RegistrySecretsRefusal,
    RegistrySecretsResult,
    registry_lease_key,
    registry_receipt_key,
)

OWNER, REPO = "fixture-org", "fixture-product"
OTHER_HOLDER = "other-holder-token"
LEASE_SECONDS = 30
REGISTRY_ENV = {
    "ORCHESTRATOR_HOSTNAME": "registry.example.com",
    "REGISTRY_USER": "registry-user",
    "REGISTRY_PASSWORD": "registry-password-value",  # noqa: S105
}
CHANGED_ENV = {
    "ORCHESTRATOR_HOSTNAME": "registry.rotated.example.com",
    "REGISTRY_USER": "rotated-user",
    "REGISTRY_PASSWORD": "rotated-password-value",  # noqa: S105
}
REGISTRY_SECRETS = {
    "REGISTRY_URL": "registry.example.com",
    "REGISTRY_USER": "registry-user",
    "REGISTRY_PASSWORD": "registry-password-value",
}
REGISTRY_NAMES = list(REGISTRY_SECRETS)
SECRET_FOR_VARIABLE = {
    "ORCHESTRATOR_HOSTNAME": "REGISTRY_URL",
    "REGISTRY_USER": "REGISTRY_USER",
    "REGISTRY_PASSWORD": "REGISTRY_PASSWORD",
}


class Refused(Exception):
    pass


class Repository:
    """The GitHub repository `OWNER/REPO` as the Actions secrets API exposes it."""

    def __init__(self, server: fakeredis.FakeServer) -> None:
        self.server = server
        self.redis = fakeredis.FakeRedis(server=server, decode_responses=True)
        self.id = 4242
        self._key = public.PrivateKey.generate()
        self.secrets: dict[str, str] = {}
        self.requests: list[tuple[str, str]] = []
        self.refuse: set[str] = set()
        self.hand_over_on: tuple[str, str] | None = None
        self.answer_first_put_with: int | None = None
        self.pause_on: tuple[str, str] | None = None
        self.paused = asyncio.Event()
        self.lease_ttls: list[int] = []
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def hand_over(self) -> None:
        """The lease expires and another holder claims it."""
        self.redis.set(registry_lease_key(self.id), OTHER_HOLDER)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix(f"/repos/{OWNER}/{REPO}")
        self.requests.append((request.method, path))
        if (request.method, path) == self.hand_over_on:
            self.hand_over()
        if (request.method, path) == self.pause_on:
            self.paused.set()
            await asyncio.Event().wait()
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
            key = base64.b64encode(bytes(self._key.public_key)).decode()
            return httpx.Response(200, json={"key": key, "key_id": "k1"})
        self.lease_ttls.append(self.redis.pttl(registry_lease_key(self.id)))
        name = path.removeprefix("/actions/secrets/")
        if self.answer_first_put_with is not None:
            status, self.answer_first_put_with = self.answer_first_put_with, None
            return httpx.Response(status)
        if name in self.refuse:
            sending = os.environ["REGISTRY_PASSWORD"]
            raise httpx.ConnectError(f"refused while sending {sending}", request=request)
        sealed = base64.b64decode(json.loads(request.content)["encrypted_value"])
        self.secrets[name] = public.SealedBox(self._key).decrypt(sealed).decode()
        return httpx.Response(204)

    def written(self) -> list[str]:
        return [path.rsplit("/", 1)[1] for method, path in self.requests if method == "PUT"]

    def receipt(self) -> str | None:
        return self.redis.get(registry_receipt_key(self.id))

    def lease(self) -> str | None:
        return self.redis.get(registry_lease_key(self.id))


@pytest.fixture
def server():
    return fakeredis.FakeServer()


@pytest.fixture
async def repository(server):
    repository = Repository(server)
    env = {**REGISTRY_ENV, "SECRETS_ENCRYPTION_KEY": Fernet.generate_key().decode()}
    with (
        patch.dict(os.environ, env),
        patch("shared.clients.github._base.asyncio.sleep", new_callable=AsyncMock),
    ):
        yield repository
    await repository.http.aclose()


def _client(repository: Repository) -> GitHubAppClient:
    """A separate client per caller, as each job constructs its own."""
    with patch.dict(os.environ, {"GITHUB_APP_ID": "12345"}):
        github = GitHubAppClient()
    github._http_client = repository.http
    github._token_cache[111] = ("token", datetime.now(UTC) + timedelta(hours=1))
    github.get_installation_id = AsyncMock(return_value=111)
    return github


async def _ensure(repository: Repository, redis=None, guard=None) -> RegistrySecretsResult:
    if redis is None:
        redis = fakeredis.aioredis.FakeRedis(server=repository.server)
    return await _client(repository).ensure_registry_secrets(
        OWNER, REPO, redis=redis, lease_seconds=LEASE_SECONDS, guard=guard
    )


async def _refused(repository: Repository, redis=None, guard=None) -> RegistrySecretsRefusal:
    with pytest.raises(RegistrySecretsNotRefreshedError) as refused:
        await _ensure(repository, redis, guard)
    return refused.value.reason


@pytest.mark.asyncio
async def test_a_second_client_with_the_same_values_writes_and_reads_no_key(repository):
    assert await _ensure(repository) == RegistrySecretsResult(written=3)
    assert repository.written() == REGISTRY_NAMES
    assert repository.secrets == REGISTRY_SECRETS
    repository.requests.clear()

    with capture_logs() as logs:
        assert await _ensure(repository) == RegistrySecretsResult(written=0)

    assert repository.requests == [("GET", "")]
    current = [entry for entry in logs if entry["event"] == "registry_secrets_current"]
    assert [entry["written"] for entry in current] == [0]


@pytest.mark.asyncio
async def test_the_receipt_is_one_encrypted_digest_holding_no_value(repository):
    with capture_logs() as logs:
        assert await _ensure(repository) == RegistrySecretsResult(written=3)

    assert set(repository.redis.keys("*")) == {registry_receipt_key(repository.id)}
    receipt = repository.receipt()
    assert re.fullmatch(r"[0-9a-f]{64}", receipt) is None
    for value in REGISTRY_SECRETS.values():
        assert value not in receipt
        assert value not in repr(logs)


@pytest.mark.asyncio
async def test_the_lease_held_during_writes_expires_on_its_own(repository):
    assert await _ensure(repository) == RegistrySecretsResult(written=3)

    assert len(repository.lease_ttls) == 3
    assert all(0 < ttl <= LEASE_SECONDS * 1000 for ttl in repository.lease_ttls)
    assert repository.lease() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("variable", list(REGISTRY_ENV))
async def test_any_changed_registry_value_writes_all_three_again(repository, variable):
    assert await _ensure(repository) == RegistrySecretsResult(written=3)
    repository.requests.clear()

    with patch.dict(os.environ, {variable: CHANGED_ENV[variable]}):
        assert await _ensure(repository) == RegistrySecretsResult(written=3)

    assert repository.written() == REGISTRY_NAMES
    assert repository.secrets[SECRET_FOR_VARIABLE[variable]] == CHANGED_ENV[variable]


@pytest.mark.asyncio
async def test_a_repository_recreated_under_the_same_name_is_written_again(repository):
    assert await _ensure(repository) == RegistrySecretsResult(written=3)
    repository.id = 4343
    repository.secrets.clear()

    assert await _ensure(repository) == RegistrySecretsResult(written=3)

    assert repository.secrets == REGISTRY_SECRETS
    assert repository.receipt() is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "receipt",
    [None, "corrupt", "0" * 64, Fernet(Fernet.generate_key()).encrypt(b"0" * 64).decode()],
    ids=["deleted", "corrupt", "plain-digest", "other-key"],
)
async def test_a_missing_or_unreadable_receipt_writes_and_repairs(repository, receipt):
    assert await _ensure(repository) == RegistrySecretsResult(written=3)
    repository.redis.delete(registry_receipt_key(repository.id))
    if receipt is not None:
        repository.redis.set(registry_receipt_key(repository.id), receipt)
    repository.requests.clear()

    assert await _ensure(repository) == RegistrySecretsResult(written=3)
    assert repository.written() == REGISTRY_NAMES
    assert await _ensure(repository) == RegistrySecretsResult(written=0)


@pytest.mark.asyncio
async def test_a_receipt_from_another_encryption_key_is_a_miss(repository):
    assert await _ensure(repository) == RegistrySecretsResult(written=3)
    repository.requests.clear()

    with patch.dict(os.environ, {"SECRETS_ENCRYPTION_KEY": Fernet.generate_key().decode()}):
        assert await _ensure(repository) == RegistrySecretsResult(written=3)
        assert await _ensure(repository) == RegistrySecretsResult(written=0)

    assert repository.written() == REGISTRY_NAMES


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["", "not-a-fernet-key"], ids=["missing", "invalid"])
async def test_an_unusable_encryption_key_refuses_before_any_request(repository, key):
    with patch.dict(os.environ, {"SECRETS_ENCRYPTION_KEY": key}):
        assert await _refused(repository) is RegistrySecretsRefusal.RECEIPT_KEY_INVALID

    assert repository.requests == []
    assert repository.redis.keys("*") == []


@pytest.mark.asyncio
async def test_a_missing_registry_variable_refuses_before_any_request(repository):
    with patch.dict(os.environ, {"REGISTRY_PASSWORD": ""}):
        assert await _refused(repository) is RegistrySecretsRefusal.ENV_MISSING

    assert repository.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("lease_seconds", [0, -5])
async def test_a_lease_duration_that_is_not_positive_is_rejected(repository, lease_seconds):
    with pytest.raises(ValueError, match="lease_seconds"):
        await _client(repository).ensure_registry_secrets(
            OWNER,
            REPO,
            redis=fakeredis.aioredis.FakeRedis(server=repository.server),
            lease_seconds=lease_seconds,
        )

    assert repository.requests == []


@pytest.mark.asyncio
async def test_a_partial_write_of_b_leaves_no_receipt_and_a_is_written_again(repository):
    assert await _ensure(repository) == RegistrySecretsResult(written=3)
    repository.refuse = {"REGISTRY_PASSWORD"}

    with patch.dict(os.environ, CHANGED_ENV), capture_logs() as logs:
        assert await _refused(repository) is RegistrySecretsRefusal.WRITE_INCOMPLETE

    assert repository.secrets["REGISTRY_URL"] == CHANGED_ENV["ORCHESTRATOR_HOSTNAME"]
    assert repository.receipt() is None
    assert repository.lease() is None
    assert "rotated-password-value" not in repr(logs)
    repository.refuse = set()
    repository.requests.clear()
    assert await _ensure(repository) == RegistrySecretsResult(written=3)
    assert repository.written() == REGISTRY_NAMES
    assert repository.secrets == REGISTRY_SECRETS


@pytest.mark.asyncio
async def test_a_live_holder_refuses_at_once_and_keeps_its_lease(repository):
    assert await _ensure(repository) == RegistrySecretsResult(written=3)
    repository.hand_over()
    repository.requests.clear()

    with capture_logs() as logs:
        assert await _refused(repository) is RegistrySecretsRefusal.LEASE_BUSY

    assert repository.requests == [("GET", "")]
    assert repository.lease() == OTHER_HOLDER
    assert repository.receipt() is not None
    assert OTHER_HOLDER not in repr(logs)


@pytest.mark.asyncio
async def test_a_lease_lost_during_a_key_read_sends_no_secret(repository):
    repository.hand_over_on = ("GET", "/actions/secrets/public-key")

    assert await _refused(repository) is RegistrySecretsRefusal.LEASE_LOST

    assert repository.written() == []
    assert repository.lease() == OTHER_HOLDER


@pytest.mark.asyncio
async def test_a_lease_lost_before_a_retry_stops_the_batch(repository):
    repository.answer_first_put_with = 502
    repository.hand_over_on = ("PUT", "/actions/secrets/REGISTRY_URL")

    assert await _refused(repository) is RegistrySecretsRefusal.LEASE_LOST

    assert repository.written() == ["REGISTRY_URL"]
    assert repository.receipt() is None
    assert repository.lease() == OTHER_HOLDER


@pytest.mark.asyncio
async def test_a_lease_lost_after_the_last_write_certifies_nothing(repository):
    repository.hand_over_on = ("PUT", "/actions/secrets/REGISTRY_PASSWORD")

    assert await _refused(repository) is RegistrySecretsRefusal.LEASE_LOST

    assert repository.written() == REGISTRY_NAMES
    assert repository.receipt() is None
    assert repository.lease() == OTHER_HOLDER
    repository.redis.delete(registry_lease_key(repository.id))
    repository.hand_over_on = None
    repository.requests.clear()
    assert await _ensure(repository) == RegistrySecretsResult(written=3)


@pytest.mark.asyncio
async def test_a_lease_lost_after_the_receipt_read_is_no_hit(repository):
    assert await _ensure(repository) == RegistrySecretsResult(written=3)

    class HandOverAfterFirstScript(fakeredis.aioredis.FakeRedis):
        async def eval(self, *args):
            result = await super().eval(*args)
            if repository.lease() != OTHER_HOLDER:
                repository.hand_over()
            return result

    redis = HandOverAfterFirstScript(server=repository.server)
    repository.requests.clear()

    assert await _refused(repository, redis) is RegistrySecretsRefusal.LEASE_LOST

    assert repository.written() == []
    assert repository.lease() == OTHER_HOLDER


@pytest.mark.asyncio
async def test_the_caller_guard_runs_before_every_put_and_its_refusal_is_raised_unchanged(
    repository,
):
    checks: list[int] = []

    async def guard() -> None:
        checks.append(len(repository.written()))
        if len(checks) == 2:
            raise Refused("caller fence lost")

    with pytest.raises(Refused):
        await _ensure(repository, guard=guard)

    assert checks == [0, 1]
    assert repository.written() == ["REGISTRY_URL"]
    assert repository.receipt() is None
    assert repository.lease() is None


@pytest.mark.asyncio
async def test_a_cancelled_call_releases_its_lease_and_certifies_nothing(repository):
    repository.pause_on = ("PUT", "/actions/secrets/REGISTRY_USER")
    call = asyncio.ensure_future(_ensure(repository))
    await repository.paused.wait()
    assert repository.lease() is not None

    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call

    assert repository.lease() is None
    assert repository.receipt() is None


@pytest.mark.asyncio
async def test_an_unreachable_lease_store_is_no_hit_and_writes_nothing(repository):
    assert await _ensure(repository) == RegistrySecretsResult(written=3)
    repository.requests.clear()
    repository.server.connected = False

    with pytest.raises(redis_errors.ConnectionError):
        await _ensure(repository)

    assert repository.written() == []
