"""Unit tests for InternalAPIClient.

Tests that the API client correctly constructs URLs with /api prefix.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest


@pytest.fixture
def mock_settings():
    """Mock settings with API URL."""
    with patch("src.tools.base.get_settings") as mock:
        settings = AsyncMock()
        settings.api_base_url = "http://api:8000"
        mock.return_value = settings
        yield settings


@pytest.mark.asyncio
async def test_api_client_base_url(mock_settings):
    """Test that InternalAPIClient uses correct base URL with /api prefix."""
    from src.tools.base import InternalAPIClient

    client = InternalAPIClient()
    assert client.base_url == "http://api:8000"


@pytest.mark.asyncio
async def test_api_client_get_request(mock_settings):
    """Test GET request constructs correct path."""
    from src.tools.base import InternalAPIClient

    client = InternalAPIClient()

    # Mock httpx.AsyncClient
    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_response = AsyncMock()
        mock_response.json.return_value = {"id": 1, "name": "test"}
        mock_client.request.return_value = mock_response
        mock_client.is_closed = False
        mock_client_class.return_value = mock_client

        result = await client.get("/projects/")

        # Verify client was created with correct base_url
        mock_client_class.assert_called_once()
        call_kwargs = mock_client_class.call_args.kwargs
        assert call_kwargs["base_url"] == "http://api:8000"

        # Verify GET was called with correct path
        mock_client.request.assert_called_once_with("GET", "/api/projects/", headers=None)
        assert result == {"id": 1, "name": "test"}


@pytest.mark.asyncio
async def test_api_client_post_request(mock_settings):
    """Test POST request constructs correct path."""
    from src.tools.base import InternalAPIClient

    client = InternalAPIClient()

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_response = AsyncMock()
        mock_response.json.return_value = {"id": 2, "status": "created"}
        mock_client.request.return_value = mock_response
        mock_client.is_closed = False
        mock_client_class.return_value = mock_client

        result = await client.post("/projects/", json={"name": "test"})

        mock_client.request.assert_called_once_with(
            "POST", "/api/projects/", headers=None, json={"name": "test"}
        )
        assert result == {"id": 2, "status": "created"}


@pytest.mark.asyncio
async def test_api_client_handles_errors(mock_settings):
    """Test that API client properly raises HTTP errors."""
    from src.tools.base import InternalAPIClient

    client = InternalAPIClient()

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_response = AsyncMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "404 Not Found", request=AsyncMock(), response=AsyncMock(status_code=404)
        )
        mock_client.request.return_value = mock_response
        mock_client.is_closed = False
        mock_client_class.return_value = mock_client

        with pytest.raises(httpx.HTTPStatusError):
            await client.get("/nonexistent/")


def test_api_client_rejects_api_prefix(mock_settings):
    """Test that paths cannot include /api prefix."""
    from src.tools.base import InternalAPIClient

    client = InternalAPIClient()

    with pytest.raises(ValueError):
        client._api_path("/api/projects/")
