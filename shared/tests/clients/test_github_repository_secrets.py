"""Repository secret writes under a caller's write guard."""

from base64 import b64encode
from datetime import UTC, datetime, timedelta
import os
from unittest.mock import AsyncMock, patch

import httpx
from nacl import public
import pytest
from structlog.testing import capture_logs

from shared.clients.github import GitHubAppClient

OWNER, REPO = "my-org", "product"
SECRETS = {"FIRST": "first-value", "SECOND": "second-value", "THIRD": "third-value"}


class Refused(Exception):
    pass


@pytest.fixture
async def github():
    key = public.PrivateKey.generate().public_key
    puts: list[str] = []
    answers = {"FIRST": [502, 204], "SECOND": [204], "THIRD": [204]}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/public-key"):
            return httpx.Response(200, json={"key": b64encode(bytes(key)).decode(), "key_id": "k1"})
        name = request.url.path.rsplit("/", 1)[1]
        puts.append(name)
        return httpx.Response(answers[name].pop(0))

    with patch.dict(os.environ, {"GITHUB_APP_ID": "12345"}):
        client = GitHubAppClient()
    client._token_cache[111] = ("token", datetime.now(UTC) + timedelta(hours=1))
    client.get_installation_id = AsyncMock(return_value=111)
    client._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    with patch("shared.clients.github._base.asyncio.sleep", new_callable=AsyncMock):
        yield client, puts
    await client._http_client.aclose()


@pytest.mark.asyncio
async def test_the_guard_precedes_every_put_attempt_including_retries(github):
    github, puts = github
    checks: list[int] = []

    async def guard() -> None:
        checks.append(len(puts))

    assert await github.set_repository_secrets(OWNER, REPO, SECRETS, guard=guard) == 3

    assert puts == ["FIRST", "FIRST", "SECOND", "THIRD"]
    assert checks == [0, 1, 2, 3]


@pytest.mark.asyncio
async def test_a_guard_refusal_stops_the_batch_and_is_raised_unchanged(github):
    github, puts = github

    async def guard() -> None:
        if puts:
            raise Refused("lock lost")

    with capture_logs() as logs, pytest.raises(Refused):
        await github.set_repository_secrets(OWNER, REPO, SECRETS, guard=guard)

    assert puts == ["FIRST"]
    assert not [entry for entry in logs if entry["event"] == "github_secret_set_failed"]
