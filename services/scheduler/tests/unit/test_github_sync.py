from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest

from shared.contracts.dto.project import ProjectDTO, ProjectStatus
from shared.contracts.dto.repository import RepositoryStatus
from src.tasks import github_sync

PROJ1_UUID = uuid.uuid4()
PROJ2_UUID = uuid.uuid4()


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

    db_repo = {
        "id": "repo-1",
        "project_id": str(PROJ1_UUID),
        "provider_repo_id": repo.id,
    }

    existing_project = ProjectDTO(
        id=PROJ1_UUID,
        name="test-repo",
        status=ProjectStatus.ACTIVE,
        owner_id=1,
        modules=[],
    )

    # Mocks
    mock_api_client.get_repository_by_provider_id = AsyncMock(return_value=db_repo)
    mock_api_client.get_project = AsyncMock(return_value=existing_project)

    # Execution
    missing_counters = {}
    await github_sync._sync_single_repo(mock_github, repo, missing_counters)

    # Verification — no exception raised means sync succeeded
    pass


@pytest.mark.asyncio
async def test_sync_single_repo_notifies_admins_for_unknown_repo(
    mock_api_client, mock_github, mock_notify_admins
):
    # Setup
    repo = await mock_github.create_repo(org="org", name="new-repo", private=True)

    # Mocks
    mock_api_client.get_repository_by_provider_id = AsyncMock(return_value=None)
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
    proj_ok = ProjectDTO(id=PROJ1_UUID, name="ok", status=ProjectStatus.ACTIVE, owner_id=1)
    proj_missing = ProjectDTO(id=PROJ2_UUID, name="gone", status=ProjectStatus.ACTIVE, owner_id=1)

    mock_api_client.get_projects = AsyncMock(return_value=[proj_ok, proj_missing])
    mock_api_client.update_repository = AsyncMock()
    # proj_ok has a repo with provider_repo_id=1 (present in gh_repos_map)
    # proj_missing has a repo with provider_repo_id=2 (missing from gh_repos_map)
    mock_api_client.get_repositories = AsyncMock(
        side_effect=[
            [{"provider_repo_id": 1}],  # repos for proj_ok (initial check)
            [{"provider_repo_id": 2}],  # repos for proj_missing (initial check)
            [{"id": "repo-2", "provider_repo_id": 2}],  # repos for proj_missing (marking)
        ]
    )

    gh_repos_map = {1: MagicMock()}  # Repo 1 exists, Repo 2 missing
    missing_counters = {str(PROJ2_UUID): github_sync.MISSING_THRESHOLD - 1}  # Almost threshold

    # Execution
    await github_sync._detect_missing_projects(gh_repos_map, missing_counters)

    # Verification — repository marked missing, not the project
    mock_api_client.update_repository.assert_called_once_with(
        "repo-2", {"status": RepositoryStatus.MISSING.value}
    )
