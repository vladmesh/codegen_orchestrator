"""Tests for the shared typed Run client boundary."""

from unittest.mock import AsyncMock

from pydantic import ValidationError
import pytest

from shared.clients.run_api import RunAPIClientMixin
from shared.contracts.dto.run import RunStatus, RunUpdate


class _RunClient(RunAPIClientMixin):
    def __init__(self) -> None:
        self.request = AsyncMock()


def test_unknown_run_update_field_is_rejected():
    with pytest.raises(ValidationError, match="extra_forbidden"):
        RunUpdate.model_validate({"statuz": "failed"})


@pytest.mark.asyncio
async def test_update_run_validates_and_serializes_the_shared_contract():
    client = _RunClient()

    await client.update_run("run-1", {"status": RunStatus.FAILED, "error_message": "boom"})

    client.request.assert_awaited_once_with(
        "PATCH",
        "runs/run-1",
        json={"status": "failed", "error_message": "boom"},
    )


@pytest.mark.asyncio
async def test_update_run_refuses_unknown_dict_fields_before_http():
    client = _RunClient()

    with pytest.raises(ValidationError, match="extra_forbidden"):
        await client.update_run("run-1", {"statuz": "failed"})

    client.request.assert_not_awaited()
