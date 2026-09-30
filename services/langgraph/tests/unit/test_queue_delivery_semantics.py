"""Queue delivery behavior at the LangGraph consumer boundary."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pydantic import BaseModel, ValidationError
import pytest

from src.consumers import _base


class _RequiredMessage(BaseModel):
    value: str


def _validation_error() -> ValidationError:
    try:
        _RequiredMessage.model_validate({})
    except ValidationError as exc:
        return exc
    raise AssertionError("expected validation failure")


@pytest.mark.asyncio
async def test_terminal_validation_is_quarantined_before_ack():
    msg = SimpleNamespace(message_id="1-0", data={})
    redis = AsyncMock()
    redis.reject_entry = AsyncMock()
    redis.reject_if_exhausted = AsyncMock(return_value=False)

    with patch.object(
        _base,
        "execute_live_work",
        new=AsyncMock(side_effect=_base.TerminalMessageValidationError(_validation_error())),
    ):
        await _base._process_entry(msg, redis, "architect:queue", "g", "architect", AsyncMock())

    redis.reject_entry.assert_awaited_once()
    redis.ack.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeated_processing_failure_stops_at_delivery_ceiling():
    msg = SimpleNamespace(message_id="2-0", data={"project_id": "p1"})
    redis = AsyncMock()
    redis.reject_if_exhausted = AsyncMock(return_value=True)

    with (
        patch.object(_base, "_check_message_staleness", new=AsyncMock(return_value=False)),
        patch.object(_base, "execute_live_work", new=AsyncMock(side_effect=RuntimeError("boom"))),
    ):
        await _base._process_entry(msg, redis, "architect:queue", "g", "architect", AsyncMock())

    redis.reject_if_exhausted.assert_awaited_once()
    redis.ack.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_propagates_without_spending_delivery_budget():
    msg = SimpleNamespace(message_id="3-0", data={})
    redis = AsyncMock()
    redis.reject_if_exhausted = AsyncMock(return_value=False)

    with (
        patch.object(_base, "_check_message_staleness", new=AsyncMock(return_value=False)),
        patch.object(_base, "execute_live_work", new=AsyncMock(side_effect=asyncio.CancelledError())),
        pytest.raises(asyncio.CancelledError),
    ):
        await _base._process_entry(msg, redis, "architect:queue", "g", "architect", AsyncMock())

    redis.reject_if_exhausted.assert_not_awaited()
    redis.reject_entry.assert_not_awaited()
