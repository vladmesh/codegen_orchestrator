"""Tests for the user-facing engineering budget balance command."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

os.environ.setdefault("API_BASE_URL", "http://localhost:8000")
os.environ.setdefault("INTERNAL_API_KEY", "test-internal-key")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-for-unit-tests")
os.environ.setdefault("LK_DOMAIN", "https://lk.test.example.com")


def _update(telegram_id: int = 42) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = telegram_id
    update.message.reply_text = AsyncMock()
    return update


@pytest.mark.asyncio
async def test_balance_shows_exact_spend_and_available_without_internal_hold_split():
    from src.main import balance

    update = _update()
    payload = {
        "enforcement": "enforced",
        "known_spend_microusd": 12_340_001,
        "active_held_microusd": 3_000_000,
        "unknown_final_held_microusd": 2_000_000,
        "remaining_microusd": 32_659_999,
        "exhausted": False,
        "unknown_cost_attempt_count": 0,
        "incomplete_coverage": False,
    }

    with patch("src.main.api_client") as mock_api:
        mock_api.get_json = AsyncMock(return_value=payload)
        await balance(update, MagicMock())

    mock_api.get_json.assert_awaited_once_with(
        "engineering-budget-policy/balance",
        headers={"X-Telegram-ID": "42"},
    )
    text = update.message.reply_text.await_args.args[0]
    assert "$12.340001" in text
    assert "$32.659999" in text
    assert "$3.00" not in text
    assert "$2.00" not in text
    assert "удерж" not in text.lower()


@pytest.mark.asyncio
async def test_balance_warns_when_cost_coverage_is_incomplete():
    from src.main import balance

    update = _update()
    payload = {
        "enforcement": "enforced",
        "known_spend_microusd": 10_000_000,
        "remaining_microusd": 35_000_000,
        "exhausted": False,
        "unknown_cost_attempt_count": 2,
        "incomplete_coverage": True,
    }

    with patch("src.main.api_client") as mock_api:
        mock_api.get_json = AsyncMock(return_value=payload)
        await balance(update, MagicMock())

    text = update.message.reply_text.await_args.args[0]
    assert "2" in text
    assert "неизвестной стоимостью" in text.lower()


@pytest.mark.asyncio
async def test_balance_explains_unlimited_policy():
    from src.main import balance

    update = _update()
    payload = {
        "enforcement": "unlimited",
        "known_spend_microusd": 1_500_000,
        "remaining_microusd": None,
        "exhausted": False,
        "unknown_cost_attempt_count": 0,
        "incomplete_coverage": False,
    }

    with patch("src.main.api_client") as mock_api:
        mock_api.get_json = AsyncMock(return_value=payload)
        await balance(update, MagicMock())

    text = update.message.reply_text.await_args.args[0]
    assert "$1.50" in text
    assert "лимит не установлен" in text.lower()


@pytest.mark.asyncio
async def test_balance_reports_temporary_api_failure_without_leaking_details():
    from src.main import balance

    update = _update()
    request = httpx.Request("GET", "http://api/api/engineering-budget-policy/balance")
    error = httpx.ConnectError("private upstream detail", request=request)

    with patch("src.main.api_client") as mock_api:
        mock_api.get_json = AsyncMock(side_effect=error)
        await balance(update, MagicMock())

    text = update.message.reply_text.await_args.args[0]
    assert "попробуйте позже" in text.lower()
    assert "private upstream detail" not in text
