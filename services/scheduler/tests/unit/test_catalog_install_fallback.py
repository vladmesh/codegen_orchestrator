"""Mechanical refusals cannot purchase an automatic coding repair."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.contracts.dto.task import TaskStatus, TaskType
from src.tasks.catalog_install import refuse_install_coding_fallback


@pytest.mark.asyncio
async def test_install_failure_parks_only_its_story_without_a_paid_call():
    api = AsyncMock()
    api.get_tasks_by_story.return_value = [
        SimpleNamespace(type=TaskType.INSTALL, status=TaskStatus.DONE)
    ]
    assert await refuse_install_coding_fallback(api, "story-1", "PR conflict")
    api.stop_story.assert_awaited_once()
    assert api.stop_story.call_args.args[:2] == ("story-1", "human-review")
    api.start_paid_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_ordinary_feature_repairs_keep_their_existing_owner():
    api = AsyncMock()
    api.get_tasks_by_story.return_value = [
        SimpleNamespace(type=TaskType.FEATURE, status=TaskStatus.DONE)
    ]
    assert not await refuse_install_coding_fallback(api, "story-1", "CI failure")
    api.stop_story.assert_not_awaited()
