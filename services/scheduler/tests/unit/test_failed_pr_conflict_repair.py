"""Client reads never authorize repair settlement; send immutable identity."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import uuid

import pytest

from shared.contracts.dto.pr_conflict_repair import (
    PRConflictRepairAttemptDisposition,
    PRConflictRepairAttemptRead,
    PRConflictRepairEvidence,
    repair_task_id,
)
from shared.pr_conflict_repair import settle_pr_repair_attempt


def admitted_api(outcome="exhausted"):
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
    ending = {
        "outcome": outcome,
        "task_id": tid,
        "attempt_id": "eng-1",
        "current_iteration": 3,
        "task_status": "waiting_human_review" if outcome == "exhausted" else "failed",
        "story_status": "waiting_human_review" if outcome == "exhausted" else "in_progress",
    }
    api = AsyncMock()
    api.get_run.return_value = SimpleNamespace(run_metadata={"iteration": 3})

    def response(method, path, **kwargs):
        body = (
            [{"details": {"pr_conflict_repair": evidence.model_dump(mode="json")}}]
            if method == "GET"
            else ending
        )
        return Mock(json=lambda: body)

    api.request.side_effect = response
    return api, tid, evidence, ending


@pytest.mark.asyncio
async def test_terminal_repair_stop_uses_immutable_admission_and_reuses_committed_reason():
    api, tid, evidence, _ = admitted_api()
    for _ in range(2):
        result = await settle_pr_repair_attempt(
            api,
            "story-1",
            tid,
            "eng-1",
            "repair failed",
            PRConflictRepairAttemptDisposition.FAILED,
        )
        assert result.outcome.value == "exhausted"
    api.stop_story.assert_not_awaited()
    api.get_story.assert_not_awaited()
    posts = [c for c in api.request.await_args_list if c.args[0] == "POST"]
    assert len(posts) == 2 and posts[0] == posts[1]
    command = posts[0].kwargs["json"]
    assert command["cycle_started_at"] == evidence.model_dump(mode="json")["cycle_started_at"]
    assert command["task_id"] == tid and command["attempt_id"] == "eng-1"
    assert command["pr_number"] == 3 and command["expected_iteration"] == 3


@pytest.mark.asyncio
async def test_old_cycle_task_cannot_stop_current_work():
    api, tid, _, _ = admitted_api("stale")
    result = await settle_pr_repair_attempt(
        api,
        "story-1",
        tid,
        "eng-1",
        "old failure",
        PRConflictRepairAttemptDisposition.FAILED,
    )
    assert result.outcome.value == "stale"
    api.stop_story.assert_not_awaited()
    api.transition_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_failure_retains_failed_task_for_next_supervisor_visit():
    from _run_routing_factories import _make_task

    from shared.contracts.dto.task import TaskStatus
    from src.tasks.supervisor.liveness import supervise_failed_tasks

    api, tid, _, ending = admitted_api()
    task = _make_task(id=tid, story_id="story-1", status="failed", current_iteration=3)
    api.get_tasks_by_status.side_effect = lambda status: (
        [task] if status is TaskStatus.FAILED else []
    )
    api.list_runs.return_value = [SimpleNamespace(id="eng-1", result=None)]
    settle = AsyncMock(side_effect=OSError("API response lost"))
    with patch("src.tasks.supervisor.liveness.settle_pr_repair_attempt", settle):
        assert await supervise_failed_tasks(api, AsyncMock()) == {"retried": 0, "escalated": 0}
        api.transition_task.assert_not_awaited()
        api.transition_story.assert_not_awaited()
        settle.side_effect = None
        settle.return_value = PRConflictRepairAttemptRead.model_validate(ending)
        assert await supervise_failed_tasks(api, AsyncMock()) == {"retried": 0, "escalated": 1}
    api.transition_task.assert_not_awaited()
    api.transition_story.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("engineering_status", ["failed", "gave_up"])
async def test_terminal_conflict_recovery_defers_task_writes_to_scoped_settlement(
    engineering_status,
):
    from _run_routing_factories import _make_run, _make_task

    from shared.contracts.dto.task import TaskStatus
    from src.tasks.supervisor.liveness import supervise_failed_tasks, supervise_stuck_tasks

    api, tid, _, ending = admitted_api()
    task = _make_task(id=tid, story_id="story-1", status="in_dev")
    run = _make_run(
        id="eng-1",
        type="engineering",
        status="failed",
        result={"engineering_status": engineering_status},
        run_metadata={"iteration": 0},
    )
    api.get_tasks_by_status.side_effect = lambda status: [task] if status is task.status else []
    api.list_runs.return_value = [run]
    settle = AsyncMock(side_effect=OSError("settlement unavailable before commit"))
    with patch("src.tasks.supervisor.liveness.settle_pr_repair_attempt", settle):
        await supervise_stuck_tasks(api, AsyncMock())
        assert task.status is TaskStatus.IN_DEV
        api.transition_task.assert_not_awaited()
        api.transition_story.assert_not_awaited()
        settle.assert_awaited_once()
        settle.side_effect = None
        settle.return_value = PRConflictRepairAttemptRead.model_validate(ending)
        await supervise_stuck_tasks(api, AsyncMock())
        assert settle.await_count == 2
        task.status = TaskStatus.WAITING_HUMAN_REVIEW
        await supervise_stuck_tasks(api, AsyncMock())
        await supervise_failed_tasks(api, AsyncMock())
        assert settle.await_count == 2
    api.transition_task.assert_not_awaited()
    api.transition_story.assert_not_awaited()


@pytest.mark.asyncio
async def test_generic_terminal_replay_cannot_park_a_conflict_task():
    from _run_routing_factories import _make_run

    from src.tasks.worker_liveness import replay_terminal_attempt

    api, tid, _, _ = admitted_api()
    run = _make_run(type="engineering", status="failed", result={"engineering_status": "gave_up"})
    with pytest.raises(RuntimeError, match="scoped settlement"):
        await replay_terminal_attempt(api, tid, run, "supervisor")
    api.transition_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_terminal_recovery_uses_scoped_settlement_without_task_only_hop():
    from _run_routing_factories import _make_run
    import structlog

    from src.tasks.task_dispatcher import _recover_dispatched_task

    api, tid, _, ending = admitted_api()
    run = _make_run(
        id="eng-1", type="engineering", status="failed", result={"engineering_status": "gave_up"}
    )
    settle = AsyncMock(return_value=PRConflictRepairAttemptRead.model_validate(ending))
    with patch("src.tasks.task_dispatcher.settle_pr_repair_attempt", settle):
        await _recover_dispatched_task(api, tid, run, structlog.get_logger())
    settle.assert_awaited_once()
    assert settle.await_args.args[1:4] == ("story-1", tid, "eng-1")
    api.transition_task.assert_not_awaited()
