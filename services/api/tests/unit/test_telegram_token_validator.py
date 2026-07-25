"""The validation chain itself — what verdict each kind of token produces."""

from unittest.mock import patch

import httpx
import pytest

from shared.contracts.dto.telegram import (
    TokenCheckName,
    TokenRejectionReason,
    TokenVerdictStatus,
)
from src.utils.telegram_token import looks_like_bot_token, validate_telegram_token

VALID_TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"  # noqa: S105


# Captured before patching: the factory below must not call the patched name.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _patched_telegram(handler):
    """Route the validator's getMe call through a MockTransport."""

    def factory(*args, **kwargs):
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler))

    return patch("src.utils.telegram_token.httpx.AsyncClient", factory)


@pytest.mark.asyncio
async def test_valid_token_yields_ok_verdict_with_username():
    def handler(request: httpx.Request) -> httpx.Response:
        assert VALID_TOKEN in str(request.url)
        return httpx.Response(200, json={"ok": True, "result": {"username": "palindrome_bot"}})

    with _patched_telegram(handler):
        verdict = await validate_telegram_token(VALID_TOKEN)

    assert verdict.status == TokenVerdictStatus.OK
    assert verdict.reason_code is None
    assert verdict.bot_username == "palindrome_bot"
    assert "palindrome_bot" in verdict.user_message
    assert [c.name for c in verdict.checks] == [
        TokenCheckName.FORMAT,
        TokenCheckName.TELEGRAM_GET_ME,
    ]
    assert all(c.passed for c in verdict.checks)


@pytest.mark.asyncio
async def test_token_telegram_rejects_is_rejected_with_reason():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    with _patched_telegram(handler):
        verdict = await validate_telegram_token(VALID_TOKEN)

    assert verdict.status == TokenVerdictStatus.REJECTED
    assert verdict.reason_code == TokenRejectionReason.INVALID_TOKEN
    assert verdict.bot_username is None
    assert "Unauthorized" in verdict.user_message
    assert VALID_TOKEN not in verdict.user_message


@pytest.mark.asyncio
async def test_malformed_token_is_rejected_without_calling_telegram():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Telegram must not be called for a malformed token")

    with _patched_telegram(handler):
        verdict = await validate_telegram_token("not-a-token")

    assert verdict.status == TokenVerdictStatus.REJECTED
    assert verdict.reason_code == TokenRejectionReason.MALFORMED
    assert [c.name for c in verdict.checks] == [TokenCheckName.FORMAT]


@pytest.mark.asyncio
async def test_getme_without_username_is_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"id": 42}})

    with _patched_telegram(handler):
        verdict = await validate_telegram_token(VALID_TOKEN)

    assert verdict.status == TokenVerdictStatus.REJECTED
    assert verdict.reason_code == TokenRejectionReason.NO_USERNAME


@pytest.mark.asyncio
async def test_unreachable_telegram_is_its_own_reason_code():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with _patched_telegram(handler):
        verdict = await validate_telegram_token(VALID_TOKEN)

    assert verdict.status == TokenVerdictStatus.REJECTED
    assert verdict.reason_code == TokenRejectionReason.TELEGRAM_UNREACHABLE


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (VALID_TOKEN, True),
        (f"  {VALID_TOKEN}  ", True),
        ("123456789:short", False),
        ("sk-proj-abcdefghijklmnopqrstuvwxyz0123456789", False),
        ("42", False),
    ],
)
def test_looks_like_bot_token(value, expected):
    assert looks_like_bot_token(value) is expected
