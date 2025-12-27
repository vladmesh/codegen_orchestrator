"""Unit tests for Telegram bot API client."""

from unittest.mock import patch

import pytest

from src.clients.api import TelegramAPIClient


def test_telegram_api_client_base_url():
    """Client should use API_BASE_URL without /api."""
    with patch("src.clients.api.get_settings") as mock_settings:
        mock_settings.return_value.api_base_url = "http://api:8000"
        client = TelegramAPIClient()
        assert client.base_url == "http://api:8000"


def test_telegram_api_client_path_joining():
    """Client should prefix /api and reject manual /api paths."""
    with patch("src.clients.api.get_settings") as mock_settings:
        mock_settings.return_value.api_base_url = "http://api:8000"
        client = TelegramAPIClient()

        assert client._api_path("projects") == "/api/projects"
        with pytest.raises(ValueError):
            client._api_path("/api/projects")
