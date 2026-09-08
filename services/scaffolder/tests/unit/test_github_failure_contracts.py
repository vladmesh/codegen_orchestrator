import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.consumer import _process_ensure_mode, _process_full_mode
from src.scaffold import ScaffoldResult


def _http_error(status_code: int, method: str = "GET") -> httpx.HTTPStatusError:
    request = httpx.Request(method, "https://api.github.com/example")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(
        f"GitHub returned {status_code}",
        request=request,
        response=response,
    )


def _message():
    return SimpleNamespace(
        project_id="project-1",
        repository_id="repo-1",
        project_name="demo",
        template_repo="gh:vladmesh/codegen-product-kit",
        template_ref="0.6.1",
        modules="backend",
        task_description="demo",
    )


@pytest.mark.asyncio
async def test_full_mode_does_not_parse_422_from_exception_text():
    github = AsyncMock()
    github.create_repo.side_effect = RuntimeError("transport failed with text 422")

    with pytest.raises(RuntimeError, match="transport failed"):
        await _process_full_mode(
            _message(),
            "org/demo",
            github,
            "token",
            AsyncMock(),
            MagicMock(),
            MagicMock(),
        )

    github.get_repo.assert_not_awaited()


@pytest.mark.asyncio
async def test_full_mode_verifies_existing_repo_after_http_422():
    github = AsyncMock()
    github.create_repo.side_effect = _http_error(
        httpx.codes.UNPROCESSABLE_ENTITY, method="POST"
    )
    github.get_repo.return_value = SimpleNamespace(id=123)
    api = AsyncMock()
    api.get_project.return_value = SimpleNamespace(config={})
    api.get_stories_by_project.return_value = []

    with (
        patch.dict(os.environ, {}, clear=True),
        patch(
            "src.consumer.run_scaffold",
            new=AsyncMock(return_value=ScaffoldResult(success=False, error="stop after create")),
        ),
    ):
        result = await _process_full_mode(
            _message(),
            "org/demo",
            github,
            "token",
            api,
            MagicMock(),
            MagicMock(),
        )

    assert result["status"] == "failed"
    github.get_repo.assert_awaited_once_with("org", "demo")
    api.update_repository.assert_awaited_once_with(
        "repo-1",
        git_url="https://github.com/org/demo",
        provider_repo_id=123,
    )


@pytest.mark.asyncio
async def test_full_mode_keeps_unconfirmed_http_422():
    github = AsyncMock()
    create_error = _http_error(httpx.codes.UNPROCESSABLE_ENTITY, method="POST")
    github.create_repo.side_effect = create_error
    github.get_repo.side_effect = _http_error(httpx.codes.NOT_FOUND)

    with pytest.raises(httpx.HTTPStatusError) as raised:
        await _process_full_mode(
            _message(),
            "org/demo",
            github,
            "token",
            AsyncMock(),
            MagicMock(),
            MagicMock(),
        )

    assert raised.value is create_error


@pytest.mark.asyncio
async def test_ensure_mode_treats_only_404_as_repo_absence():
    github = AsyncMock()
    github.get_repo.side_effect = _http_error(httpx.codes.NOT_FOUND)
    ensure = AsyncMock(return_value=ScaffoldResult(success=True, skipped=True))

    with patch("src.consumer.run_ensure_workspace", new=ensure):
        result = await _process_ensure_mode(
            _message(),
            "org/demo",
            github,
            "token",
            AsyncMock(),
            MagicMock(),
            MagicMock(),
        )

    assert result == {"status": "skipped"}
    assert ensure.await_args.kwargs["repo_exists_on_github"] is False


@pytest.mark.asyncio
async def test_ensure_mode_does_not_parse_404_from_exception_text():
    github = AsyncMock()
    github.get_repo.side_effect = RuntimeError("transport failed with text 404")

    with pytest.raises(RuntimeError, match="transport failed"):
        await _process_ensure_mode(
            _message(),
            "org/demo",
            github,
            "token",
            AsyncMock(),
            MagicMock(),
            MagicMock(),
        )
