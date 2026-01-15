import pytest

from shared.contracts.dto.project import ProjectStatus
from src.tasks import github_sync


@pytest.mark.asyncio
async def test_github_sync_integration_flow(mock_github, api_client):
    """
    Integration Test: GitHub Sync Flow

    1. Seed Mock GitHub with a new repository.
    2. Run the sync task.
    3. Verify Project is created in the API.
    """
    # 1. Seed Mock Data
    org_name = "test-org"
    repo_name = "integration-demo"

    # Must match GITHUB_ORG_NAME env var set in docker-compose
    repo = await mock_github.create_repo(org=org_name, name=repo_name, private=True)

    # 2. Run Sync Task
    # We invoke _sync_single_repo directly because the worker has an infinite loop
    await github_sync._sync_single_repo(mock_github, repo, missing_counters={})

    # 3. Verify in API
    # We use the internal api_client to fetch state from the Real API
    project = await api_client.get_project_by_name(repo_name)

    assert project is not None
    assert project.name == repo_name
    assert project.github_repo_id is not None
    assert project.status == ProjectStatus.DISCOVERED
