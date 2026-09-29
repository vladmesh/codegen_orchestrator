"""Terminal repair failures owe both audiences instead of a bare human-review move."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import uuid

import pytest

from shared.contracts.dto.pr_conflict_repair import PRConflictRepairEvidence, repair_task_id


@pytest.mark.asyncio
async def test_terminal_repair_stop_uses_immutable_admission_and_reuses_committed_reason():
    from shared.pr_conflict_repair import stop_failed_pr_repair

    cycle = datetime.now(UTC)
    pid = uuid.uuid4()
    tid = repair_task_id("story-1", cycle)
    evidence = PRConflictRepairEvidence(
        project_id=pid,
        story_id="story-1",
        pr_number=3,
        cycle_started_at=cycle,
        repository_id="repo-1",
        head_sha="a" * 40,
        default_branch="trunk",
        default_sha="b" * 40,
        max_iterations=3,
    )
    api = AsyncMock()
    story = SimpleNamespace(
        id="story-1",
        project_id=pid,
        pr_number=3,
        reopened_at=cycle,
        created_at=cycle,
        status="in_progress",
        quarantine_reason=None,
    )
    api.get_story.return_value = story
    response = api.request.return_value
    response.json = lambda: [{"details": {"pr_conflict_repair": evidence.model_dump(mode="json")}}]
    assert await stop_failed_pr_repair(api, "story-1", tid, "eng-1", "repair failed", "scheduler")
    args, kwargs = api.stop_story.await_args
    assert args[:2] == ("story-1", "human-review")
    failure = args[2]
    assert failure.code.value == "pr_conflict_repair_exhausted"
    assert "PR #3" in failure.detail and tid in failure.detail and "ceiling 3" in failure.detail
    story.status = "waiting_human_review"
    story.quarantine_reason = failure.model_dump(mode="json")
    from shared.contracts.dto.story_failure import (
        story_failure_admin_text,
        story_failure_owner_text,
    )

    api.get_story_owner_notification.return_value = SimpleNamespace(
        text=story_failure_owner_text(failure),
        admin_text=story_failure_admin_text("story-1", str(pid), failure),
        state="owed",
        admin_state="owed",
    )
    assert await stop_failed_pr_repair(api, "story-1", tid, "eng-1", "repair failed", "scheduler")
    assert api.stop_story.await_count == 1


@pytest.mark.asyncio
async def test_old_cycle_task_cannot_stop_current_work():
    from shared.pr_conflict_repair import stop_failed_pr_repair

    api = AsyncMock()
    cycle = datetime.now(UTC)
    api.get_story.return_value = SimpleNamespace(id="story-1", reopened_at=cycle, created_at=cycle)
    assert not await stop_failed_pr_repair(
        api, "story-1", "pr-conflict-old", "eng-1", "old failure", "scheduler"
    )
    api.stop_story.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_failure_retains_failed_task_for_next_supervisor_visit():
    from _run_routing_factories import _make_task

    from src.tasks.supervisor.liveness import supervise_failed_tasks

    api = AsyncMock()
    task = _make_task(
        id="pr-conflict-terminal", story_id="story-1", status="failed", current_iteration=3
    )
    api.get_tasks_by_status.return_value = [task]
    api.list_runs.return_value = []
    stop = AsyncMock(side_effect=OSError("API response lost"))
    with patch("src.tasks.supervisor.liveness.stop_failed_pr_repair", stop):
        assert await supervise_failed_tasks(api, AsyncMock()) == {"retried": 0, "escalated": 0}
        api.transition_task.assert_not_awaited()
        api.transition_story.assert_not_awaited()
        stop.side_effect = None
        stop.return_value = True
        assert await supervise_failed_tasks(api, AsyncMock()) == {"retried": 0, "escalated": 1}
    api.transition_task.assert_awaited_once_with(task.id, "waiting_human_review", "supervisor")
    api.transition_story.assert_not_awaited()
