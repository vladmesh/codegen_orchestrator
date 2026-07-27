"""The validation chain itself — what verdict each kind of token produces.

Scope here is the remote layers, so the uniqueness lookup runs against a database
where nobody holds the bot. The uniqueness layer itself is covered against a real
database in tests/service/test_telegram_token_uniqueness.py.
"""

from unittest.mock import patch
import uuid

import httpx
import pytest

from shared.contracts.dto.telegram import (
    TokenCheckName,
    TokenRejectionReason,
    TokenVerdictStatus,
)
from shared.models import Project
from src.utils.telegram_token import looks_like_bot_token, validate_telegram_token

VALID_TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"  # noqa: S105


class _FreeDatabase:
    """A database in which no project holds the bot."""

    async def execute(self, query):
        return _EmptyResult()


class _EmptyResult:
    def all(self):
        return []


def _project() -> Project:
    return Project(id=uuid.uuid4(), title="Palindrome", owner_id=1)


# Captured before patching: the factory below must not call the patched name.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _patched_telegram(handler):
    """Route the validator's Telegram calls through a MockTransport."""

    def factory(*args, **kwargs):
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler))

    return patch("src.utils.telegram_token.httpx.AsyncClient", factory)


def _fake_telegram(*, getme=None, webhook_url="", webhook=None, get_updates=None):
    """A Telegram that answers a healthy getMe and a quiet token, unless overridden."""

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "getMe":
            if getme is not None:
                return getme(request)
            return httpx.Response(200, json={"ok": True, "result": {"username": "palindrome_bot"}})
        if method == "getWebhookInfo":
            if webhook is not None:
                return webhook(request)
            return httpx.Response(200, json={"ok": True, "result": {"url": webhook_url}})
        if method == "getUpdates":
            if get_updates is not None:
                return get_updates(request)
            return httpx.Response(200, json={"ok": True, "result": []})
        raise AssertionError(f"Unexpected Telegram method: {method}")

    return handler


@pytest.mark.asyncio
async def test_valid_token_yields_ok_verdict_with_username():
    def getme(request: httpx.Request) -> httpx.Response:
        assert VALID_TOKEN in str(request.url)
        return httpx.Response(200, json={"ok": True, "result": {"username": "palindrome_bot"}})

    with _patched_telegram(_fake_telegram(getme=getme)):
        verdict = await validate_telegram_token(VALID_TOKEN, db=_FreeDatabase(), project=_project())

    assert verdict.status == TokenVerdictStatus.OK
    assert verdict.reason_code is None
    assert verdict.bot_username == "palindrome_bot"
    assert "palindrome_bot" in verdict.user_message
    assert [c.name for c in verdict.checks] == [
        TokenCheckName.FORMAT,
        TokenCheckName.TELEGRAM_GET_ME,
        TokenCheckName.TELEGRAM_WEBHOOK,
        TokenCheckName.TELEGRAM_POLLER,
        TokenCheckName.PROJECT_UNIQUENESS,
    ]
    assert all(c.passed for c in verdict.checks)


@pytest.mark.asyncio
async def test_token_telegram_rejects_is_rejected_with_reason():
    def getme(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    with _patched_telegram(_fake_telegram(getme=getme)):
        verdict = await validate_telegram_token(VALID_TOKEN, db=_FreeDatabase(), project=_project())

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
        verdict = await validate_telegram_token(
            "not-a-token", db=_FreeDatabase(), project=_project()
        )

    assert verdict.status == TokenVerdictStatus.REJECTED
    assert verdict.reason_code == TokenRejectionReason.MALFORMED
    assert [c.name for c in verdict.checks] == [TokenCheckName.FORMAT]


@pytest.mark.asyncio
async def test_getme_without_username_is_rejected():
    def getme(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"id": 42}})

    with _patched_telegram(_fake_telegram(getme=getme)):
        verdict = await validate_telegram_token(VALID_TOKEN, db=_FreeDatabase(), project=_project())

    assert verdict.status == TokenVerdictStatus.REJECTED
    assert verdict.reason_code == TokenRejectionReason.NO_USERNAME


