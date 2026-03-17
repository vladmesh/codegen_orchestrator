"""Unit tests for PO validate_telegram_token tool."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

TOOL_CONFIG = {"configurable": {"thread_id": "po-user-1", "user_id": "93459832"}}
GETME_URL = "https://api.telegram.org/bot<token>/getMe"


def _getme_json(username="fortune_teller_bot"):
    return {"ok": True, "result": {"id": 123, "username": username, "is_bot": True}}


def _mock_httpx(response):
    """Create a mock httpx.AsyncClient context manager."""
    instance = AsyncMock()
    instance.get = AsyncMock(return_value=response)
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=False)
    return instance


@pytest.fixture
def mock_api():
    """Mock API client for set_project_secret calls."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"keys": ["TELEGRAM_BOT_USERNAME"]}
    mock_response.raise_for_status = MagicMock()

    api = AsyncMock()
    api.post = AsyncMock(return_value=mock_response)
    return api


class TestValidateTelegramToken:
    """validate_telegram_token calls getMe, stores username on success."""

    @pytest.mark.asyncio
    async def test_valid_token_returns_username(self, mock_api):
        """Valid token -> getMe succeeds -> returns bot username."""
        resp = httpx.Response(200, json=_getme_json(), request=httpx.Request("GET", GETME_URL))
        http_mock = _mock_httpx(resp)

        with (
            patch("src.agents.po.tools_projects._get_api", return_value=mock_api),
            patch("src.agents.po.tools_projects.httpx.AsyncClient", return_value=http_mock),
        ):
            from src.agents.po.tools import validate_telegram_token

            result = await validate_telegram_token.ainvoke(
                {"project_id": "proj-1", "token": "123:ABC-def"},
                config=TOOL_CONFIG,
            )

        assert "@fortune_teller_bot" in result
        assert mock_api.post.call_count == 2

        first_call = mock_api.post.call_args_list[0]
        assert "/config/secrets" in first_call[0][0]
        token_payload = first_call[1]["json"]["secrets"]
        assert token_payload["TELEGRAM_BOT_TOKEN"] == "123:ABC-def"  # noqa: S105

        second_call = mock_api.post.call_args_list[1]
        username_payload = second_call[1]["json"]["secrets"]
        assert username_payload["TELEGRAM_BOT_USERNAME"] == "fortune_teller_bot"

    @pytest.mark.asyncio
    async def test_invalid_token_returns_error(self, mock_api):
        """Invalid token -> getMe fails -> error, nothing stored."""
        resp = httpx.Response(
            401,
            json={"ok": False, "error_code": 401, "description": "Unauthorized"},
            request=httpx.Request("GET", GETME_URL),
        )
        http_mock = _mock_httpx(resp)

        with (
            patch("src.agents.po.tools_projects._get_api", return_value=mock_api),
            patch("src.agents.po.tools_projects.httpx.AsyncClient", return_value=http_mock),
        ):
            from src.agents.po.tools import validate_telegram_token

            result = await validate_telegram_token.ainvoke(
                {"project_id": "proj-1", "token": "invalid-token"},
                config=TOOL_CONFIG,
            )

        assert "invalid" in result.lower()
        mock_api.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_network_error_returns_error(self, mock_api):
        """Network timeout -> error, nothing stored."""
        instance = AsyncMock()
        instance.get = AsyncMock(side_effect=httpx.ConnectTimeout("timeout"))
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("src.agents.po.tools_projects._get_api", return_value=mock_api),
            patch("src.agents.po.tools_projects.httpx.AsyncClient", return_value=instance),
        ):
            from src.agents.po.tools import validate_telegram_token

            result = await validate_telegram_token.ainvoke(
                {"project_id": "proj-1", "token": "123:ABC-def"},
                config=TOOL_CONFIG,
            )

        assert "error" in result.lower()
        mock_api.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_getme_missing_username_returns_error(self, mock_api):
        """getMe returns ok but no username -> error."""
        resp = httpx.Response(
            200,
            json={"ok": True, "result": {"id": 123, "is_bot": True}},
            request=httpx.Request("GET", GETME_URL),
        )
        http_mock = _mock_httpx(resp)

        with (
            patch("src.agents.po.tools_projects._get_api", return_value=mock_api),
            patch("src.agents.po.tools_projects.httpx.AsyncClient", return_value=http_mock),
        ):
            from src.agents.po.tools import validate_telegram_token

            result = await validate_telegram_token.ainvoke(
                {"project_id": "proj-1", "token": "123:ABC-def"},
                config=TOOL_CONFIG,
            )

        assert "error" in result.lower() or "username" in result.lower()
        mock_api.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_hint_included_for_both_secrets(self, mock_api):
        """Both secrets should include env_hints."""
        resp = httpx.Response(
            200,
            json=_getme_json("my_bot"),
            request=httpx.Request("GET", GETME_URL),
        )
        http_mock = _mock_httpx(resp)

        with (
            patch("src.agents.po.tools_projects._get_api", return_value=mock_api),
            patch("src.agents.po.tools_projects.httpx.AsyncClient", return_value=http_mock),
        ):
            from src.agents.po.tools import validate_telegram_token

            await validate_telegram_token.ainvoke(
                {"project_id": "proj-1", "token": "123:ABC-def"},
                config=TOOL_CONFIG,
            )

        for call in mock_api.post.call_args_list:
            payload = call[1]["json"]
            assert "env_hints" in payload
