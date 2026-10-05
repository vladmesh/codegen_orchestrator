"""Unit tests for Telegram bot handlers and backend access admission."""

from __future__ import annotations

from collections.abc import Iterator
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
def mock_broker() -> Iterator[MagicMock]:
    from unittest.mock import patch

    mock = MagicMock()
    mock.connect = AsyncMock()
    mock.close = AsyncMock()
    with patch("services.tg_bot.src.main.get_broker", return_value=mock):
        yield mock


@pytest.fixture
def mock_publish() -> Iterator[AsyncMock]:
    from unittest.mock import patch

    with patch("services.tg_bot.src.main.publish_command_received") as mock:
        mock.return_value = None
        yield mock


@pytest.fixture
def mock_bindings(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    from services.tg_bot.src.main import bindings

    mock = MagicMock()
    mock.start = AsyncMock()
    mock.stop = AsyncMock()
    monkeypatch.setattr(bindings, "start", mock.start)
    monkeypatch.setattr(bindings, "stop", mock.stop)
    return mock


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
    awaited = mock_publish.await_args
    assert awaited is not None
    event = awaited.args[0]
    assert event.command == "/command test"
    assert event.args == ["arg1", "arg2"]
    assert event.user_id == TEST_TELEGRAM_USER_ID


@pytest.mark.asyncio
async def test_post_init_and_shutdown_manage_broker(
    mock_broker: MagicMock, mock_bindings: MagicMock
) -> None:
    from unittest.mock import call

    from services.tg_bot.src.main import post_init, post_shutdown

    lifecycle = MagicMock()
    lifecycle.attach_mock(mock_broker.connect, "connect")
    lifecycle.attach_mock(mock_bindings.start, "start")
    lifecycle.attach_mock(mock_bindings.stop, "stop")
    lifecycle.attach_mock(mock_broker.close, "close")
    application = MagicMock()
    await post_init(application)
    await post_shutdown(application)

    mock_broker.connect.assert_awaited_once()
    mock_broker.close.assert_awaited_once()
    mock_bindings.start.assert_awaited_once_with(application)
    mock_bindings.stop.assert_awaited_once_with(application)
    assert lifecycle.mock_calls == [
        call.connect(),
        call.start(application),
        call.stop(application),
        call.close(),
    ]


@pytest.mark.asyncio
async def test_post_init_closes_broker_when_binding_start_fails(
    mock_broker: MagicMock, mock_bindings: MagicMock
) -> None:
    from services.tg_bot.src.main import post_init

    mock_bindings.start.side_effect = RuntimeError("binding start failed")
    application = MagicMock()
    with pytest.raises(RuntimeError, match="binding start failed"):
        await post_init(application)

    mock_broker.connect.assert_awaited_once()
    mock_bindings.start.assert_awaited_once_with(application)
    mock_broker.close.assert_awaited_once()
    mock_bindings.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_shutdown_closes_broker_when_binding_stop_fails(
    mock_broker: MagicMock, mock_bindings: MagicMock
) -> None:
    from services.tg_bot.src.main import post_shutdown

    mock_bindings.stop.side_effect = RuntimeError("binding stop failed")
    application = MagicMock()
    with pytest.raises(RuntimeError, match="binding stop failed"):
        await post_shutdown(application)

    mock_bindings.stop.assert_awaited_once_with(application)
    mock_broker.close.assert_awaited_once()


class TestBackendAccess:
    @pytest.mark.asyncio
    async def test_active_identity_is_admitted(self) -> None:
        from unittest.mock import patch

        from shared.generated.schemas import Status, UserAccess

        access = UserAccess(
            user_id=1,
            status=Status.active,
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

        from shared.generated.schemas import Status, UserAccess

        revoked = UserAccess(
            user_id=1,
            status=Status.inactive,
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

        from shared.generated.schemas import Status, UserAccess

        revoked = UserAccess(
            user_id=1,
            status=Status.inactive,
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


class TestBackendCallsAsTelegramUser:
    @pytest.mark.asyncio
    async def test_package_route_call_carries_the_telegram_identity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import httpx

        from services.tg_bot.src.main import BackendClient

        monkeypatch.setenv("BACKEND_API_URL", "http://backend:8000")
        monkeypatch.setenv("USER_IDENTITY_CAPABILITY", "test-identity-capability")
        sent: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return httpx.Response(200, json=[])

        async with BackendClient() as client:
            await client._ensure_client().aclose()
            client._client = httpx.AsyncClient(
                base_url=client.base_url, transport=httpx.MockTransport(handler)
            )
            response = await client.request_as_telegram_user(
                "get", "/reminders", TEST_TELEGRAM_USER_ID
            )

        assert response.json() == []
        assert len(sent) == 1
        assert sent[0].url.path == "/reminders"
        assert sent[0].headers.get_list("X-Identity-Capability") == ["test-identity-capability"]
        assert sent[0].headers.get_list("X-User-Channel") == ["telegram"]
        assert sent[0].headers.get_list("X-User-External-Id") == [str(TEST_TELEGRAM_USER_ID)]

    @pytest.mark.asyncio
    async def test_package_route_call_fails_closed_without_identity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from services.tg_bot.src.main import BackendClient

        monkeypatch.setenv("BACKEND_API_URL", "http://backend:8000")
        monkeypatch.delenv("USER_IDENTITY_CAPABILITY", raising=False)

        async with BackendClient() as client:
            with pytest.raises(ValueError, match="valid Telegram user id"):
                await client.request_as_telegram_user("get", "/reminders", 0)
            with pytest.raises(RuntimeError, match="USER_IDENTITY_CAPABILITY is not set"):
                await client.request_as_telegram_user("get", "/reminders", TEST_TELEGRAM_USER_ID)


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
