"""Dirty PRs enter atomic repair admission, retaining ordinary merge handling."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
import structlog

from src.tasks.pr_poller import _merge_open_pr


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["admitted", "reused", "exhausted"])
async def test_dirty_uses_atomic_api_without_merge_or_separate_task_creation(outcome):
    api, github, redis = AsyncMock(), AsyncMock(), AsyncMock()
    project = str(uuid.uuid4())
    cycle = datetime.now(UTC)
    api.get_story.return_value = SimpleNamespace(
        project_id=project, reopened_at=cycle, created_at=cycle, pr_number=3
    )
    api.repair_story_pr_conflicts.return_value = SimpleNamespace(
        outcome=outcome, task_id="repair-1"
    )
    result = await _merge_open_pr(
        api,
        github,
        redis,
        story_id="story-1",
        project_id=project,
        owner="synthetic",
        repo_name="product",
        pull_request={
            "number": 3,
            "state": "open",
            "mergeable_state": "dirty",
            "head": {"sha": "a" * 40},
        },
        log=structlog.get_logger(),
    )
    assert result is None
    (sid, command), _ = api.repair_story_pr_conflicts.await_args
    assert sid == "story-1" and command.pr_number == 3
    assert command.expected_head_sha == "a" * 40 and command.cycle_started_at == cycle
    api.create_task.assert_not_awaited()
    api.transition_story.assert_not_awaited()
    api.update_story.assert_not_awaited()
    github.merge_pull_request.assert_not_awaited()
    github.update_pull_request_branch.assert_not_awaited()


@pytest.mark.asyncio
async def test_repair_admission_failure_stays_visible_and_does_not_park():
    api, github, redis = AsyncMock(), AsyncMock(), AsyncMock()
    project = str(uuid.uuid4())
    api.get_story.return_value = SimpleNamespace(
        project_id=project, reopened_at=None, created_at=datetime.now(UTC), pr_number=3
    )
    api.repair_story_pr_conflicts.side_effect = RuntimeError("admission interrupted")
    with pytest.raises(RuntimeError, match="admission interrupted"):
        await _merge_open_pr(
            api,
            github,
            redis,
            story_id="story-1",
            project_id=project,
            owner="synthetic",
            repo_name="product",
            pull_request={
                "number": 3,
                "state": "open",
                "mergeable_state": "dirty",
                "head": {"sha": "a" * 40},
            },
            log=structlog.get_logger(),
        )
    api.transition_story.assert_not_awaited()
