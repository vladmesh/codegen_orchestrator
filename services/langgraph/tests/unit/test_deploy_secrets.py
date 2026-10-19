"""A deploy's repository secrets: written when they change, skipped while receipts vouch.

The GitHub client is the real one over controlled HTTP, sealing each value with
the repository key; the receipts, the registry lease and the deploy lock are the
real Lua scripts on an in-memory Redis shared by every deploy of the test.
"""

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
from structlog.testing import capture_logs

from shared.clients.github import (
    GitHubAppClient,
    RegistrySecretsResult,
    registry_lease_key,
    registry_receipt_key,
)
from src.deploy_fence import DeployFence, DeployFenceLost
from src.subgraphs.devops.deploy_secrets import receipt_key, write_deploy_secrets

OWNER, REPO = "my-org", "reminder-bot"
PROJECT_ID = "proj-receipt"
OTHER_HOLDER = "deploy-b:4f1c"
REGISTRY_ENV = {
    "ORCHESTRATOR_HOSTNAME": "registry.example.com",
    "REGISTRY_USER": "registry-user",
    "REGISTRY_PASSWORD": "registry-password-value",  # noqa: S105
}
REGISTRY_SECRETS = {
    "REGISTRY_URL": "registry.example.com",
    "REGISTRY_USER": "registry-user",
    "REGISTRY_PASSWORD": "registry-password-value",
}
REGISTRY_NAMES = list(REGISTRY_SECRETS)
WRITER_ENV = {
    **REGISTRY_ENV,
    "SECRETS_ENCRYPTION_KEY": Fernet.generate_key().decode(),
    "GITHUB_REGISTRY_SECRETS_LEASE_SECONDS": "180",
}
DOTENV = base64.b64encode(b"BACKEND_IMAGE=registry.example.com/bot:sha-b\nTOKEN=canary\n").decode()
VALUES = {
    "DOTENV": DOTENV,
    "DEPLOY_HOST": "192.0.2.10",
    "DEPLOY_USER": "deploy",
    "DEPLOY_SSH_KEY": "-----BEGIN OPENSSH PRIVATE KEY-----\nsynthetic\n-----END-----",
    "DEPLOY_PORT": "8080",
    "PROJECT_NAME": "reminder-bot-0000",
}
CHANGED = {
    "DOTENV": base64.b64encode(b"TOKEN=rotated\n").decode(),
    "DEPLOY_HOST": "192.0.2.11",
    "DEPLOY_USER": "ops",
    "DEPLOY_SSH_KEY": "-----BEGIN OPENSSH PRIVATE KEY-----\nrotated\n-----END-----",
    "DEPLOY_PORT": "8081",
    "PROJECT_NAME": "reminder-bot-0001",
}


class Repository:
    """The GitHub repository `OWNER/REPO` as the Actions secrets API exposes it."""

    def __init__(self, server: fakeredis.FakeServer) -> None:
        self.server = server
        self.redis = fakeredis.FakeRedis(server=server, decode_responses=True)
        self.id = 1001
        self._key = public.PrivateKey.generate()
        self.secrets: dict[str, str] = {}
        self.requests: list[tuple[str, str]] = []
        self.refuse: set[str] = set()
        self.hand_over_on: tuple[str, str] | None = None
        self.answer_first_put_with: int | None = None
        self.unreachable = False
        # One pool for every client: building an httpx client loads the TLS trust store.
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def hand_over(self) -> None:
        """The deploy's lock expires and another deploy claims the project."""
        self.redis.set(f"deploy:{PROJECT_ID}:lock", OTHER_HOLDER)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix(f"/repos/{OWNER}/{REPO}")
        self.requests.append((request.method, path))
        if (request.method, path) == self.hand_over_on:
            self.hand_over()
        if path == "" and self.unreachable:
            raise httpx.ConnectError(f"refused while sending {DOTENV}", request=request)
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
        name = path.removeprefix("/actions/secrets/")
        if self.answer_first_put_with is not None:
            status, self.answer_first_put_with = self.answer_first_put_with, None
            return httpx.Response(status)
        if name in self.refuse:
            return httpx.Response(422, json={"message": "Unprocessable"})
        sealed = base64.b64decode(json.loads(request.content)["encrypted_value"])
        self.secrets[name] = public.SealedBox(self._key).decrypt(sealed).decode()
        return httpx.Response(204)

    def written(self) -> list[str]:
        return [path.rsplit("/", 1)[1] for method, path in self.requests if method == "PUT"]

    def key_reads(self) -> int:
        return self.requests.count(("GET", "/actions/secrets/public-key"))


