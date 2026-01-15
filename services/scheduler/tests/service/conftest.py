from unittest.mock import patch

import pytest
import respx

from shared.tests.mocks.github import MockGitHubClient


@pytest.fixture
def mock_github():
    """Universal GitHub Mock for service tests."""
    mock = MockGitHubClient()
    # Patcher to replace the real client in src.tasks.github_sync
    with patch("shared.clients.github.GitHubAppClient", return_value=mock):
        yield mock


@pytest.fixture
def time4vps_mock():
    """Respx mock for Time4VPS API."""
    with respx.mock(base_url="https://billing.time4vps.com", assert_all_called=False) as respx_mock:
        # Allow requests to the internal API service to pass through
        respx_mock.route(host="api").pass_through()
        yield respx_mock


@pytest.fixture
async def api_client():
    """Real SchedulerAPIClient configured from env."""
    from src.clients.api import api_client as client

    # Reset internal client to avoid Event Loop Closed errors across tests
    client._client = None
    yield client
    await client.close()
