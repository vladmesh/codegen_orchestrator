"""The bot speaks Russian to a Russian Telegram client and English to everyone else."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.ext import ApplicationHandlerStop

from src import middleware
from src.keyboards import main_menu_keyboard
from src.texts import TEXTS, locale_for, text, user_locale


@pytest.mark.parametrize(
    ("language_code", "locale"),
    [("ru", "ru"), ("ru-RU", "ru"), ("RU", "ru"), ("en", "en"), ("en-US", "en"), ("de", "en")],
)
def test_locale_follows_the_telegram_language_code(language_code: str, locale: str) -> None:
    assert locale_for(language_code) == locale


def test_a_user_without_a_language_code_gets_english() -> None:
    assert locale_for(None) == "en"


def test_an_update_without_a_user_gets_english() -> None:
    update = MagicMock()
    update.effective_user = None
    assert user_locale(update) == "en"


def test_every_locale_has_the_same_keys() -> None:
    assert TEXTS["ru"].keys() == TEXTS["en"].keys()


def test_an_unknown_locale_falls_back_to_english() -> None:
    assert text("de", "promo_activated") == text("en", "promo_activated")


def test_main_menu_buttons_follow_the_locale() -> None:
    def labels(locale: str) -> list[str]:
        keyboard = main_menu_keyboard(locale, is_admin=True)
        return [button.text for row in keyboard.inline_keyboard for button in row]

    assert labels("en")[:3] == ["📦 Projects", "➕ New project", "📊 My dashboard"]
    assert labels("ru")[:3] == ["📦 Список проектов", "➕ Новый проект", "📊 Мой дашборд"]


@pytest.mark.asyncio
async def test_main_menu_callback_answers_in_english_for_an_english_user() -> None:
    from src.handlers import handle_callback_query

    query = AsyncMock()
    query.data = "menu:back"
    update = MagicMock()
    update.callback_query = query
    update.effective_user.language_code = "en"
    context = MagicMock()
    context.user_data = {}

    await handle_callback_query(update, context)

    assert query.edit_message_text.call_args.args[0] == "🏠 *Main menu*\n\nChoose an action:"


@pytest.mark.parametrize(
    ("language_code", "expected"),
    [("en", "To get started, send your one-time promo code."), ("ru", "Чтобы начать")],
)
@pytest.mark.asyncio
async def test_promo_prompt_follows_the_locale(monkeypatch, language_code, expected) -> None:
    update = AsyncMock()
    update.effective_user.id = 44
    update.effective_user.language_code = language_code
    update.message.text = "/start"
    update.message.reply_text = AsyncMock()
    context = AsyncMock()
    context.user_data = {}
    monkeypatch.setattr(middleware, "_check_user_in_db", AsyncMock(return_value=None))

    with pytest.raises(ApplicationHandlerStop):
        await middleware.auth_middleware(update, context)

    assert expected in update.message.reply_text.await_args.args[0]
