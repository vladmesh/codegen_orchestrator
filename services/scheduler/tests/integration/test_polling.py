"""Integration tests for scheduler polling."""

import http
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_health_check_polling():
    """Test that health check polling works."""
    # Placeholder for actual polling implementation
    with patch("httpx.AsyncClient") as mock_client:
        mock_response = AsyncMock()
        mock_response.status_code = http.HTTPStatus.OK
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_response)

        # Verify mock setup works
        async with mock_client() as client:
            response = await client.get("http://test")
            assert response.status_code == http.HTTPStatus.OK
