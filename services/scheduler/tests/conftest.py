"""Pytest configuration for scheduler tests."""

from pathlib import Path
import sys
from unittest.mock import patch

import pytest

# Add scheduler src to path for imports
src_path = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(src_path))

# Add project root for shared imports
project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))

from shared.tests.mocks.github import MockGitHubClient  # noqa: E402


@pytest.fixture
def mock_github():
    """Replace GitHubAppClient with mock for Scheduler tests."""
    mock = MockGitHubClient()

    with patch("shared.clients.github.GitHubAppClient", return_value=mock):
        yield mock
