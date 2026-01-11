from datetime import UTC, datetime, timedelta
import os
import time
from unittest.mock import patch

import httpx
import pytest
import respx

from shared.clients.github import GitHubAppClient


@pytest.fixture
def mock_env():
    with patch.dict(
        os.environ, {"GITHUB_APP_ID": "12345", "GITHUB_APP_PRIVATE_KEY_PATH": "dummy.pem"}
    ):
        yield


@pytest.fixture
def client(mock_env):
    client = GitHubAppClient()
    client._private_key = "dummy_private_key"  # Bypass file loading
    return client


@pytest.fixture
def mock_jwt():
    with patch(
        "shared.clients.github.GitHubAppClient._generate_jwt", return_value="mock_jwt_token"
    ):
        yield


@pytest.mark.asyncio
async def test_get_installation_token_success(client, mock_jwt):
    installation_id = 999

    async with respx.mock(base_url="https://api.github.com") as respx_mock:
        respx_mock.post(f"/app/installations/{installation_id}/access_tokens").mock(
            return_value=httpx.Response(
                httpx.codes.CREATED,
                json={
                    "token": "ghs_new_token",
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                },
            )
        )

        token = await client._get_installation_token(installation_id)
        assert token == "ghs_new_token"  # noqa: S105
        assert installation_id in client._token_cache
        assert client._token_cache[installation_id][0] == "ghs_new_token"  # noqa: S105


@pytest.mark.asyncio
async def test_get_installation_token_cached(client, mock_jwt):
    installation_id = 888
    # Pre-populate cache with a valid token
    expires_at = datetime.now(UTC) + timedelta(minutes=50)
    client._token_cache[installation_id] = ("cached_token", expires_at)

    async with respx.mock(base_url="https://api.github.com", assert_all_called=False) as respx_mock:
        # Should NOT make a request
        token_endpoint = respx_mock.post(f"/app/installations/{installation_id}/access_tokens")

        token = await client._get_installation_token(installation_id)

        assert token == "cached_token"  # noqa: S105
        assert not token_endpoint.called


@pytest.mark.asyncio
async def test_get_installation_token_expired(client, mock_jwt):
    installation_id = 777
    # Pre-populate cache with an expired token (less than 60s buffer)
    expires_at = datetime.now(UTC) + timedelta(seconds=30)
    client._token_cache[installation_id] = ("expired_token", expires_at)

    async with respx.mock(base_url="https://api.github.com") as respx_mock:
        respx_mock.post(f"/app/installations/{installation_id}/access_tokens").mock(
            return_value=httpx.Response(
                httpx.codes.CREATED,
                json={
                    "token": "new_refreshed_token",
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                },
            )
        )

        token = await client._get_installation_token(installation_id)

        assert token == "new_refreshed_token"  # noqa: S105
        assert client._token_cache[installation_id][0] == "new_refreshed_token"  # noqa: S105


@pytest.mark.asyncio
async def test_rate_limiting_handling(client):
    # Mocking rate limit hit then success

    async with respx.mock(base_url="https://api.github.com") as respx_mock:
        # 1st request: 403 Rate Limit
        route = respx_mock.get("/rate_limit_test")
        route.side_effect = [
            httpx.Response(
                httpx.codes.FORBIDDEN,
                headers={
                    "x-ratelimit-remaining": "0",
                    "x-ratelimit-reset": str(int(time.time()) + 1),
                },
            ),
            httpx.Response(httpx.codes.OK, json={"ok": True}),
        ]

        # We need to mock asyncio.sleep to speed up test
        with patch("asyncio.sleep", return_value=None) as mock_sleep:
            resp = await client._make_request(
                "GET", "https://api.github.com/rate_limit_test", headers={}
            )

            assert resp.status_code == httpx.codes.OK
            assert resp.json() == {"ok": True}
            # Verify sleep was called
            assert mock_sleep.called


@pytest.mark.asyncio
async def test_get_file_contents_404(client, mock_jwt):
    owner, repo, path = "foo", "bar", "baz.txt"
    # Mock token retrieval
    client._token_cache[111] = ("token", datetime.now(UTC) + timedelta(hours=1))
    with patch.object(client, "get_installation_id", return_value=111):
        async with respx.mock(base_url="https://api.github.com") as respx_mock:
            respx_mock.get(f"/repos/{owner}/{repo}/contents/{path}").mock(
                return_value=httpx.Response(httpx.codes.NOT_FOUND)
            )

            content = await client.get_file_contents(owner, repo, path)
            assert content is None
