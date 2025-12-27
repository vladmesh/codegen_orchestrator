"""Unit tests for Telegram bot middleware."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.ext import ApplicationHandlerStop

from src.middleware import auth_middleware


@pytest.fixture
def mock_settings():
    """Mock configuration settings."""
    with patch("src.middleware.get_settings") as mock:
        mock.return_value.get_admin_ids.return_value = {123456789, 987654321}
        yield mock


@pytest.mark.asyncio
async def test_auth_middleware_allowed_user(mock_settings):
    """Test auth_middleware allows whitelisted user."""
    update = MagicMock()
    update.effective_user.id = 123456789
    context = MagicMock()

    result = await auth_middleware(update, context)

    assert result is True


@pytest.mark.asyncio
async def test_auth_middleware_denied_user(mock_settings):
    """Test auth_middleware rejects unauthorized user."""
    update = MagicMock()
    update.effective_user.id = 111111111  # Not in whitelist
    update.message.reply_text = AsyncMock()
    context = MagicMock()

    # Should raise stop propagation
    with pytest.raises(ApplicationHandlerStop):
        await auth_middleware(update, context)

    update.message.reply_text.assert_called_once()
    assert "Доступ запрещён" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_auth_middleware_no_whitelist_set():
    """Test auth_middleware allows all if whitelist is empty (if configured that way)."""
    with patch("src.middleware.get_settings") as mock:
        # Empty whitelist
        mock.return_value.get_admin_ids.return_value = set()

        update = MagicMock()
        update.effective_user.id = 55555
        context = MagicMock()

        result = await auth_middleware(update, context)

        # Current logic: verify behavior when list is empty
        # In code: if not admin_ids: return True (allow all warning)
        assert result is True