@pytest.mark.asyncio
async def test_unreachable_telegram_is_its_own_reason_code():
    def getme(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with _patched_telegram(_fake_telegram(getme=getme)):
        verdict = await validate_telegram_token(VALID_TOKEN, db=_FreeDatabase(), project=_project())

    assert verdict.status == TokenVerdictStatus.REJECTED
    assert verdict.reason_code == TokenRejectionReason.TELEGRAM_UNREACHABLE


@pytest.mark.asyncio
async def test_token_with_a_webhook_is_rejected_without_probing_getupdates():
    def get_updates(request: httpx.Request) -> httpx.Response:
        raise AssertionError("getUpdates must not run once a webhook is found")

    handler = _fake_telegram(
        webhook_url="https://someones-bot.example/hook", get_updates=get_updates
    )
    with _patched_telegram(handler):
        verdict = await validate_telegram_token(VALID_TOKEN, db=_FreeDatabase(), project=_project())

    assert verdict.status == TokenVerdictStatus.REJECTED
    assert verdict.reason_code == TokenRejectionReason.WEBHOOK_ACTIVE
    assert [c.name for c in verdict.checks] == [
        TokenCheckName.FORMAT,
        TokenCheckName.TELEGRAM_GET_ME,
        TokenCheckName.TELEGRAM_WEBHOOK,
    ]
    # The message stays generic: no url, no guess about whose bot it is.
    assert "someones-bot.example" not in verdict.user_message
    assert VALID_TOKEN not in verdict.user_message


@pytest.mark.asyncio
async def test_getupdates_conflict_means_an_active_poller():
    def get_updates(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "ok": False,
                "error_code": 409,
                "description": (
                    "Conflict: terminated by other getUpdates request; "
                    "make sure that only one bot instance is running"
                ),
            },
        )

    with _patched_telegram(_fake_telegram(get_updates=get_updates)):
        verdict = await validate_telegram_token(VALID_TOKEN, db=_FreeDatabase(), project=_project())

    assert verdict.status == TokenVerdictStatus.REJECTED
    assert verdict.reason_code == TokenRejectionReason.POLLER_ACTIVE
    assert [c.name for c in verdict.checks] == [
        TokenCheckName.FORMAT,
        TokenCheckName.TELEGRAM_GET_ME,
        TokenCheckName.TELEGRAM_WEBHOOK,
        TokenCheckName.TELEGRAM_POLLER,
    ]
    assert VALID_TOKEN not in verdict.user_message


@pytest.mark.asyncio
async def test_poller_probe_neither_confirms_nor_consumes_updates():
    seen = {}

    def get_updates(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(
            200,
            json={"ok": True, "result": [{"update_id": 700, "message": {"text": "hi"}}]},
        )

    with _patched_telegram(_fake_telegram(get_updates=get_updates)):
        verdict = await validate_telegram_token(VALID_TOKEN, db=_FreeDatabase(), project=_project())

    assert verdict.status == TokenVerdictStatus.OK
    # No offset: a higher one acks updates, a negative one makes earlier ones forgotten.
    # timeout=0 keeps us out of another bot's long poll.
    assert "offset" not in seen
    assert seen["timeout"] == "0"


@pytest.mark.asyncio
async def test_odd_getupdates_answer_does_not_refuse_the_token():
    def get_updates(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"ok": False, "description": "Too Many Requests"})

    with _patched_telegram(_fake_telegram(get_updates=get_updates)):
        verdict = await validate_telegram_token(VALID_TOKEN, db=_FreeDatabase(), project=_project())

    assert verdict.status == TokenVerdictStatus.OK


@pytest.mark.asyncio
async def test_unreachable_webhook_probe_does_not_pass_the_token():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"username": "palindrome_bot"}})
        raise httpx.ConnectError("connection refused")

    with _patched_telegram(handler):
        verdict = await validate_telegram_token(VALID_TOKEN, db=_FreeDatabase(), project=_project())

    assert verdict.status == TokenVerdictStatus.REJECTED
    assert verdict.reason_code == TokenRejectionReason.TELEGRAM_UNREACHABLE


@pytest.mark.asyncio
async def test_rate_limited_webhook_probe_is_unreachable_not_a_crash():
    def webhook(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"ok": False, "description": "Too Many Requests"})

    def get_updates(request: httpx.Request) -> httpx.Response:
        raise AssertionError("the chain must stop once the webhook probe fails")

    with _patched_telegram(_fake_telegram(webhook=webhook, get_updates=get_updates)):
        verdict = await validate_telegram_token(VALID_TOKEN, db=_FreeDatabase(), project=_project())

    assert verdict.status == TokenVerdictStatus.REJECTED
    assert verdict.reason_code == TokenRejectionReason.TELEGRAM_UNREACHABLE
    assert verdict.checks[-1].name == TokenCheckName.TELEGRAM_WEBHOOK


@pytest.mark.asyncio
async def test_unparseable_webhook_answer_is_unreachable_not_a_crash():
    def webhook(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>bad gateway</html>")

    with _patched_telegram(_fake_telegram(webhook=webhook)):
        verdict = await validate_telegram_token(VALID_TOKEN, db=_FreeDatabase(), project=_project())

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
