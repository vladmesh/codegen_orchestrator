"""A no-commits PR refusal must owe notices through the native stop action."""

from unittest.mock import AsyncMock

from _run_routing_factories import _make_run, _make_task
import pytest
import structlog

from shared.contracts.dto.run_result import EngineeringRunResult
from src.tasks.story_completion import _park_story_without_commits
from src.tasks.supervisor import supervise_failed_tasks


@pytest.mark.asyncio
async def test_exhausted_story_chooses_empty_reason_before_any_bare_stop():
    api = AsyncMock()
    ordinary = _make_task(id="ordinary", story_id="story-1", status="failed", current_iteration=3)
    empty = _make_task(id="empty", story_id="story-1", status="failed", current_iteration=3)
    api.get_tasks_by_status.return_value = [ordinary, empty]
    empty_run = _make_run(
        id="empty-run",
        type="engineering",
        status="failed",
        result=EngineeringRunResult(engineering_status="failed", failure_reason="no_new_commit"),
    )
    api.list_runs.side_effect = lambda *, task_id, **kwargs: (
        [empty_run] if task_id == empty.id else []
    )
    await supervise_failed_tasks(api, AsyncMock())
    api.stop_story.assert_awaited_once()
    api.transition_story.assert_not_awaited()
    assert api.transition_task.await_count == 2


@pytest.mark.asyncio
async def test_no_commits_uses_a_typed_stop_without_a_reason_patch():
    api = AsyncMock()
    await _park_story_without_commits(
        api,
        "story-1",
        "story/story-1",
        "No commits between main and story/story-1",
        structlog.get_logger(),
    )
    api.stop_story.assert_awaited_once()
    assert api.stop_story.await_args.args[:2] == ("story-1", "human-review")
    failure = api.stop_story.await_args.args[2]
    assert failure.code.value == "no_new_commit"
    assert "No commits between" in failure.detail
    api.update_story.assert_not_awaited()
    api.transition_story.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("iteration", [2, 3])
async def test_empty_planned_attempt_keeps_its_budget_and_owes_a_reason_only_at_exhaustion(
    iteration,
):
    api = AsyncMock()
    task = _make_task(
        story_id="story-1", status="failed", current_iteration=iteration, max_iterations=3
    )
    api.get_tasks_by_status.return_value = [task]
    api.list_runs.return_value = [
        _make_run(
            id="eng-1",
            type="engineering",
            status="failed",
            result=EngineeringRunResult(
                engineering_status="failed", failure_reason="no_new_commit"
            ),
        )
    ]
    result = await supervise_failed_tasks(api, AsyncMock())
    if iteration < 3:
        assert result == {"retried": 1, "escalated": 0}
        api.retry_failed_task.assert_awaited_once_with(task.id, "supervisor")
        api.update_task.assert_not_awaited()
        api.stop_story.assert_not_awaited()
    else:
        assert result == {"retried": 0, "escalated": 1}
        api.stop_story.assert_awaited_once()
        assert api.stop_story.await_args.args[2].code.value == "no_new_commit"
        api.transition_story.assert_not_awaited()
        api.update_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_refused_empty_task_escalation_leaves_the_failed_task_selected():
    api = AsyncMock()
    api.get_tasks_by_status.return_value = [
        _make_task(story_id="story-1", status="failed", current_iteration=3, max_iterations=3)
    ]
    api.list_runs.return_value = [
        _make_run(
            id="eng-1",
            type="engineering",
            status="failed",
            result=EngineeringRunResult(
                engineering_status="failed", failure_reason="no_new_commit"
            ),
        )
    ]
    api.stop_story.side_effect = RuntimeError("API refused")
    assert await supervise_failed_tasks(api, AsyncMock()) == {"retried": 0, "escalated": 0}
    api.transition_task.assert_not_awaited()
