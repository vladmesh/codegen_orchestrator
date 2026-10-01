"""Focused retry and installation-cache contracts for the GitHub App client."""

import time
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from shared.clients.github._base import GitHubAppClientBase


@pytest.mark.asyncio
async def test_permanent_403_is_not_retried(monkeypatch):
    client = GitHubAppClientBase()
    request = httpx.Request("GET", "https://api.github.test/resource")
    response = httpx.Response(403, request=request)
    transport = AsyncMock(return_value=response)
    http = MagicMock(request=transport)
    sleep = AsyncMock()
    monkeypatch.setattr("shared.clients.github._base.asyncio.sleep", sleep)

    with pytest.raises(httpx.HTTPStatusError):
        await client._request_with_client(http, "GET", str(request.url), {})

    transport.assert_awaited_once()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_final_rate_limit_raises_http_error(monkeypatch):
    client = GitHubAppClientBase()
    request = httpx.Request("GET", "https://api.github.test/resource")
    headers = {
        "x-ratelimit-remaining": "0",
        "x-ratelimit-reset": str(int(time.time())),
    }
    transport = AsyncMock(
        side_effect=[
            httpx.Response(403, request=request, headers=headers),
            httpx.Response(403, request=request, headers=headers),
            httpx.Response(403, request=request, headers=headers),
        ]
    )
    http = MagicMock(request=transport)
    monkeypatch.setattr("shared.clients.github._base.asyncio.sleep", AsyncMock())

    with pytest.raises(httpx.HTTPStatusError):
        await client._request_with_client(http, "GET", str(request.url), {})

    assert transport.await_count == 3


@pytest.mark.asyncio
async def test_repo_installation_lookup_is_cached(monkeypatch):
    client = GitHubAppClientBase()
    monkeypatch.setattr(client, "_generate_jwt", lambda: "jwt")
    response = MagicMock()
    response.json.return_value = {"id": 42}
    request = AsyncMock(return_value=response)
    monkeypatch.setattr(client, "_make_request", request)

    assert await client.get_installation_id("org", "repo") == 42
    assert await client.get_installation_id("org", "repo") == 42
    request.assert_awaited_once()
