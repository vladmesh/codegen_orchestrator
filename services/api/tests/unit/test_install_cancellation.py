"""Cancellation releases queued install ownership; a running writer still owes settlement."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.routers._task_helpers import apply_cancellation


@pytest.mark.asyncio
async def test_cancel_queued_install_releases_its_non_engineering_operation():
    task = SimpleNamespace(
        type="install",
        status="todo",
        install_operation={
            "id": "operation-1",
            "project_id": "00000000-0000-0000-0000-000000000001",
            "task_id": "task-1",
            "story_id": "story-1",
            "repository_id": "repo-1",
            "cycle_started_at": datetime.now(UTC).isoformat(),
            "state": "queued",
            "stage": "queued",
        },
    )
    with patch("src.routers._task_helpers.create_status_event", AsyncMock()):
        await apply_cancellation(task, AsyncMock())
    assert task.status == "cancelled"
    assert task.install_operation["state"] == "refused"
    assert task.install_operation["stage"] == "cancelled"
