"""Tests for admin Add User flow."""

import os

os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("API_BASE_URL", "http://localhost:8000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-for-unit-tests")

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.keyboards import (
    ACTION_ADD_USER,
    PREFIX_ADMIN,
    main_menu_keyboard,
)


class TestMainMenuKeyboardAddUser:
    """Test that Add User button appears for admins."""

    def test_admin_sees_add_user_button(self):
        keyboard = main_menu_keyboard(is_admin=True)
        all_buttons = [btn for row in keyboard.inline_keyboard for btn in row]
        callbacks = [btn.callback_data for btn in all_buttons]
        assert f"{PREFIX_ADMIN}:{ACTION_ADD_USER}" in callbacks

    def test_non_admin_no_add_user_button(self):
        keyboard = main_menu_keyboard(is_admin=False)
        all_buttons = [btn for row in keyboard.inline_keyboard for btn in row]
        callbacks = [btn.callback_data for btn in all_buttons]
        assert f"{PREFIX_ADMIN}:{ACTION_ADD_USER}" not in callbacks


class TestAdminCallbackHandler:
    """Test admin callback handler dispatching."""

    @pytest.mark.asyncio
    async def test_handle_admin_add_user_sets_flag(self):
        """Clicking Add User button sets awaiting flag and prompts for ID."""
        from src.handlers import handle_callback_query

        query = AsyncMock()
        query.data = f"{PREFIX_ADMIN}:{ACTION_ADD_USER}"
        query.from_user.id = 111
        query.from_user.username = "admin"

        update = MagicMock()
        update.callback_query = query

        context = MagicMock()
        context.user_data = {}

        with patch("src.handlers.is_admin", return_value=True):
            await handle_callback_query(update, context)

        assert context.user_data.get("awaiting_add_user") is True
        query.edit_message_text.assert_called_once()
        call_text = query.edit_message_text.call_args[0][0]
        assert "Telegram ID" in call_text


class TestAddUserInput:
    """Test text handler for receiving telegram_id."""

    @pytest.mark.asyncio
    async def test_valid_telegram_id_creates_user(self):
        from src.handlers import handle_add_user_input

        update = MagicMock()
        update.message.text = "123456789"
        update.message.reply_text = AsyncMock()
        update.effective_user.id = 111

        context = MagicMock()
        context.user_data = {"awaiting_add_user": True}

        with patch("src.handlers.api_client") as mock_api:
            mock_api.post_json = AsyncMock(
                return_value={"id": 1, "telegram_id": 123456789, "is_admin": False}
            )
            await handle_add_user_input(update, context)

        mock_api.post_json.assert_called_once()
        call_args = mock_api.post_json.call_args
        assert call_args[0][0] == "users/"
        assert call_args[1]["json"]["telegram_id"] == 123456789
        assert context.user_data.get("awaiting_add_user") is None
        update.message.reply_text.assert_called_once()
        assert "123456789" in update.message.reply_text.call_args[0][0]

    @pytest.mark.asyncio
    async def test_invalid_input_asks_again(self):
        from src.handlers import handle_add_user_input

        update = MagicMock()
        update.message.text = "not-a-number"
        update.message.reply_text = AsyncMock()
        update.effective_user.id = 111

        context = MagicMock()
        context.user_data = {"awaiting_add_user": True}

        await handle_add_user_input(update, context)

        # Flag should remain set
        assert context.user_data.get("awaiting_add_user") is True
        update.message.reply_text.assert_called_once()
        assert (
            "число" in update.message.reply_text.call_args[0][0].lower()
            or "цифр" in update.message.reply_text.call_args[0][0].lower()
        )

    @pytest.mark.asyncio
    async def test_duplicate_user_reports_error(self):
        import httpx

        from src.handlers import handle_add_user_input

        update = MagicMock()
        update.message.text = "123456789"
        update.message.reply_text = AsyncMock()
        update.effective_user.id = 111

        context = MagicMock()
        context.user_data = {"awaiting_add_user": True}

        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.json.return_value = {"detail": "User with this telegram_id already exists"}

        with patch("src.handlers.api_client") as mock_api:
            mock_api.post_json = AsyncMock(
                side_effect=httpx.HTTPStatusError(
                    "400", request=MagicMock(), response=mock_response
                )
            )
            await handle_add_user_input(update, context)

        assert context.user_data.get("awaiting_add_user") is None
        update.message.reply_text.assert_called_once()
        reply_text = update.message.reply_text.call_args[0][0]
        assert (
            "уже" in reply_text.lower()
            or "exists" in reply_text.lower()
            or "существует" in reply_text.lower()
        )

    @pytest.mark.asyncio
    async def test_not_awaiting_returns_none(self):
        """If not in add_user flow, handler returns None (pass-through)."""
        from src.handlers import handle_add_user_input

        update = MagicMock()
        update.message.text = "123456789"

        context = MagicMock()
        context.user_data = {}

        result = await handle_add_user_input(update, context)
        assert result is None
        update.message.reply_text.assert_not_called()
