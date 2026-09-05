"""Unit tests for Telegram bot handlers and backend access admission."""

from __future__ import annotations

from typing import Final
from unittest.mock import AsyncMock, MagicMock

import pytest

TEST_TELEGRAM_USER_ID: Final[int] = 123456789


@pytest.fixture
def mock_update() -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = TEST_TELEGRAM_USER_ID
    update.effective_user.first_name = "John"
    update.message = MagicMock()
    update.message.text = "/command test"
    update.message.reply_text = AsyncMock()
    return update


@pytest.fixture
def mock_context() -> MagicMock:
    context = MagicMock()
    context.args = ["arg1", "arg2"]
    return context


@pytest.fixture
def mock_broker():
    from unittest.mock import patch

    mock = MagicMock()
    mock.connect = AsyncMock()
    mock.close = AsyncMock()
    with patch("services.tg_bot.src.main.get_broker", return_value=mock):
        yield mock


@pytest.fixture
def mock_publish():
    from unittest.mock import patch

    with patch("services.tg_bot.src.main.publish_command_received") as mock:
        mock.return_value = None
        yield mock


@pytest.mark.asyncio
async def test_handle_command_publishes_event(
    mock_publish: AsyncMock,
    mock_broker: MagicMock,
    mock_update: MagicMock,
    mock_context: MagicMock,
) -> None:
    from services.tg_bot.src.main import handle_command

    await handle_command(mock_update, mock_context)

    mock_publish.assert_awaited_once()
    event = mock_publish.await_args.args[0]
    assert event.command == "/command test"
    assert event.args == ["arg1", "arg2"]
    assert event.user_id == TEST_TELEGRAM_USER_ID


@pytest.mark.asyncio
async def test_post_init_and_shutdown_manage_broker(mock_broker: MagicMock) -> None:
    from services.tg_bot.src.main import post_init, post_shutdown

    application = MagicMock()
    await post_init(application)
    await post_shutdown(application)

    mock_broker.connect.assert_awaited_once()
    mock_broker.close.assert_awaited_once()


class TestBackendAccess:
    @pytest.mark.asyncio
    async def test_active_identity_is_admitted(self) -> None:
        from unittest.mock import patch

        from shared.generated.schemas import UserAccess

        access = UserAccess(
            user_id=1,
            status="active",
            channel="telegram",
            external_id=str(TEST_TELEGRAM_USER_ID),
        )
        with patch("services.tg_bot.src.main.BackendClient") as client_class:
            client = AsyncMock()
            client.resolve = AsyncMock(return_value=access)
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=None)
            client_class.return_value = client

            from services.tg_bot.src.main import _has_active_access

            assert await _has_active_access(TEST_TELEGRAM_USER_ID)

    @pytest.mark.asyncio
    async def test_revoked_and_unknown_identities_are_denied(self) -> None:
        from unittest.mock import patch

        from shared.generated.schemas import UserAccess

        revoked = UserAccess(
            user_id=1,
            status="inactive",
            channel="telegram",
            external_id=str(TEST_TELEGRAM_USER_ID),
        )
        with patch("services.tg_bot.src.main.BackendClient") as client_class:
            client = AsyncMock()
            client.resolve = AsyncMock(return_value=revoked)
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=None)
            client_class.return_value = client

            from services.tg_bot.src.main import _has_active_access

            assert not await _has_active_access(TEST_TELEGRAM_USER_ID)

            import httpx

            client.resolve.side_effect = httpx.HTTPStatusError(
                "not found", request=MagicMock(), response=MagicMock(status_code=404)
            )
            assert not await _has_active_access(TEST_TELEGRAM_USER_ID)

    @pytest.mark.asyncio
    async def test_malformed_identity_is_denied_without_backend_lookup(self) -> None:
        from unittest.mock import patch

        with patch("services.tg_bot.src.main.BackendClient") as client_class:
            from services.tg_bot.src.main import _has_active_access

            assert not await _has_active_access(0)
            client_class.assert_not_called()

    @pytest.mark.asyncio
    async def test_revoked_identity_stops_before_handlers(self, mock_update: MagicMock) -> None:
        from unittest.mock import patch

        from telegram.ext import ApplicationHandlerStop
        from shared.generated.schemas import UserAccess

        revoked = UserAccess(
            user_id=1,
            status="inactive",
            channel="telegram",
            external_id=str(TEST_TELEGRAM_USER_ID),
        )
        with patch("services.tg_bot.src.main.BackendClient") as client_class:
            client = AsyncMock()
            client.resolve = AsyncMock(return_value=revoked)
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=None)
            client_class.return_value = client

            from services.tg_bot.src.main import enforce_access

            with pytest.raises(ApplicationHandlerStop):
                await enforce_access(mock_update, MagicMock())

    @pytest.mark.asyncio
    async def test_non_http_resolver_failure_stops_before_group_zero_handler(self) -> None:
        from datetime import UTC, datetime
        from unittest.mock import patch

        from telegram import Chat, Message, Update, User
        from telegram.ext import ApplicationBuilder, TypeHandler

        from services.tg_bot.src.main import enforce_access

        group_zero_handler = AsyncMock()
        user = User(id=TEST_TELEGRAM_USER_ID, is_bot=False, first_name="Test")
        update = Update(
            update_id=1,
            message=Message(
                message_id=1,
                date=datetime.now(UTC),
                chat=Chat(id=TEST_TELEGRAM_USER_ID, type="private"),
                from_user=user,
                text="/start",
            ),
        )
        with (
            patch("telegram.Bot.initialize", new=AsyncMock()),
            patch("telegram.Bot.get_me", new=AsyncMock()),
        ):
            application = ApplicationBuilder().token("test:token").build()
            application.add_handler(
                TypeHandler(Update, enforce_access),
                group=-1,
            )
            application.add_handler(TypeHandler(Update, group_zero_handler), group=0)
            await application.initialize()

            with patch(
                "services.tg_bot.src.main._has_active_access",
                new=AsyncMock(side_effect=ValueError("unexpected resolver failure")),
            ):
                await application.process_update(update)

            await application.shutdown()

        group_zero_handler.assert_not_awaited()



class TestHandleStart:
    @pytest.mark.asyncio
    async def test_handle_start_greets_user(
        self, mock_update: MagicMock, mock_context: MagicMock
    ) -> None:
        from services.tg_bot.src.main import DEFAULT_GREETING, handle_start

        await handle_start(mock_update, mock_context)

        mock_update.message.reply_text.assert_awaited_once()
        assert DEFAULT_GREETING in mock_update.message.reply_text.await_args.args[0]

    @pytest.mark.asyncio
    async def test_handle_start_skips_update_without_user(self, mock_context: MagicMock) -> None:
        from services.tg_bot.src.main import handle_start

        update = MagicMock()
        update.effective_user = None
        update.message = MagicMock()
        update.message.reply_text = AsyncMock()

        await handle_start(update, mock_context)

        update.message.reply_text.assert_not_awaited()
