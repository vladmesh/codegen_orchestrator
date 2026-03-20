"""Tests for /dashboard command and dashboard callback."""

import os

os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("API_BASE_URL", "http://localhost:8000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-for-unit-tests")
os.environ.setdefault("LK_DOMAIN", "https://lk.test.example.com")

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.keyboards import (
    PREFIX_DASHBOARD,
    main_menu_keyboard,
)


class TestMainMenuDashboardButton:
    """Dashboard button appears for all users."""

    def test_non_admin_sees_dashboard_button(self):
        keyboard = main_menu_keyboard(is_admin=False)
        all_buttons = [btn for row in keyboard.inline_keyboard for btn in row]
        callbacks = [btn.callback_data for btn in all_buttons]
        assert PREFIX_DASHBOARD in callbacks

    def test_admin_sees_dashboard_button(self):
        keyboard = main_menu_keyboard(is_admin=True)
        all_buttons = [btn for row in keyboard.inline_keyboard for btn in row]
        callbacks = [btn.callback_data for btn in all_buttons]
        assert PREFIX_DASHBOARD in callbacks


class TestDashboardCommand:
    """Test /dashboard command handler."""

    @pytest.mark.asyncio
    async def test_dashboard_generates_token_and_sends_url(self):
        from src.main import dashboard

        update = MagicMock()
        update.effective_user.id = 42
        update.message.reply_text = AsyncMock()

        context = MagicMock()

        mock_redis = AsyncMock()

        with (
            patch("src.main._stream_client") as mock_client,
            patch("src.main.api_client") as mock_api,
            patch("src.main.get_settings") as mock_settings,
        ):
            mock_client.redis = mock_redis
            mock_api.get_json = AsyncMock(return_value=[{"id": "proj-1", "name": "Test"}])
            mock_settings.return_value.lk_domain = "https://lk.test.example.com"

            await dashboard(update, context)

        # Verify Redis set was called with lk_token:* pattern, TTL 300
        mock_redis.set.assert_called_once()
        call_args = mock_redis.set.call_args
        key = call_args[0][0]
        value = call_args[0][1]
        assert key.startswith("lk_token:")
        assert value == "42"
        assert call_args[1]["ex"] == 300

        # Verify reply contains inline button with URL
        update.message.reply_text.assert_called_once()
        call_kwargs = update.message.reply_text.call_args[1]
        reply_markup = call_kwargs["reply_markup"]
        buttons = [btn for row in reply_markup.inline_keyboard for btn in row]
        assert len(buttons) == 1
        assert buttons[0].url.startswith("https://lk.test.example.com/auth?token=")

    @pytest.mark.asyncio
    async def test_dashboard_no_projects_shows_message(self):
        from src.main import dashboard

        update = MagicMock()
        update.effective_user.id = 42
        update.message.reply_text = AsyncMock()

        context = MagicMock()

        with (
            patch("src.main._stream_client") as mock_client,
            patch("src.main.api_client") as mock_api,
        ):
            mock_client.redis = AsyncMock()
            mock_api.get_json = AsyncMock(return_value=[])

            await dashboard(update, context)

        update.message.reply_text.assert_called_once()
        reply_text = update.message.reply_text.call_args[0][0]
        assert "нет проектов" in reply_text.lower()

    @pytest.mark.asyncio
    async def test_dashboard_no_redis_raises(self):
        from src.main import dashboard

        update = MagicMock()
        update.effective_user.id = 42
        update.message.reply_text = AsyncMock()

        context = MagicMock()

        with patch("src.main._stream_client", None):
            with pytest.raises(RuntimeError, match="Redis client not initialized"):
                await dashboard(update, context)


class TestDashboardCallback:
    """Test dashboard callback from inline button."""

    @pytest.mark.asyncio
    async def test_callback_generates_token_and_sends_url(self):
        from src.handlers import handle_callback_query

        query = AsyncMock()
        query.data = PREFIX_DASHBOARD
        query.from_user.id = 42
        query.from_user.username = "testuser"

        update = MagicMock()
        update.callback_query = query

        context = MagicMock()

        mock_redis = AsyncMock()

        with (
            patch("src.handlers.is_admin", return_value=False),
            patch("src.handlers._get_stream_client") as mock_get_client,
            patch("src.handlers.api_client") as mock_api,
            patch("src.handlers.get_settings") as mock_settings,
        ):
            mock_client = MagicMock()
            mock_client.redis = mock_redis
            mock_get_client.return_value = mock_client
            mock_api.get_json = AsyncMock(return_value=[{"id": "proj-1"}])
            mock_settings.return_value.lk_domain = "https://lk.test.example.com"

            await handle_callback_query(update, context)

        # Verify Redis set
        mock_redis.set.assert_called_once()
        key = mock_redis.set.call_args[0][0]
        assert key.startswith("lk_token:")

        # Verify edit_message_text with URL button
        query.edit_message_text.assert_called_once()
        call_kwargs = query.edit_message_text.call_args[1]
        reply_markup = call_kwargs["reply_markup"]
        buttons = [btn for row in reply_markup.inline_keyboard for btn in row]
        url_buttons = [btn for btn in buttons if btn.url]
        assert len(url_buttons) == 1
        assert url_buttons[0].url.startswith("https://lk.test.example.com/auth?token=")
