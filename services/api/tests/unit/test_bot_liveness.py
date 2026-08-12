"""Is the bot behind a stored token live right now, and who is told what.

The question is asked here because the token is here. What the caller — QA, in
practice — gets back is a state and the username Telegram reported, so a run can
establish that the bot answers without the credential ever entering its runtime.

The three states are the point: Telegram refusing the token is a fact about the
bot, Telegram not answering at all is a fact about Telegram, and confusing them
would either send a working project to a human or hide a dead one behind a
retry.
"""

from unittest.mock import patch

import httpx
import pytest

from shared.contracts.dto.telegram import BotLivenessState
from src.utils.telegram_token import bot_liveness

TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"  # noqa: S105

# Captured before patching: the factory below must not call the patched name.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _patched_telegram(handler):
    def factory(*args, **kwargs):
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler))

    return patch("src.utils.telegram_token.httpx.AsyncClient", factory)


@pytest.mark.asyncio
async def test_a_live_bot_answers_with_its_username():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/getMe")
        assert TOKEN in str(request.url)
        return httpx.Response(200, json={"ok": True, "result": {"username": "palindrome_bot"}})

    with _patched_telegram(handler):
        liveness = await bot_liveness(TOKEN)

    assert liveness.state is BotLivenessState.ALIVE
    assert liveness.bot_username == "palindrome_bot"
    assert TOKEN not in liveness.detail


@pytest.mark.asyncio
async def test_a_token_telegram_refuses_is_a_dead_bot_not_an_outage():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    with _patched_telegram(handler):
        liveness = await bot_liveness(TOKEN)

    assert liveness.state is BotLivenessState.NOT_LIVE
    assert "Unauthorized" in liveness.detail
    assert liveness.bot_username is None
    assert TOKEN not in liveness.detail


@pytest.mark.asyncio
async def test_an_accepted_token_with_no_username_is_not_a_usable_bot():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"id": 42}})

    with _patched_telegram(handler):
        liveness = await bot_liveness(TOKEN)

    assert liveness.state is BotLivenessState.NOT_LIVE


@pytest.mark.asyncio
async def test_telegram_not_answering_is_reported_as_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with _patched_telegram(handler):
        liveness = await bot_liveness(TOKEN)

    assert liveness.state is BotLivenessState.TELEGRAM_UNREACHABLE
    assert "connection refused" in liveness.detail


@pytest.mark.asyncio
async def test_a_telegram_server_error_is_unreachable_not_a_dead_bot():
    """5xx is Telegram being unwell; the bot's own credential is not implicated."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"ok": False, "description": "Bad Gateway"})

    with _patched_telegram(handler):
        liveness = await bot_liveness(TOKEN)

    assert liveness.state is BotLivenessState.TELEGRAM_UNREACHABLE


@pytest.mark.asyncio
async def test_a_body_that_is_not_json_is_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>proxy error</html>")

    with _patched_telegram(handler):
        liveness = await bot_liveness(TOKEN)

    assert liveness.state is BotLivenessState.TELEGRAM_UNREACHABLE
