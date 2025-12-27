"""Unit tests for Scheduler API client."""

from unittest.mock import patch

import pytest

from src.clients.api import SchedulerAPIClient


def test_scheduler_api_client_base_url():
    """Client should use API_BASE_URL without /api."""
    with patch("src.clients.api.get_settings") as mock_settings:
        mock_settings.return_value.api_base_url = "http://api:8000"
        client = SchedulerAPIClient()
        assert client.base_url == "http://api:8000"


def test_scheduler_api_client_path_joining():
    """Client should prefix /api and reject manual /api paths."""
    with patch("src.clients.api.get_settings") as mock_settings:
        mock_settings.return_value.api_base_url = "http://api:8000"
        client = SchedulerAPIClient()

        assert client._api_path("rag/ingest") == "/api/rag/ingest"
        with pytest.raises(ValueError):
            client._api_path("/api/rag/ingest")
