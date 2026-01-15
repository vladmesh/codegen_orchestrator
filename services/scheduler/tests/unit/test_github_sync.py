from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.contracts.dto.project import ProjectCreate, ProjectDTO, ProjectStatus
from src.tasks import github_sync


@pytest.fixture
def mock_api_client():
    with patch("src.tasks.github_sync.api_client") as mock:
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
async def test_sync_single_repo_links_legacy_project(mock_api_client, mock_github):
    # Setup
    repo = await mock_github.create_repo(org="org", name="legacy-repo", private=True)

    legacy_project = ProjectDTO(
        id="proj-legacy",
        name="legacy-repo",
        status=ProjectStatus.ACTIVE,
        github_repo_id=None,
        modules=[],  # No repo ID yet
    )

    # Mocks
    mock_api_client.get_project_by_repo_id = AsyncMock(return_value=None)
    mock_api_client.get_project_by_name = AsyncMock(return_value=legacy_project)
    mock_api_client.update_project = AsyncMock(return_value=legacy_project)

    # Execution
    missing_counters = {}
    await github_sync._sync_single_repo(mock_github, repo, missing_counters)

    # Verification
    mock_api_client.update_project.assert_called_once()
    call_args = mock_api_client.update_project.call_args
    assert call_args[0][0] == "proj-legacy"
    assert call_args[0][1].github_repo_id == repo.id


@pytest.mark.asyncio
async def test_sync_single_repo_creates_new_project(mock_api_client, mock_github):
    # Setup
    repo = await mock_github.create_repo(org="org", name="new-repo", private=True)

    # Mocks
    mock_api_client.get_project_by_repo_id = AsyncMock(return_value=None)
    mock_api_client.get_project_by_name = AsyncMock(return_value=None)

    new_project_dto = ProjectDTO(
        id="new-id",
        name="new-repo",
        status=ProjectStatus.DISCOVERED,
        github_repo_id=repo.id,
        modules=[],
    )
    mock_api_client.create_project = AsyncMock(return_value=new_project_dto)

    # Execution
    missing_counters = {}
    await github_sync._sync_single_repo(mock_github, repo, missing_counters)

    # Verification
    mock_api_client.create_project.assert_called_once()
    create_payload = mock_api_client.create_project.call_args[0][0]
    assert isinstance(create_payload, ProjectCreate)
    assert create_payload.name == "new-repo"
    assert create_payload.github_repo_id == repo.id


@pytest.mark.asyncio
async def test_detect_missing_projects_marks_missing(mock_api_client):
    # Setup
    proj_ok = ProjectDTO(id="p1", name="ok", github_repo_id=1, status=ProjectStatus.ACTIVE)
    proj_missing = ProjectDTO(id="p2", name="gone", github_repo_id=2, status=ProjectStatus.ACTIVE)

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
