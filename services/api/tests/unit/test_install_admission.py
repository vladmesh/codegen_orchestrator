"""INSTALL must refuse on rung one before any paid work can be selected."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.contracts.dto.engineering_dispatch import EngineeringDispatchCommand
from src import engineering_dispatch_admission as admission


@pytest.mark.asyncio
async def test_install_is_never_an_engineering_dispatch(monkeypatch):
    task = SimpleNamespace(id="install-1", type="install", status="todo", dispatch_admitted=True)
    monkeypatch.setattr(
        admission, "_lock_dispatch_tasks", AsyncMock(return_value=(task, {}, None, None))
    )
    paid = AsyncMock()
    monkeypatch.setattr(admission, "start_paid_run", paid)
    result = await admission.admit_engineering_dispatch(
        EngineeringDispatchCommand(task_id=task.id), object()
    )
    assert result.outcome == "refused"
    assert result.reason == "catalog_install_not_engineering"
    paid.assert_not_awaited()
