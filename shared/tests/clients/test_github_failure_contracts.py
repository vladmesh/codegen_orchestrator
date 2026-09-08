from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from shared.clients.github import GitHubAppClient


def _http_error(status_code: int, method: str = "GET") -> httpx.HTTPStatusError:
    request = httpx.Request(method, "https://api.github.com/example")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(
        f"GitHub returned {status_code}",
        request=request,
        response=response,
    )


@pytest.mark.asyncio
async def test_list_repo_files_returns_empty_only_for_404():
    client = object.__new__(GitHubAppClient)
    client.get_token = AsyncMock(return_value="token")
    client._make_request = AsyncMock(side_effect=_http_error(httpx.codes.NOT_FOUND))

    assert await client.list_repo_files("org", "repo") == []


@pytest.mark.asyncio
async def test_list_repo_files_propagates_unexpected_failure():
    client = object.__new__(GitHubAppClient)
    client.get_token = AsyncMock(return_value="token")
    client._make_request = AsyncMock(side_effect=RuntimeError("transport wrapper failed"))

    with pytest.raises(RuntimeError, match="transport wrapper failed"):
        await client.list_repo_files("org", "repo")


@pytest.mark.asyncio
async def test_create_or_update_file_does_not_create_after_failed_sha_lookup():
    client = object.__new__(GitHubAppClient)
    client.get_token = AsyncMock(return_value="token")
    client._make_request = AsyncMock(side_effect=RuntimeError("lookup failed"))

    with pytest.raises(RuntimeError, match="lookup failed"):
        await client.create_or_update_file("org", "repo", "README.md", "body", "update")

    assert client._make_request.await_count == 1
    assert client._make_request.await_args.args[0] == "GET"


@pytest.mark.asyncio
async def test_create_or_update_file_creates_after_explicit_404():
    client = object.__new__(GitHubAppClient)
    client.get_token = AsyncMock(return_value="token")
    created = httpx.Response(
        httpx.codes.OK,
        json={"content": {"sha": "new-sha"}},
        request=httpx.Request("PUT", "https://api.github.com/example"),
    )
    client._make_request = AsyncMock(
        side_effect=[_http_error(httpx.codes.NOT_FOUND), created]
    )

    result = await client.create_or_update_file(
        "org", "repo", "README.md", "body", "create"
    )

    assert result == {"sha": "new-sha"}
    assert client._make_request.await_count == 2
    put_call = client._make_request.await_args_list[1]
    assert put_call.args[0] == "PUT"
    assert "sha" not in put_call.kwargs["json"]


@pytest.mark.asyncio
async def test_provisioning_does_not_parse_422_from_exception_text():
    client = object.__new__(GitHubAppClient)
    client.get_first_org_installation = AsyncMock(return_value={"org": "org"})
    client.create_repo = AsyncMock(side_effect=RuntimeError("transport failed with text 422"))
    client.get_repo = AsyncMock()

    with pytest.raises(RuntimeError, match="transport failed"):
        await client.provision_project_repo("repo")

    client.get_repo.assert_not_awaited()


@pytest.mark.asyncio
async def test_provisioning_uses_existing_repo_only_after_http_422():
    client = object.__new__(GitHubAppClient)
    existing = MagicMock()
    client.get_first_org_installation = AsyncMock(return_value={"org": "org"})
    client.create_repo = AsyncMock(
        side_effect=_http_error(httpx.codes.UNPROCESSABLE_ENTITY, method="POST")
    )
    client.get_repo = AsyncMock(return_value=existing)

    assert await client.provision_project_repo("repo") is existing
    client.get_repo.assert_awaited_once_with("org", "repo")


@pytest.mark.asyncio
async def test_provisioning_keeps_unconfirmed_http_422():
    client = object.__new__(GitHubAppClient)
    create_error = _http_error(httpx.codes.UNPROCESSABLE_ENTITY, method="POST")
    client.get_first_org_installation = AsyncMock(return_value={"org": "org"})
    client.create_repo = AsyncMock(side_effect=create_error)
    client.get_repo = AsyncMock(side_effect=_http_error(httpx.codes.NOT_FOUND))

    with pytest.raises(httpx.HTTPStatusError) as raised:
        await client.provision_project_repo("repo")

    assert raised.value is create_error
