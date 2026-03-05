"""Unit tests for LanggraphAPIClient telegram_id header support."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


@pytest.fixture
def mock_httpx_client():
    client = AsyncMock(spec=httpx.AsyncClient)
    client.is_closed = False
    return client


@pytest.fixture
def api_client(mock_httpx_client):
    with patch("src.clients.api.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(api_base_url="http://api:8000")
        from src.clients.api import LanggraphAPIClient

        c = LanggraphAPIClient()
        c._client = mock_httpx_client
        return c


class TestGetProjectWithTelegramId:
    @pytest.mark.asyncio
    async def test_passes_telegram_id_header(self, api_client, mock_httpx_client):
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.return_value = {"id": "proj-1", "name": "test"}
        mock_httpx_client.request.return_value = resp

        result = await api_client.get_project("proj-1", telegram_id=12345)

        assert result == {"id": "proj-1", "name": "test"}
        call_kwargs = mock_httpx_client.request.call_args
        assert call_kwargs[1].get("headers", {}).get("X-Telegram-ID") == "12345"

    @pytest.mark.asyncio
    async def test_no_header_when_no_telegram_id(self, api_client, mock_httpx_client):
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.return_value = {"id": "proj-1"}
        mock_httpx_client.request.return_value = resp

        await api_client.get_project("proj-1")

        call_kwargs = mock_httpx_client.request.call_args
        headers = call_kwargs[1].get("headers") or {}
        assert "X-Telegram-ID" not in headers


class TestListProjectsWithTelegramId:
    @pytest.mark.asyncio
    async def test_passes_telegram_id_header(self, api_client, mock_httpx_client):
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.return_value = [{"id": "proj-1"}]
        mock_httpx_client.request.return_value = resp

        result = await api_client.list_projects(telegram_id=99999)

        assert result == [{"id": "proj-1"}]
        call_kwargs = mock_httpx_client.request.call_args
        assert call_kwargs[1].get("headers", {}).get("X-Telegram-ID") == "99999"
