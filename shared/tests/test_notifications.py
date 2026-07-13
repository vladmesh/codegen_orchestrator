"""Tests for shared.notifications module — lazy config validation."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

from pydantic import ValidationError
import pytest

import shared.notifications as notifications_mod

TEST_TOKEN = "test-token-123"  # noqa: S105
TEST_API_URL = "http://api:8000"
DEFAULT_RATE_LIMIT = 10


@pytest.fixture(autouse=True)
def _reset_config():
    """Reset lazy config cache between tests."""
    notifications_mod._config = None
    yield
    notifications_mod._config = None


class TestNotificationConfig:
    """_ensure_config must fail fast when required env vars are missing."""

    def test_missing_telegram_bot_token_raises(self):
        """TELEGRAM_BOT_TOKEN must be set — no silent empty string."""
        env = {"API_BASE_URL": TEST_API_URL}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
                notifications_mod._ensure_config()

    def test_missing_api_base_url_raises(self):
        """API_BASE_URL must be set — no silent empty string."""
        env = {"TELEGRAM_BOT_TOKEN": TEST_TOKEN}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(RuntimeError, match="API_BASE_URL"):
                notifications_mod._ensure_config()

    def test_valid_config_returns_values(self):
        """When all required vars set, returns them correctly."""
        env = {
            "TELEGRAM_BOT_TOKEN": TEST_TOKEN,
            "API_BASE_URL": TEST_API_URL,
            "NOTIFICATION_RATE_LIMIT": "20",
        }
        with patch.dict(os.environ, env, clear=True):
            config = notifications_mod._ensure_config()
            assert config["telegram_token"] == TEST_TOKEN
            assert config["api_url"] == TEST_API_URL
            expected_limit = 20
            assert config["rate_limit"] == expected_limit

    def test_api_url_with_api_suffix_raises(self):
        """API_BASE_URL must not end with /api."""
        env = {
            "TELEGRAM_BOT_TOKEN": TEST_TOKEN,
            "API_BASE_URL": "http://api:8000/api",
        }
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(RuntimeError, match="must not include /api"):
                notifications_mod._ensure_config()

    def test_rate_limit_defaults_to_10(self):
        """NOTIFICATION_RATE_LIMIT is a tuning param — default OK."""
        env = {
            "TELEGRAM_BOT_TOKEN": TEST_TOKEN,
            "API_BASE_URL": TEST_API_URL,
        }
        with patch.dict(os.environ, env, clear=True):
            config = notifications_mod._ensure_config()
            assert config["rate_limit"] == DEFAULT_RATE_LIMIT

    def test_config_is_cached(self):
        """Second call returns cached config without re-reading env."""
        env = {
            "TELEGRAM_BOT_TOKEN": TEST_TOKEN,
            "API_BASE_URL": TEST_API_URL,
        }
        with patch.dict(os.environ, env, clear=True):
            config1 = notifications_mod._ensure_config()

        # Even with cleared env, cached config should be returned
        with patch.dict(os.environ, {}, clear=True):
            config2 = notifications_mod._ensure_config()

        assert config1 is config2

    def test_import_does_not_require_env_vars(self):
        """Importing the module should NOT raise even without env vars."""
        # This test validates lazy init — the module is already imported
        # but _ensure_config is not called at import time
        assert hasattr(notifications_mod, "notify_admins")
        assert hasattr(notifications_mod, "send_telegram_message")


class TestNotifyAdmins:
    @staticmethod
    def _session_with_users(users, status=200):
        response = AsyncMock()
        response.status = status
        response.json = AsyncMock(return_value=users)
        response.__aenter__ = AsyncMock(return_value=response)
        response.__aexit__ = AsyncMock(return_value=False)

        session = AsyncMock()
        session.get = MagicMock(return_value=response)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        return session

    @pytest.mark.asyncio
    async def test_empty_valid_user_list_returns_zero(self):
        session = self._session_with_users([])
        env = {"TELEGRAM_BOT_TOKEN": TEST_TOKEN, "API_BASE_URL": TEST_API_URL}

        with (
            patch.dict(os.environ, env, clear=True),
            patch("shared.notifications.aiohttp.ClientSession", return_value=session),
        ):
            assert await notifications_mod.notify_admins("test") == 0

    @pytest.mark.asyncio
    async def test_invalid_users_response_propagates_validation_error(self):
        session = self._session_with_users([{"telegram_id": "not-an-int"}])
        env = {"TELEGRAM_BOT_TOKEN": TEST_TOKEN, "API_BASE_URL": TEST_API_URL}

        with (
            patch.dict(os.environ, env, clear=True),
            patch("shared.notifications.aiohttp.ClientSession", return_value=session),
            pytest.raises(ValidationError),
        ):
            await notifications_mod.notify_admins("test")


class TestBestEffortNotifications:
    @pytest.mark.asyncio
    async def test_zero_recipients_is_a_valid_best_effort_result(self):
        with patch.object(notifications_mod, "notify_admins", new_callable=AsyncMock) as notify:
            notify.return_value = 0
            with patch.object(notifications_mod.logger, "error") as log_error:
                assert (
                    await notifications_mod.notify_admins_best_effort("test", component="test")
                    is None
                )

        log_error.assert_not_called()

    @pytest.mark.asyncio
    async def test_failure_logs_once_with_safe_context_and_error_type(self):
        with patch.object(notifications_mod, "notify_admins", new_callable=AsyncMock) as notify:
            notify.side_effect = RuntimeError("response payload must stay private")
            with patch.object(notifications_mod.logger, "error") as log_error:
                assert (
                    await notifications_mod.notify_admins_best_effort(
                        "test", component="test", server_handle="server-1"
                    )
                    is None
                )

        log_error.assert_called_once_with(
            "admin_notification_failed",
            level="info",
            error_type="RuntimeError",
            component="test",
            server_handle="server-1",
        )


class TestSendTelegramParseRetry:
    """send_telegram_message retries without parse_mode on entity parse errors."""

    async def test_retries_without_parse_mode_on_parse_error(self):
        """400 'can't parse entities' → retry with no parse_mode → success."""
        from unittest.mock import AsyncMock, MagicMock

        env = {
            "TELEGRAM_BOT_TOKEN": TEST_TOKEN,
            "API_BASE_URL": TEST_API_URL,
        }

        # First call: 400 parse error. Second call: 200 OK.
        mock_resp_400 = AsyncMock()
        mock_resp_400.status = 400
        mock_resp_400.text = AsyncMock(
            return_value='{"ok":false,"description":"Bad Request: can\'t parse entities"}'
        )
        mock_resp_400.__aenter__ = AsyncMock(return_value=mock_resp_400)
        mock_resp_400.__aexit__ = AsyncMock(return_value=False)

        mock_resp_200 = AsyncMock()
        mock_resp_200.status = 200
        mock_resp_200.__aenter__ = AsyncMock(return_value=mock_resp_200)
        mock_resp_200.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(side_effect=[mock_resp_400, mock_resp_200])
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch.dict(os.environ, env, clear=True),
            patch("shared.notifications.aiohttp.ClientSession", return_value=mock_session),
        ):
            result = await notifications_mod.send_telegram_message(
                telegram_id=123, text="test <b>bad</b>", parse_mode="HTML"
            )

        assert result is True
        # Second call should NOT have parse_mode
        second_call_payload = mock_session.post.call_args_list[1]
        assert "parse_mode" not in second_call_payload.kwargs.get(
            "json", second_call_payload[1].get("json", {})
        )

    async def test_no_retry_on_other_400_errors(self):
        """400 that is NOT about parse entities → no retry, return False."""
        from unittest.mock import AsyncMock, MagicMock

        env = {
            "TELEGRAM_BOT_TOKEN": TEST_TOKEN,
            "API_BASE_URL": TEST_API_URL,
        }

        mock_resp_400 = AsyncMock()
        mock_resp_400.status = 400
        mock_resp_400.text = AsyncMock(
            return_value='{"ok":false,"description":"Bad Request: chat not found"}'
        )
        mock_resp_400.__aenter__ = AsyncMock(return_value=mock_resp_400)
        mock_resp_400.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(side_effect=[mock_resp_400])
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch.dict(os.environ, env, clear=True),
            patch("shared.notifications.aiohttp.ClientSession", return_value=mock_session),
        ):
            result = await notifications_mod.send_telegram_message(
                telegram_id=123, text="test", parse_mode="HTML"
            )

        assert result is False
        assert mock_session.post.call_count == 1
