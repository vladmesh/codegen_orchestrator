"""Unit tests for API interaction in handlers."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.handlers import _api_get, _handle_projects, _handle_servers


@pytest.mark.asyncio
async def test_api_get_constructs_correct_url():
    """Test that _api_get forwards path and headers to the client."""
    with patch("src.handlers.api_client.get_json", new_callable=AsyncMock) as mock_get_json:
        mock_get_json.return_value = {}

        # Case 1: Path starting with / (no telegram_id)
        await _api_get("/projects")
        mock_get_json.assert_called_with("/projects", headers={})

        # Case 2: Path without /
        await _api_get("servers")
        mock_get_json.assert_called_with("servers", headers={})

        # Case 3: With telegram_id
        await _api_get("/projects", telegram_id=12345)
        mock_get_json.assert_called_with("/projects", headers={"X-Telegram-ID": "12345"})


@pytest.mark.asyncio
async def test_handle_projects_calls_correct_endpoint():
    """Test that _handle_projects calls /projects with telegram_id."""
    query = MagicMock()
    query.edit_message_text = AsyncMock()
    query.from_user.id = 12345

    # Mock _api_get
    with patch("src.handlers._api_get", new_callable=AsyncMock) as mock_api_get:
        mock_api_get.return_value = []  # Empty list of projects

        await _handle_projects(query, ["projects", "list"])

        # Verify correct path and telegram_id passed
        mock_api_get.assert_called_once_with("/projects", telegram_id=12345)


@pytest.mark.asyncio
async def test_handle_servers_calls_correct_endpoint():
    """Test that _handle_servers calls /servers with telegram_id."""
    query = MagicMock()
    query.edit_message_text = AsyncMock()
    query.from_user.id = 12345

    # Mock _api_get
    with patch("src.handlers._api_get", new_callable=AsyncMock) as mock_api_get:
        mock_api_get.return_value = []

        await _handle_servers(query, ["servers", "list"], user_is_admin=True)

        # Verify correct path and telegram_id passed
        mock_api_get.assert_called_once_with("/servers?is_managed=true", telegram_id=12345)
