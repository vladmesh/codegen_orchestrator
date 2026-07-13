from unittest.mock import AsyncMock, patch

import pytest

from src.tasks import github_sync


@pytest.mark.asyncio
async def test_github_sync_notifies_admins_for_unknown_repo(mock_github, api_client):
    """
    Integration Test: GitHub Sync — unknown repo triggers admin notification.

    1. Seed Mock GitHub with a repository not tracked in DB.
    2. Run the sync task.
    3. Verify no project is created and notify_admins was called.
    """
    org_name = "test-org"
    repo_name = "integration-demo"

    repo = await mock_github.create_repo(org=org_name, name=repo_name, private=True)

    with patch(
        "src.tasks.github_sync.notify_admins_best_effort", new_callable=AsyncMock
    ) as mock_notify:
        await github_sync._sync_single_repo(mock_github, repo, missing_counters={})

    # Repository should NOT be tracked
    repo_entry = await api_client.get_repository_by_provider_id(repo.id)
    assert repo_entry is None

    # Admin notification should have been sent
    mock_notify.assert_called_once()
    assert repo_name in mock_notify.call_args[0][0]
