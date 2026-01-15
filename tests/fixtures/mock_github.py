from unittest.mock import patch

import pytest

from shared.tests.mocks.github import MockGitHubClient


@pytest.fixture
def mock_github():
    """Replace GitHubAppClient with mock."""
    mock = MockGitHubClient()

    with patch("shared.clients.github.GitHubAppClient", return_value=mock):
        yield mock
