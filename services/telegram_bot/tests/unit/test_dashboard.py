"""Tests for /dashboard command and dashboard callback."""

import os

os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("API_BASE_URL", "http://localhost:8000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-for-unit-tests")
os.environ.setdefault("LK_DOMAIN", "https://lk.test.example.com")

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest

from shared.contracts.dto.project import ProjectDTO
from src.keyboards import (
    PREFIX_DASHBOARD,
    main_menu_keyboard,
)

LK_DOMAIN = "https://lk.test.example.com"
PROJECT = ProjectDTO(
    id=uuid.uuid4(),
    title="Test",
    slug="test",
    status="active",
    owner_id=1,
    created_at=datetime(2026, 1, 1, tzinfo=UTC),
)


class FakeRedis:
    """Records what is stored, with its expiry, as Redis would hold it."""

    def __init__(self) -> None:
        self.values: dict[str, tuple[str, int]] = {}

    async def set(self, key: str, value: str, *, ex: int) -> None:
        self.values[key] = (value, ex)


def _minting(projects: list[ProjectDTO]):
    api = MagicMock()
    api.list_projects = AsyncMock(return_value=projects)
    settings = MagicMock(lk_domain=LK_DOMAIN)
    return (
        patch("src.dashboard.api_client", api),
        patch("src.dashboard.get_settings", return_value=settings),
    )


def _assert_one_time_token(redis: FakeRedis, url: str) -> None:
    [(key, (value, ttl))] = redis.values.items()
    assert key.startswith("lk_token:")
    assert (value, ttl) == ("42", 300)
    assert url == f"{LK_DOMAIN}/auth?token={key.removeprefix('lk_token:')}"


class TestMainMenuDashboardButton:
    """Dashboard button appears for all users."""

    def test_non_admin_sees_dashboard_button(self):
        keyboard = main_menu_keyboard("en", is_admin=False)
        all_buttons = [btn for row in keyboard.inline_keyboard for btn in row]
        callbacks = [btn.callback_data for btn in all_buttons]
        assert PREFIX_DASHBOARD in callbacks

    def test_admin_sees_dashboard_button(self):
        keyboard = main_menu_keyboard("en", is_admin=True)
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
        redis = FakeRedis()
        api, settings = _minting([PROJECT])

        with patch("src.main._stream_client", MagicMock(redis=redis)), api, settings:
            await dashboard(update, MagicMock())

        reply_markup = update.message.reply_text.call_args.kwargs["reply_markup"]
        [button] = [btn for row in reply_markup.inline_keyboard for btn in row]
        _assert_one_time_token(redis, button.url)

    @pytest.mark.asyncio
    async def test_dashboard_no_projects_shows_message(self):
        from src.main import dashboard

        for language_code, expected in (("ru", "нет проектов"), ("en", "no projects")):
            update = MagicMock()
            update.effective_user.id = 42
            update.effective_user.language_code = language_code
            update.message.reply_text = AsyncMock()
            redis = FakeRedis()
            api, settings = _minting([])

            with patch("src.main._stream_client", MagicMock(redis=redis)), api, settings:
                await dashboard(update, MagicMock())

            update.message.reply_text.assert_called_once()
            reply_text = update.message.reply_text.call_args[0][0]
            assert expected in reply_text.lower()
            assert redis.values == {}

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

    @staticmethod
    def _callback():
        query = AsyncMock()
        query.data = PREFIX_DASHBOARD
        query.from_user.id = 42
        query.from_user.username = "testuser"
        update = MagicMock()
        update.callback_query = query
        update.effective_user.language_code = "en"
        return update, query

    @pytest.mark.asyncio
    async def test_callback_generates_token_and_sends_url(self):
        from src.handlers import handle_callback_query

        update, query = self._callback()
        redis = FakeRedis()
        api, settings = _minting([PROJECT])

        with (
            patch("src.handlers.is_admin", return_value=False),
            patch("src.handlers._get_stream_client", return_value=MagicMock(redis=redis)),
            api,
            settings,
        ):
            await handle_callback_query(update, MagicMock())

        reply_markup = query.edit_message_text.call_args.kwargs["reply_markup"]
        [url] = [btn.url for row in reply_markup.inline_keyboard for btn in row if btn.url]
        _assert_one_time_token(redis, url)

    @pytest.mark.asyncio
    async def test_callback_without_projects_mints_nothing(self):
        from src.handlers import handle_callback_query
        from src.texts import text

        update, query = self._callback()
        redis = FakeRedis()
        api, settings = _minting([])

        with (
            patch("src.handlers.is_admin", return_value=False),
            patch("src.handlers._get_stream_client", return_value=MagicMock(redis=redis)),
            api,
            settings,
        ):
            await handle_callback_query(update, MagicMock())

        assert query.edit_message_text.call_args.args[0] == text("en", "no_projects_md")
        assert redis.values == {}
