"""Tests for the shared typed Run client boundary."""

from unittest.mock import AsyncMock

from pydantic import ValidationError
import pytest

from shared.clients.run_api import RunAPIClientMixin
from shared.contracts.dto.run import RunStatus, RunUpdate


class _RunClient(RunAPIClientMixin):
    def __init__(self) -> None:
        self.request = AsyncMock()


def test_explicit_null_run_status_is_rejected():
    with pytest.raises(ValidationError, match="status may be omitted but must not be null"):
        RunUpdate.model_validate({"status": None})


@pytest.mark.asyncio
async def test_update_run_validates_and_serializes_the_shared_contract():
    client = _RunClient()

    await client.update_run("run-1", {"status": RunStatus.FAILED, "error_message": "boom"})

    client.request.assert_awaited_once_with(
        "PATCH",
        "runs/run-1",
        json={"status": "failed", "error_message": "boom"},
    )