@pytest.fixture
def server():
    return fakeredis.FakeServer()


@pytest.fixture
async def repository(server):
    repository = Repository(server)
    with (
        patch.dict(os.environ, WRITER_ENV),
        patch("shared.clients.github._base.asyncio.sleep", new_callable=AsyncMock),
    ):
        yield repository
    await repository.http.aclose()


def _client(repository: Repository) -> GitHubAppClient:
    """A separate client per deploy, as each deploy job constructs its own."""
    with patch.dict(os.environ, {"GITHUB_APP_ID": "12345"}):
        github = GitHubAppClient()
    github._http_client = repository.http
    github._token_cache[111] = ("token", datetime.now(UTC) + timedelta(hours=1))
    github.get_installation_id = AsyncMock(return_value=111)
    return github


def _deploy(server: fakeredis.FakeServer, task_id: str) -> DeployFence:
    """A new deploy job of the project, holding its lock."""
    fence = DeployFence.for_job(
        fakeredis.aioredis.FakeRedis(server=server, decode_responses=True), PROJECT_ID, task_id
    )
    fakeredis.FakeRedis(server=server, decode_responses=True).set(fence.lock_key, fence.token)
    return fence


async def _write(repository, values, task_id="deploy-a", diagnostic_secrets=()) -> bool:
    return await write_deploy_secrets(
        _client(repository),
        OWNER,
        REPO,
        values,
        deploy_fence=_deploy(repository.server, task_id),
        diagnostic_secrets=diagnostic_secrets,
    )


def _receipt(repository: Repository) -> str | None:
    return repository.redis.get(receipt_key(PROJECT_ID))


def _registry_receipt(repository: Repository) -> str | None:
    return repository.redis.get(registry_receipt_key(repository.id))


def _counts(logs) -> list[tuple[int, int, int]]:
    configured = [entry for entry in logs if entry["event"] == "deploy_secrets_configured"]
    return [(e["secrets_count"], e["cached_count"], e["total"]) for e in configured]


@pytest.mark.asyncio
async def test_a_redeploy_from_another_job_writes_no_secret_and_reads_no_key(repository):
    with capture_logs() as logs:
        assert await _write(repository, VALUES, "deploy-a")
    assert repository.written() == [*REGISTRY_NAMES, *VALUES]
    assert repository.secrets == {**VALUES, **REGISTRY_SECRETS}
    repository.requests.clear()

    with capture_logs() as logs_b:
        assert await _write(repository, VALUES, "deploy-b")

    assert repository.written() == []
    assert repository.key_reads() == 0
    assert _counts(logs) == [(9, 0, 9)]
    assert _counts(logs_b) == [(0, 9, 9)]


@pytest.mark.asyncio
async def test_the_receipts_hold_no_value(repository):
    assert await _write(repository, VALUES, "deploy-a")
    assert await _write(repository, CHANGED, "deploy-b")

    keys = set(repository.redis.keys("*"))
    assert keys == {
        f"deploy:{PROJECT_ID}:lock",
        receipt_key(PROJECT_ID),
        registry_receipt_key(repository.id),
    }
    assert re.fullmatch(r"[0-9a-f]{64}", _receipt(repository))
    for value in [*VALUES.values(), *CHANGED.values(), *REGISTRY_SECRETS.values()]:
        assert value not in _registry_receipt(repository)


