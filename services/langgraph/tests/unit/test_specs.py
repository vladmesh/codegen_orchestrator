from unittest.mock import AsyncMock, patch

import pytest

from src.tools.github import create_file_in_repo
from src.tools.specs import get_project_spec, update_project_spec


@pytest.fixture
def mock_github_client():
    with patch("src.tools.specs.get_github_client") as mock:
        client = AsyncMock()
        mock.return_value = client
        yield client


@pytest.fixture
def mock_github_client_tools():
    with patch("src.tools.github.get_github_client") as mock:
        client = AsyncMock()
        mock.return_value = client
        yield client


@pytest.mark.asyncio
async def test_get_project_spec_success(mock_github_client):
    mock_github_client.get_file_contents.return_value = "# Spec Content"

    result = await get_project_spec.ainvoke({"repo_full_name": "org/repo"})

    assert result == "# Spec Content"
    mock_github_client.get_file_contents.assert_called_with("org", "repo", "SPEC.md")


@pytest.mark.asyncio
async def test_get_project_spec_not_found(mock_github_client):
    mock_github_client.get_file_contents.return_value = None

    result = await get_project_spec.ainvoke({"repo_full_name": "org/repo"})

    assert "not found" in result


@pytest.mark.asyncio
async def test_update_project_spec_success(mock_github_client):
    mock_github_client.create_or_update_file.return_value = {"sha": "new_sha"}

    result = await update_project_spec.ainvoke(
        {"repo_full_name": "org/repo", "content": "# New Spec", "update_description": "updates"}
    )

    assert "successfully" in result
    mock_github_client.create_or_update_file.assert_called_with(
        owner="org",
        repo="repo",
        path="SPEC.md",
        content="# New Spec",
        message="Update SPEC.md: updates",
        branch="main",
    )


@pytest.mark.asyncio
async def test_create_file_in_repo_success(mock_github_client_tools):
    mock_github_client_tools.create_or_update_file.return_value = {"sha": "sha123"}

    result = await create_file_in_repo.ainvoke(
        {
            "repo_full_name": "org/repo",
            "path": "docs/README.md",
            "content": "content",
            "message": "init",
            "branch": "main",
        }
    )

    assert result == "sha123"
    mock_github_client_tools.create_or_update_file.assert_called_with(
        owner="org",
        repo="repo",
        path="docs/README.md",
        content="content",
        message="init",
        branch="main",
    )
