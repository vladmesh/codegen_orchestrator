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


def _json_response(payload):
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status = MagicMock()
    return resp


@pytest.fixture
def mock_api():
    """Mock API client: secret writes plus the repository lookup/patch."""
    api = AsyncMock()
    api.post = AsyncMock(return_value=_json_response({"keys": ["TELEGRAM_BOT_USERNAME"]}))
    api.get = AsyncMock(
        return_value=_json_response(
            [{"id": "repo-1", "role": "primary", "bot_username": None}],
        )
    )
    api.patch = AsyncMock(return_value=_json_response({"id": "repo-1"}))
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


class TestBotUsernamePersisted:
    """The username lands on the primary repository — that's what QA reads."""

    async def _validate(self, mock_api, username, token="123:ABC-def"):  # noqa: S107
        resp = httpx.Response(
            200, json=_getme_json(username), request=httpx.Request("GET", GETME_URL)
        )
        http_mock = _mock_httpx(resp)

        with (
            patch("src.agents.po.tools_projects._get_api", return_value=mock_api),
            patch("src.agents.po.tools_projects.httpx.AsyncClient", return_value=http_mock),
        ):
            from src.agents.po.tools import validate_telegram_token

            return await validate_telegram_token.ainvoke(
                {"project_id": "proj-1", "token": token},
                config=TOOL_CONFIG,
            )

    @pytest.mark.asyncio
    async def test_username_patched_onto_primary_repository(self, mock_api):
        """Valid token -> PATCH /api/repositories/<primary id> with bot_username."""
        await self._validate(mock_api, "palindrome_bot")

        mock_api.get.assert_awaited_once()
        assert "project_id=proj-1" in mock_api.get.call_args[0][0]

        mock_api.patch.assert_awaited_once()
        assert mock_api.patch.call_args[0][0] == "/api/repositories/repo-1"
        assert mock_api.patch.call_args[1]["json"] == {"bot_username": "palindrome_bot"}

    @pytest.mark.asyncio
    async def test_revalidation_overwrites_stored_username(self, mock_api):
        """A second token for the same project moves it to the new bot."""
        mock_api.get.return_value = _json_response(
            [{"id": "repo-1", "role": "primary", "bot_username": "old_bot"}]
        )

        await self._validate(mock_api, "new_bot", token="999:XYZ-ghi")  # noqa: S106

        assert mock_api.patch.call_args[1]["json"] == {"bot_username": "new_bot"}

    @pytest.mark.asyncio
    async def test_dependency_repos_are_not_written_to(self, mock_api):
        """Only the primary repository carries the username."""
        mock_api.get.return_value = _json_response(
            [
                {"id": "repo-dep", "role": "dependency", "bot_username": None},
                {"id": "repo-primary", "role": "primary", "bot_username": None},
            ]
        )

        await self._validate(mock_api, "palindrome_bot")

        assert mock_api.patch.call_args[0][0] == "/api/repositories/repo-primary"

    @pytest.mark.asyncio
    async def test_missing_primary_repository_raises(self, mock_api):
        """No repository to store on -> crash instead of a silent no-op."""
        mock_api.get.return_value = _json_response([])

        with pytest.raises(RuntimeError, match="no primary repository"):
            await self._validate(mock_api, "palindrome_bot")

    @pytest.mark.asyncio
    async def test_failed_patch_raises(self, mock_api):
        """A rejected PATCH must not be reported to the user as stored."""
        patch_resp = MagicMock()
        patch_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=httpx.Request("PATCH", "/api/repositories/repo-1"), response=None
        )
        mock_api.patch.return_value = patch_resp

        with pytest.raises(httpx.HTTPStatusError):
            await self._validate(mock_api, "palindrome_bot")

    @pytest.mark.asyncio
    async def test_invalid_token_writes_nothing(self, mock_api):
        """Rejected token -> no secrets, no repository write."""
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

            await validate_telegram_token.ainvoke(
                {"project_id": "proj-1", "token": "invalid-token"},
                config=TOOL_CONFIG,
            )

        mock_api.patch.assert_not_called()