@pytest.mark.asyncio
async def test_a_registry_receipt_warmed_by_another_writer_spares_the_first_deploy(repository):
    warmed = await _client(repository).ensure_registry_secrets(
        OWNER,
        REPO,
        redis=fakeredis.aioredis.FakeRedis(server=repository.server),
        lease_seconds=180,
    )
    assert warmed == RegistrySecretsResult(written=3)
    repository.requests.clear()

    with capture_logs() as logs:
        assert await _write(repository, VALUES, "deploy-a")

    assert repository.written() == list(VALUES)
    assert _counts(logs) == [(6, 3, 9)]


@pytest.mark.asyncio
@pytest.mark.parametrize("variable", list(REGISTRY_ENV))
async def test_a_changed_registry_value_rewrites_only_the_registry_secrets(repository, variable):
    assert await _write(repository, VALUES, "deploy-a")
    repository.requests.clear()

    with patch.dict(os.environ, {variable: "rotated-value"}), capture_logs() as logs:
        assert await _write(repository, VALUES, "deploy-b")

    assert repository.written() == REGISTRY_NAMES
    assert "rotated-value" in repository.secrets.values()
    assert _counts(logs) == [(3, 6, 9)]


@pytest.mark.asyncio
async def test_a_busy_registry_lease_refuses_before_any_deploy_secret(repository):
    assert await _write(repository, VALUES, "deploy-a")
    repository.redis.set(registry_lease_key(repository.id), "scheduler-holder")
    receipt = _receipt(repository)
    repository.requests.clear()

    assert not await _write(repository, CHANGED, "deploy-b")

    assert repository.written() == []
    assert _receipt(repository) == receipt
    assert repository.redis.get(registry_lease_key(repository.id)) == "scheduler-holder"


@pytest.mark.asyncio
async def test_a_missing_registry_variable_refuses_before_any_request(repository):
    with patch.dict(os.environ, {"REGISTRY_PASSWORD": ""}):
        assert not await _write(repository, VALUES, "deploy-a")

    assert repository.requests == []
    assert _receipt(repository) is None


@pytest.mark.asyncio
async def test_a_lock_lost_while_the_registry_secrets_are_written_certifies_neither_group(
    repository,
):
    repository.hand_over_on = ("GET", "/actions/secrets/public-key")

    with pytest.raises(DeployFenceLost):
        await _write(repository, VALUES, "deploy-a")

    assert repository.written() == []
    assert _receipt(repository) is None
    assert _registry_receipt(repository) is None
    assert repository.redis.get(registry_lease_key(repository.id)) is None
    assert repository.redis.get(f"deploy:{PROJECT_ID}:lock") == OTHER_HOLDER


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(VALUES))
async def test_any_changed_value_writes_all_six_again(repository, name):
    assert await _write(repository, VALUES, "deploy-a")
    repository.requests.clear()

    assert await _write(repository, {**VALUES, name: CHANGED[name]}, "deploy-b")

    assert repository.written() == list(VALUES)
    assert repository.secrets[name] == CHANGED[name]


@pytest.mark.asyncio
async def test_the_same_values_in_another_order_are_the_same_payload(repository):
    assert await _write(repository, VALUES, "deploy-a")
    repository.requests.clear()

    assert await _write(repository, dict(reversed(VALUES.items())), "deploy-b")

    assert repository.written() == []


@pytest.mark.asyncio
async def test_a_repository_recreated_under_the_same_name_is_written_again(repository):
    assert await _write(repository, VALUES, "deploy-a")
    repository.id = 2002
    repository.secrets.clear()
    repository.requests.clear()

    assert await _write(repository, VALUES, "deploy-b")

    assert repository.secrets == {**VALUES, **REGISTRY_SECRETS}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "receipt", [None, "corrupt", "0" * 64], ids=["deleted", "corrupt", "other"]
)
async def test_a_missing_or_foreign_receipt_writes_and_repairs(repository, receipt):
    assert await _write(repository, VALUES, "deploy-a")
    repository.redis.delete(receipt_key(PROJECT_ID))
    if receipt is not None:
        repository.redis.set(receipt_key(PROJECT_ID), receipt)
    repository.requests.clear()

    assert await _write(repository, VALUES, "deploy-b")
    assert repository.written() == list(VALUES)
    repository.requests.clear()
    assert await _write(repository, VALUES, "deploy-c")
    assert repository.written() == []


