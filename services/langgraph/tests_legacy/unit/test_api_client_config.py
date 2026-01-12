"""Unit tests for API client configuration and path handling.

Regression tests for commit 2fd04e2 - API path mismatch bugs.
"""

import pytest


def test_langgraph_api_client_rejects_api_suffix():
    """Test that LanggraphAPIClient rejects base_url with /api suffix.

    Regression test: base_url should never include /api,
    the client adds it internally via _api_path().
    """
    from unittest.mock import patch

    from src.clients.api import LanggraphAPIClient

    # Mock settings with incorrect base_url (includes /api)
    with patch("src.clients.api.get_settings") as mock_settings:
        mock_settings.return_value.api_base_url = "http://api:8000/api"

        # Should raise error on initialization
        with pytest.raises(RuntimeError, match="must not include /api"):
            LanggraphAPIClient()


def test_langgraph_api_client_correct_base_url():
    """Test that client accepts correct base_url without /api."""
    from unittest.mock import patch

    from src.clients.api import LanggraphAPIClient

    with patch("src.clients.api.get_settings") as mock_settings:
        mock_settings.return_value.api_base_url = "http://api:8000"

        client = LanggraphAPIClient()

        # Should strip trailing slash and store correctly
        assert client.base_url == "http://api:8000"


def test_langgraph_api_client_path_construction():
    """Test that API client correctly constructs paths with /api prefix."""
    from unittest.mock import patch

    from src.clients.api import LanggraphAPIClient

    with patch("src.clients.api.get_settings") as mock_settings:
        mock_settings.return_value.api_base_url = "http://api:8000"

        client = LanggraphAPIClient()

        # Test _api_path adds /api prefix
        assert client._api_path("projects/") == "/api/projects/"
        assert client._api_path("/projects/") == "/api/projects/"
        assert client._api_path("agent-configs/test") == "/api/agent-configs/test"


def test_langgraph_api_client_rejects_double_api_prefix():
    """Test that client rejects paths that already contain /api prefix.

    This prevents double /api/api in URLs which caused 404 errors.
    """
    from unittest.mock import patch

    from src.clients.api import LanggraphAPIClient

    with patch("src.clients.api.get_settings") as mock_settings:
        mock_settings.return_value.api_base_url = "http://api:8000"

        client = LanggraphAPIClient()

        # Should raise error for paths starting with api/
        with pytest.raises(ValueError, match="should not include /api prefix"):
            client._api_path("/api/projects/")

        with pytest.raises(ValueError, match="should not include /api prefix"):
            client._api_path("api/projects/")
