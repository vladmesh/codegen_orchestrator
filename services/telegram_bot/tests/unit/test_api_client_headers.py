"""The bot's calls to the internal API carry the same headers as every other service's.

`TelegramAPIClient` used to be the one client that sent no `X-Internal-Key` at
all. It takes its transport from `shared/clients/internal_api.py` now, so the
header cannot go missing without that module changing.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from shared.log_config.correlation import clear_context, set_correlation_id
from src.clients.api import TELEGRAM_TIMEOUT_SECONDS, TelegramAPIClient

INTERNAL_KEY = "telegram-bot-test-key"


@pytest.fixture
def sent_requests(monkeypatch) -> list[httpx.Request]:
    """Record what the client puts on the wire instead of dialling the API."""
    captured: list[httpx.Request] = []
    real_async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    def factory(**kwargs):
        return real_async_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("shared.clients.internal_api.httpx.AsyncClient", factory)
    monkeypatch.setenv("INTERNAL_API_KEY", INTERNAL_KEY)
    return captured


@pytest.fixture
def client(sent_requests) -> TelegramAPIClient:
    with patch("src.clients.api.get_settings") as mock_settings:
        mock_settings.return_value.api_base_url = "http://api:8000"
        return TelegramAPIClient()


@pytest.fixture(autouse=True)
def _clean_correlation_context():
    clear_context()
    yield
    clear_context()


@pytest.mark.asyncio
async def test_get_json_sends_both_internal_headers(client, sent_requests):
    set_correlation_id("corr-7")

    await client.get_json("users/by-telegram/1", headers={"X-Telegram-ID": "1"})

    sent = sent_requests[-1]
    assert sent.headers["X-Internal-Key"] == INTERNAL_KEY
    assert sent.headers["X-Correlation-ID"] == "corr-7"
    assert sent.headers["X-Telegram-ID"] == "1"
    assert sent.url.path == "/api/users/by-telegram/1"


@pytest.mark.asyncio
async def test_post_json_sends_both_internal_headers(client, sent_requests):
    set_correlation_id("corr-8")

    await client.post_json("users/upsert", json={"telegram_id": 1})

    sent = sent_requests[-1]
    assert sent.headers["X-Internal-Key"] == INTERNAL_KEY
    assert sent.headers["X-Correlation-ID"] == "corr-8"
    assert sent.url.path == "/api/users/upsert"


@pytest.mark.asyncio
async def test_the_bot_keeps_its_shorter_timeout(client):
    """A user waits on these calls, so the bot gives the API less time than the services do."""
    assert (await client._get_client()).timeout.read == TELEGRAM_TIMEOUT_SECONDS