@pytest.mark.asyncio
async def test_a_partial_write_of_b_leaves_no_receipt_and_a_is_repaired(repository):
    assert await _write(repository, VALUES, "deploy-a")
    repository.refuse = {"DEPLOY_SSH_KEY"}

    assert not await _write(repository, CHANGED, "deploy-b")

    assert repository.secrets["DOTENV"] == CHANGED["DOTENV"]
    assert _receipt(repository) is None
    repository.refuse = set()
    repository.requests.clear()
    assert await _write(repository, VALUES, "deploy-c")
    assert repository.written() == list(VALUES)
    assert {name: repository.secrets[name] for name in VALUES} == VALUES


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("values", "request_", "receipt_kept"),
    [
        (CHANGED, ("GET", ""), True),
        (CHANGED, ("GET", "/actions/secrets/public-key"), False),
        (VALUES, ("GET", ""), True),
    ],
    ids=["identity-read", "key-read", "certified-identity-read"],
)
async def test_a_lock_lost_during_a_read_sends_no_secret(
    repository, values, request_, receipt_kept
):
    assert await _write(repository, VALUES, "deploy-a")
    receipt = _receipt(repository)
    repository.requests.clear()
    repository.hand_over_on = request_

    with pytest.raises(DeployFenceLost):
        await _write(repository, values, "deploy-b")

    assert repository.written() == []
    assert _receipt(repository) == (receipt if receipt_kept else None)
    assert repository.redis.get(f"deploy:{PROJECT_ID}:lock") == OTHER_HOLDER


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["REGISTRY_URL", "DOTENV"])
async def test_a_lock_lost_before_a_retry_stops_the_batch(repository, name):
    if name == "DOTENV":
        assert await _write(repository, VALUES, "deploy-a")
        repository.requests.clear()
    repository.answer_first_put_with = 502
    repository.hand_over_on = ("PUT", f"/actions/secrets/{name}")

    with pytest.raises(DeployFenceLost):
        await _write(repository, CHANGED, "deploy-b")

    assert repository.written() == [name]
    assert _receipt(repository) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["REGISTRY_PASSWORD", "PROJECT_NAME"])
async def test_a_lock_lost_after_a_group_is_written_publishes_no_receipt_for_it(repository, name):
    repository.hand_over_on = ("PUT", f"/actions/secrets/{name}")

    with pytest.raises(DeployFenceLost):
        await _write(repository, VALUES, "deploy-a")

    registry_done = name == "PROJECT_NAME"
    assert repository.written() == ([*REGISTRY_NAMES, *VALUES] if registry_done else REGISTRY_NAMES)
    assert _receipt(repository) is None
    assert (_registry_receipt(repository) is not None) is registry_done
    repository.hand_over_on = None
    repository.requests.clear()
    assert await _write(repository, VALUES, "deploy-b")
    assert repository.written() == (list(VALUES) if registry_done else [*REGISTRY_NAMES, *VALUES])


@pytest.mark.asyncio
async def test_an_unreadable_receipt_store_is_no_hit_and_no_setup(server, repository):
    assert await _write(repository, VALUES, "deploy-a")
    fence = _deploy(server, "deploy-b")
    repository.requests.clear()
    server.connected = False

    assert not await write_deploy_secrets(
        _client(repository), OWNER, REPO, VALUES, deploy_fence=fence, diagnostic_secrets=[DOTENV]
    )

    assert repository.written() == []


@pytest.mark.asyncio
async def test_a_failed_identity_read_writes_nothing_and_logs_no_value(repository):
    repository.unreachable = True

    with capture_logs() as logs:
        assert not await _write(repository, VALUES, "deploy-a", diagnostic_secrets=[DOTENV])

    assert repository.written() == []
    assert _receipt(repository) is None
    assert [entry["event"] for entry in logs] == ["deploy_secrets_setup_failed"]
    assert DOTENV not in repr(logs)
