from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.consumers._repo_setup import _create_repo_and_set_secrets


def _http_error(status_code: int, method: str = "GET") -> httpx.HTTPStatusError:
    request = httpx.Request(method, "https://api.github.com/example")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(
        f"GitHub returned {status_code}",
        request=request,
        response=response,
    )


def _project():
    return SimpleNamespace(id="project-1", title="Demo", slug="demo")


@pytest.mark.asyncio
@patch("shared.clients.github.GitHubAppClient")
@patch.dict("os.environ", {"GITHUB_ORG": "org"})
async def test_http_422_is_stale_repo_only_when_lookup_confirms_it(mock_client_cls):
    github = AsyncMock()
    mock_client_cls.return_value = github
    github.create_repo.side_effect = _http_error(httpx.codes.UNPROCESSABLE_ENTITY, method="POST")
    github.get_repo.return_value = SimpleNamespace(id=123)

    with pytest.raises(RuntimeError, match="already exists"):
        await _create_repo_and_set_secrets(_project())

    github.get_repo.assert_awaited_once_with("org", "demo")


@pytest.mark.asyncio
@patch("shared.clients.github.GitHubAppClient")
@patch.dict("os.environ", {"GITHUB_ORG": "org"})
async def test_unconfirmed_http_422_keeps_original_validation_failure(mock_client_cls):
    github = AsyncMock()
    mock_client_cls.return_value = github
    create_error = _http_error(httpx.codes.UNPROCESSABLE_ENTITY, method="POST")
    github.create_repo.side_effect = create_error
    github.get_repo.side_effect = _http_error(httpx.codes.NOT_FOUND)

    with pytest.raises(httpx.HTTPStatusError) as raised:
        await _create_repo_and_set_secrets(_project())

    assert raised.value is create_error


@pytest.mark.asyncio
@patch("shared.clients.github.GitHubAppClient")
@patch.dict("os.environ", {"GITHUB_ORG": "org"})
async def test_transport_failure_keeps_httpx_type(mock_client_cls):
    github = AsyncMock()
    mock_client_cls.return_value = github
    request = httpx.Request("POST", "https://api.github.com/orgs/org/repos")
    github.create_repo.side_effect = httpx.ConnectError("offline", request=request)

    with pytest.raises(httpx.ConnectError, match="offline"):
        await _create_repo_and_set_secrets(_project())


@pytest.mark.asyncio
@patch("shared.clients.github.GitHubAppClient")
@patch.dict("os.environ", {"GITHUB_ORG": "org"})
async def test_exception_text_is_not_used_to_classify_stale_repo(mock_client_cls):
    github = AsyncMock()
    mock_client_cls.return_value = github
    github.create_repo.side_effect = Exception("422: already exists in transport wrapper")

    with pytest.raises(RuntimeError, match="GitHub repository creation failed") as raised:
        await _create_repo_and_set_secrets(_project())

    assert "previous run was not cleaned up" not in str(raised.value)
    github.get_repo.assert_not_awaited()
