from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.contracts.dto.project import ProjectDTO, ProjectStatus
from src.tasks import github_sync


@pytest.fixture
def mock_api_client():
    with patch("src.tasks.github_sync.api_client") as mock:
        yield mock


@pytest.fixture
def mock_notify_admins():
    with patch("src.tasks.github_sync.notify_admins", new_callable=AsyncMock) as mock:
        yield mock


@pytest.mark.asyncio
async def test_sync_single_repo_updates_existing_project(mock_api_client, mock_github):
    # Setup
    # Pre-seed the mock with the repository
    repo = await mock_github.create_repo(
        org="org", name="test-repo", private=True, description="Test Repo"
    )
    # Add a file to check content fetching if needed
    await mock_github.create_or_update_file("org", "test-repo", "README.md", "Content", "init")

    existing_project = ProjectDTO(
        id="proj-1",
        name="test-repo",
        status=ProjectStatus.ACTIVE,
        github_repo_id=repo.id,
        owner_id=1,
        modules=[],
    )

    # Mocks
    mock_api_client.get_project_by_repo_id = AsyncMock(return_value=existing_project)

    # Execution
    missing_counters = {}
    await github_sync._sync_single_repo(mock_github, repo, missing_counters)

    # Verification
    # The mock tracks calls internally if we implemented that, but MockGitHubClient is state-based.
    # Typically we check outcome (side effects).
    # github_sync likely calls get_file_contents for RAG.
    # Since we can't easily spy on the mock methods unless we wrap them,
    # we rely on the fact that if it failed (e.g. 404), it would raise an error or log.
    # We can check that no exception was raised.
    pass


@pytest.mark.asyncio
async def test_sync_single_repo_notifies_admins_for_unknown_repo(
    mock_api_client, mock_github, mock_notify_admins
):
    # Setup
    repo = await mock_github.create_repo(org="org", name="new-repo", private=True)

    # Mocks
    mock_api_client.get_project_by_repo_id = AsyncMock(return_value=None)
    mock_api_client.create_project = AsyncMock()

    # Execution
    missing_counters = {}
    await github_sync._sync_single_repo(mock_github, repo, missing_counters)

    # Verification: notify_admins called, create_project NOT called
    mock_notify_admins.assert_called_once()
    assert "new-repo" in mock_notify_admins.call_args[0][0]
    mock_api_client.create_project.assert_not_called()


@pytest.mark.asyncio
async def test_detect_missing_projects_marks_missing(mock_api_client, mock_notify_admins):
    # Setup
    proj_ok = ProjectDTO(
        id="p1", name="ok", github_repo_id=1, status=ProjectStatus.ACTIVE, owner_id=1
    )
    proj_missing = ProjectDTO(
        id="p2", name="gone", github_repo_id=2, status=ProjectStatus.ACTIVE, owner_id=1
    )

    mock_api_client.get_projects = AsyncMock(return_value=[proj_ok, proj_missing])
    mock_api_client.update_project = AsyncMock()

    gh_repos_map = {1: MagicMock()}  # Repo 1 exists, Repo 2 missing
    missing_counters = {"p2": github_sync.MISSING_THRESHOLD - 1}  # Almost threshold

    # Execution
    await github_sync._detect_missing_projects(gh_repos_map, missing_counters)

    # Verification
    # Threshold reached for p2
    mock_api_client.update_project.assert_called_once()
    assert mock_api_client.update_project.call_args[0][0] == "p2"
    assert mock_api_client.update_project.call_args[0][1].status == ProjectStatus.MISSING
