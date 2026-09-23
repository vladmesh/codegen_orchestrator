"""Tests for task dispatcher — dispatches todo tasks and completes stories."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from _github_client_context import self_entering
from _owner_notification_claims import ClaimClock, claim
import httpx
import pytest

from shared.contracts.dto.engineering_budget_policy import (
    EngineeringBudgetAdmissionOutcome,
    EngineeringBudgetAdmissionRead,
)
from shared.contracts.dto.engineering_dispatch import (
    EngineeringDispatchOutcome,
    EngineeringDispatchRead,
    EngineeringDispatchRefusal,
    EngineeringDispatchRepair,
)
from shared.contracts.dto.engineering_execution import (
    EngineeringInfrastructureParkDisposition,
)
from shared.contracts.dto.repository import RepositoryDTO
from shared.contracts.dto.run import RunDTO, RunStatus, RunType
from shared.contracts.dto.story import WAITING_ON_BY_STATUS, StoryDTO, StoryStatus
from shared.contracts.dto.task import TaskDTO, TaskEventDTO
from shared.contracts.dto.work_admission import (
    PaidRunStartRead,
    WorkAdmissionOutcome,
    WorkAdmissionRead,
)
from shared.contracts.vocab import ActionType

# ---------------------------------------------------------------------------
# Helper factories — build valid DTO instances with sensible defaults
# ---------------------------------------------------------------------------

_NOW = datetime.now(UTC)


def _task(**overrides) -> TaskDTO:
    defaults = {
        "id": "task-1",
        "project_id": "00000000-0000-0000-0000-000000000001",
        "type": "feature",
        "title": "Default task",
        "description": None,
        "plan": None,
        "status": "todo",
        "priority": 0,
        "acceptance_criteria": None,
        "current_iteration": 0,
        "max_iterations": 3,
        "need_e2e": False,
        "created_by": "system",
        "source_brainstorm_id": None,
        "repository_id": None,
        "story_id": None,
        "blocked_by_task_id": None,
        "failure_metadata": None,
        # Required on the DTO: a task that is not brief-backed is admitted.
        "dispatch_admitted": True,
        "last_event": None,
        "elapsed_minutes": None,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    defaults.update(overrides)
    return TaskDTO.model_validate(defaults)


def _task_event(**overrides) -> TaskEventDTO:
    defaults = {
        "id": 1,
        "task_id": "task-1",
        "event_type": "iteration_end",
        "from_status": None,
        "to_status": None,
        "iteration": None,
        "details": {},
        "actor": "system",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    defaults.update(overrides)
    return TaskEventDTO.model_validate(defaults)


def _published_ci_run():
    """A finished `main` CI run: the merged commit's images are published."""
    return {
        "id": 900,
        "status": "completed",
        "conclusion": "success",
        "html_url": "https://github.com/o/r/actions/runs/900",
        "created_at": "2026-03-16T12:01:00Z",
        "head_sha": "e" * 40,
    }


def _story(**overrides) -> StoryDTO:
    defaults = {
        "id": "story-1",
        "project_id": "00000000-0000-0000-0000-000000000001",
        "parent_story_id": None,
        "title": "Default story",
        "description": None,
        "acceptance_criteria": None,
        "type": "product",
        "status": "in_progress",
        "priority": 0,
        "blocked_by_story_id": None,
        "created_by": "system",
        "user_report": None,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    defaults.update(overrides)
    # Required on the DTO, and implied by the status the story sits on.
    defaults.setdefault("waiting_on", WAITING_ON_BY_STATUS[StoryStatus(defaults["status"])].value)
    return StoryDTO.model_validate(defaults)


def _repo(**overrides) -> RepositoryDTO:
    defaults = {
        "id": "repo-1",
        "project_id": "00000000-0000-0000-0000-000000000001",
        "name": "weather-bot",
        "git_url": "https://github.com/my-org/weather-bot",
        "provider_repo_id": None,
        "role": "primary",
        "visibility": "private",
        "is_managed": True,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    defaults.update(overrides)
    return RepositoryDTO.model_validate(defaults)


def _admission(
    outcome: EngineeringBudgetAdmissionOutcome = EngineeringBudgetAdmissionOutcome.ADMITTED,
) -> EngineeringBudgetAdmissionRead:
    return EngineeringBudgetAdmissionRead(
        attempt_id="eng-budget-test",
        user_id=1,
        outcome=outcome,
        reservation_microusd=10,
        known_spend_microusd=0,
        active_held_microusd=10 if outcome is EngineeringBudgetAdmissionOutcome.ADMITTED else 0,
        available_microusd=90,
        reservation_state=(
            "active" if outcome is EngineeringBudgetAdmissionOutcome.ADMITTED else None
        ),
    )


def _admitted(run_id: str = "eng-test") -> EngineeringDispatchRead:
    """The admission point admitting a dispatch: the attempt exists and is held."""
    return EngineeringDispatchRead(
        outcome=EngineeringDispatchOutcome.ADMITTED,
        run_id=run_id,
        initiating_run_id="live-run-1",
        paid_work=PaidRunStartRead(
            admission=WorkAdmissionRead(outcome=WorkAdmissionOutcome.ADMITTED), run_id=run_id
        ),
    )


def _refused(reason: EngineeringDispatchRefusal) -> EngineeringDispatchRead:
    """A refusal decided before the paid gate: nothing was created, nothing counted."""
    return EngineeringDispatchRead(outcome=EngineeringDispatchOutcome.REFUSED, reason=reason)


def _paid_refusal(
    reason: EngineeringDispatchRefusal,
    *,
    budget: EngineeringBudgetAdmissionRead | None = None,
    message: str | None = None,
    run_id: str = "eng-test",
    initiating_run_id: str = "live-run-1",
) -> EngineeringDispatchRead:
    """A refusal from the paid gate, carrying the paid decision it wraps."""
    return EngineeringDispatchRead(
        outcome=EngineeringDispatchOutcome.REFUSED,
        reason=reason,
        run_id=run_id,
        initiating_run_id=initiating_run_id,
        paid_work=PaidRunStartRead(
            admission=WorkAdmissionRead(outcome=WorkAdmissionOutcome.DENIED, message=message),
            engineering_budget=budget,
        ),
    )


_REFUSAL_DETAIL = "Repair the selected executor configuration, then retry this attempt."


def _repair(repair: EngineeringDispatchRepair, run_id: str = "eng-abc") -> EngineeringDispatchRead:
    """A prior attempt this task still owes transition work for."""
    return EngineeringDispatchRead(
        outcome=EngineeringDispatchOutcome.REPAIR,
        repair=repair,
        reason=(
            EngineeringDispatchRefusal.LIVE_ATTEMPT_IN_FLIGHT
            if repair is EngineeringDispatchRepair.ADOPT_LIVE_ATTEMPT
            else None
        ),
        run_id=run_id,
        initiating_run_id="live-run-1",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

PROJ_ID = "00000000-0000-0000-0000-000000000001"
STORY_HEAD_SHA = "a" * 40


@pytest.fixture
def api_client():
    from unittest.mock import MagicMock

    from shared.contracts.dto.project import ProjectStatus

    client = AsyncMock()
    # Default project mock — active (scaffolded) project with workspace ready
    project_mock = MagicMock()
    project_mock.id = "proj-1"
    project_mock.status = ProjectStatus.ACTIVE.value
    project_mock.config = {"workspace_ready": True}
    # The run this project's work belongs to — what the dispatcher puts on the
    # message so the worker it leads to is owned by it.
    project_mock.initiating_run_id = "live-run-1"
    client.get_project.return_value = project_mock
    # Default: project has existing applications (feature deploy)
    client.get_applications_by_project.return_value = [{"id": 1, "status": "running"}]
    # Default: no live engineering run left over from a previous tick
    client.list_runs.return_value = []
    client.admit_engineering_budget.return_value = _admission()
    # The one question dispatch asks. Admitted by default: every condition that
    # used to be answered from the mocks above now lives behind this call.
    client.admit_engineering_dispatch.return_value = _admitted()
    return client


@pytest.fixture
def redis_client():
    client = AsyncMock()
    client.publish_message = AsyncMock()
    client.publish_flat = AsyncMock()
    client.redis = AsyncMock()
    client.redis.hget = AsyncMock(return_value=None)
    client.redis.hdel = AsyncMock()
    client.redis.xadd = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_failed_task_poison_does_not_skip_later_dispatcher_supervisors(monkeypatch):
    """A contained task error still permits later order-sensitive supervisors this tick."""
    import asyncio

    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setenv("API_BASE_URL", "http://127.0.0.1:9")
    from src.clients import api as api_module
    from src.tasks import task_dispatcher

    api_client = AsyncMock()
    api_client.get_tasks_by_status.return_value = [
        _task(
            id="task-poison",
            project_id=PROJ_ID,
            story_id="story-poison",
            status="failed",
        )
    ]
    api_client.list_runs.return_value = [
        RunDTO.model_validate(
            {
                "id": "eng-poison",
                "project_id": PROJ_ID,
                "type": "engineering",
                "status": "failed",
                "story_id": "story-poison",
                "result": {
                    "engineering_status": "failed",
                    "execution": {
                        "execution_phase": "pre_agent_refused",
                        "infrastructure_refusal": "project_locked",
                    },
                },
                "created_at": _NOW,
                "updated_at": _NOW,
            }
        )
    ]
    api_client.park_infrastructure_refusal.side_effect = RuntimeError("poison park transaction")
    monkeypatch.setattr(api_module, "api_client", api_client)

    redis = AsyncMock()
    redis.redis = AsyncMock()
    monkeypatch.setattr(task_dispatcher, "RedisStreamClient", lambda: redis)
    monkeypatch.setattr(task_dispatcher, "_dispatch_interval", lambda: 0)

    checks = {
        "trigger_scaffolds": 0,
        "dispatch_todo_tasks": 0,
        "complete_stories": 0,
        "poll_merged_prs": 0,
        "poll_ci_failures": None,
        "supervise_stuck_stories": {"retried": 0, "failed": 0},
        "supervise_stuck_tasks": {"timed_out": 0},
        "supervise_waiting_resource_tasks": {"resumed": 0, "expired": 0},
        "supervise_deploying_stories": {},
        "supervise_waiting_user_secret_stories": {},
    }
    for name, result in checks.items():
        monkeypatch.setattr(task_dispatcher, name, AsyncMock(return_value=result))

    late_supervisor = AsyncMock(return_value={})
    sweep = AsyncMock()
    monkeypatch.setattr(task_dispatcher, "supervise_testing_stories", late_supervisor)
    monkeypatch.setattr(task_dispatcher, "supervise_temporary_access", sweep)
    monkeypatch.setattr(
        task_dispatcher.asyncio,
        "sleep",
        AsyncMock(side_effect=asyncio.CancelledError),
    )

    with pytest.raises(asyncio.CancelledError):
        await task_dispatcher.task_dispatcher_loop()

    late_supervisor.assert_awaited_once_with(api_client, redis)
    sweep.assert_not_awaited()
    redis.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatcher_loop_continues_after_tick_failure_without_sweeping(monkeypatch):
    import asyncio

    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setenv("API_BASE_URL", "http://127.0.0.1:9")
    from src.clients import api as api_module
    from src.tasks import task_dispatcher

    api = AsyncMock()
    redis = AsyncMock()
    log = MagicMock()
    monkeypatch.setattr(api_module, "api_client", api)
    monkeypatch.setattr(task_dispatcher, "RedisStreamClient", lambda: redis)
    monkeypatch.setattr(task_dispatcher, "_dispatch_interval", lambda: 0)
    monkeypatch.setattr(task_dispatcher, "logger", log)
    scaffold = AsyncMock(side_effect=[RuntimeError("tick failed"), 0])
    monkeypatch.setattr(task_dispatcher, "trigger_scaffolds", scaffold)
    for name in ("dispatch_todo_tasks", "complete_stories", "poll_merged_prs"):
        monkeypatch.setattr(task_dispatcher, name, AsyncMock(return_value=0))
    monkeypatch.setattr(task_dispatcher, "poll_ci_failures", AsyncMock())
    for name in (
        "supervise_stuck_stories",
        "supervise_stuck_tasks",
        "supervise_failed_tasks",
        "supervise_waiting_resource_tasks",
        "supervise_deploying_stories",
        "supervise_waiting_user_secret_stories",
        "supervise_testing_stories",
    ):
        monkeypatch.setattr(task_dispatcher, name, AsyncMock(return_value={}))
    sweep = AsyncMock(side_effect=RuntimeError("sweep must be independent"))
    monkeypatch.setattr(task_dispatcher, "supervise_temporary_access", sweep)
    monkeypatch.setattr(
        task_dispatcher.asyncio,
        "sleep",
        AsyncMock(side_effect=[None, asyncio.CancelledError]),
    )

    with pytest.raises(asyncio.CancelledError):
        await task_dispatcher.task_dispatcher_loop()

    assert scaffold.await_count == 2
    log.exception.assert_called_once_with("dispatcher_cycle_error")
    assert any(call.args[0] == "dispatcher_cycle" for call in log.info.call_args_list)
    sweep.assert_not_awaited()
    redis.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_temporary_access_loop_continues_after_sweep_failure_and_closes_redis(monkeypatch):
    import asyncio

    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setenv("API_BASE_URL", "http://127.0.0.1:9")
    from src.clients import api as api_module
    from src.tasks import temporary_access_loop

    api = AsyncMock()
    redis = AsyncMock()
    counts = {
        "dispatched": 2,
        "released": 1,
        "revoked": 3,
        "expired": 4,
        "revoke_failed": 5,
        "escalated": 6,
    }
    sweep = AsyncMock(side_effect=[RuntimeError("sweep failed"), counts])
    log = MagicMock()
    monkeypatch.setattr(api_module, "api_client", api)
    monkeypatch.setattr(temporary_access_loop, "RedisStreamClient", lambda: redis)
    monkeypatch.setattr(temporary_access_loop, "supervise_temporary_access", sweep)
    monkeypatch.setattr(temporary_access_loop, "_temporary_access_interval", lambda: 0)
    monkeypatch.setattr(temporary_access_loop, "logger", log)
    monkeypatch.setattr(
        temporary_access_loop.asyncio,
        "sleep",
        AsyncMock(side_effect=[None, asyncio.CancelledError]),
    )

    with pytest.raises(asyncio.CancelledError):
        await temporary_access_loop.temporary_access_loop()

    assert sweep.await_count == 2
    log.exception.assert_called_once_with("temporary_access_cycle_error")
    log.info.assert_any_call("temporary_access_cycle", **counts)
    log.info.assert_any_call("temporary_access_started", interval=0)
    log.info.assert_any_call("temporary_access_stopped")
    redis.connect.assert_awaited_once()
    redis.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_temporary_access_loop_logs_zero_count_cycle(monkeypatch):
    import asyncio

    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setenv("API_BASE_URL", "http://127.0.0.1:9")
    from src.tasks import temporary_access_loop

    redis = AsyncMock()
    counts = dict.fromkeys(
        ("dispatched", "released", "revoked", "expired", "revoke_failed", "escalated"), 0
    )
    log = MagicMock()
    monkeypatch.setattr(temporary_access_loop, "RedisStreamClient", lambda: redis)
    monkeypatch.setattr(
        temporary_access_loop, "supervise_temporary_access", AsyncMock(return_value=counts)
    )
    monkeypatch.setattr(temporary_access_loop, "_temporary_access_interval", lambda: 0)
    monkeypatch.setattr(temporary_access_loop, "logger", log)
    monkeypatch.setattr(
        temporary_access_loop.asyncio,
        "sleep",
        AsyncMock(side_effect=asyncio.CancelledError),
    )

    with pytest.raises(asyncio.CancelledError):
        await temporary_access_loop.temporary_access_loop()

    log.info.assert_any_call("temporary_access_cycle", **counts)


def _mock_dispatcher_tick(monkeypatch, task_dispatcher, *, scaffold):
    monkeypatch.setattr(task_dispatcher, "trigger_scaffolds", scaffold)
    for name in ("dispatch_todo_tasks", "complete_stories", "poll_merged_prs"):
        monkeypatch.setattr(task_dispatcher, name, AsyncMock(return_value=0))
    monkeypatch.setattr(task_dispatcher, "poll_ci_failures", AsyncMock())
    for name in (
        "supervise_stuck_stories",
        "supervise_stuck_tasks",
        "supervise_failed_tasks",
        "supervise_waiting_resource_tasks",
        "supervise_deploying_stories",
        "supervise_waiting_user_secret_stories",
        "supervise_testing_stories",
    ):
        monkeypatch.setattr(task_dispatcher, name, AsyncMock(return_value={}))


@pytest.mark.asyncio
async def test_dispatcher_tick_does_not_sweep_owed_owner_notifications(monkeypatch):
    import asyncio

    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setenv("API_BASE_URL", "http://127.0.0.1:9")
    from src.clients import api as api_module
    from src.tasks import owner_notifications, task_dispatcher

    api = AsyncMock()
    redis = AsyncMock()
    log = MagicMock()
    monkeypatch.setattr(api_module, "api_client", api)
    monkeypatch.setattr(task_dispatcher, "RedisStreamClient", lambda: redis)
    monkeypatch.setattr(task_dispatcher, "_dispatch_interval", lambda: 0)
    monkeypatch.setattr(task_dispatcher, "logger", log)
    _mock_dispatcher_tick(monkeypatch, task_dispatcher, scaffold=AsyncMock(return_value=0))
    sweep = AsyncMock(side_effect=RuntimeError("the tick must not sweep"))
    monkeypatch.setattr(owner_notifications, "supervise_owed_owner_notifications", sweep)
    monkeypatch.setattr(
        task_dispatcher.asyncio,
        "sleep",
        AsyncMock(side_effect=asyncio.CancelledError),
    )

    with pytest.raises(asyncio.CancelledError):
        await task_dispatcher.task_dispatcher_loop()

    assert not hasattr(task_dispatcher, "supervise_owed_owner_notifications")
    sweep.assert_not_awaited()
    api.list_runs_owing_owner_notification.assert_not_awaited()
    api.list_stories_owing_owner_notification.assert_not_awaited()
    log.exception.assert_not_called()
    assert any(call.args[0] == "dispatcher_cycle" for call in log.info.call_args_list)


_OWNER_NOTIFICATION_COUNTS = {
    "delivered": 1,
    "retrying": 2,
    "exhausted": 3,
    "unaddressable": 4,
    "voided": 5,
    "skipped": 6,
    "not_due": 7,
    "superseded": 8,
}


@pytest.mark.asyncio
async def test_owner_notification_loop_continues_after_sweep_failure_and_closes_redis(
    monkeypatch,
):
    import asyncio

    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setenv("API_BASE_URL", "http://127.0.0.1:9")
    from src.clients import api as api_module
    from src.tasks import owner_notification_loop, task_dispatcher
    from src.tasks.owner_notifications import OwnerNotificationOutcome

    assert set(_OWNER_NOTIFICATION_COUNTS) == {
        outcome.value for outcome in OwnerNotificationOutcome
    }
    api = AsyncMock()
    redis = AsyncMock()
    sweep = AsyncMock(side_effect=[RuntimeError("sweep failed"), _OWNER_NOTIFICATION_COUNTS])
    log = MagicMock()
    monkeypatch.setattr(api_module, "api_client", api)
    monkeypatch.setattr(owner_notification_loop, "RedisStreamClient", lambda: redis)
    monkeypatch.setattr(owner_notification_loop, "supervise_owed_owner_notifications", sweep)
    monkeypatch.setattr(owner_notification_loop, "_owner_notification_interval", lambda: 0)
    monkeypatch.setattr(owner_notification_loop, "logger", log)
    monkeypatch.setattr(
        owner_notification_loop.asyncio,
        "sleep",
        AsyncMock(side_effect=[None, asyncio.CancelledError]),
    )
    # The dispatcher tick is a separate loop: nothing here reaches it.
    scaffold = AsyncMock(return_value=0)
    monkeypatch.setattr(task_dispatcher, "trigger_scaffolds", scaffold)

    with pytest.raises(asyncio.CancelledError):
        await owner_notification_loop.owner_notification_loop()

    assert sweep.await_count == 2
    sweep.assert_awaited_with(api, redis)
    log.exception.assert_called_once_with("owner_notifications_cycle_error")
    log.info.assert_any_call("owner_notifications_cycle", **_OWNER_NOTIFICATION_COUNTS)
    log.info.assert_any_call("owner_notifications_started", interval=0)
    log.info.assert_any_call("owner_notifications_stopped")
    redis.connect.assert_awaited_once()
    redis.close.assert_awaited_once()
    scaffold.assert_not_awaited()


@pytest.mark.asyncio
async def test_notification_sweep_failure_and_dispatcher_tick_failure_isolate_each_other(
    monkeypatch,
):
    """Both loops run side by side; each one's failures stay inside its own cycle."""
    import asyncio

    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setenv("API_BASE_URL", "http://127.0.0.1:9")
    from src.clients import api as api_module
    from src.tasks import owner_notification_loop, task_dispatcher

    api = AsyncMock()
    monkeypatch.setattr(api_module, "api_client", api)
    dispatcher_redis = AsyncMock()
    notification_redis = AsyncMock()
    dispatcher_log = MagicMock()
    notification_log = MagicMock()
    monkeypatch.setattr(task_dispatcher, "RedisStreamClient", lambda: dispatcher_redis)
    monkeypatch.setattr(task_dispatcher, "_dispatch_interval", lambda: 0)
    monkeypatch.setattr(task_dispatcher, "logger", dispatcher_log)
    monkeypatch.setattr(owner_notification_loop, "RedisStreamClient", lambda: notification_redis)
    monkeypatch.setattr(owner_notification_loop, "_owner_notification_interval", lambda: 0)
    monkeypatch.setattr(owner_notification_loop, "logger", notification_log)

    # Every other dispatcher tick fails, and so does every other sweep.
    ticks = sweeps = 0
    enough = asyncio.Event()

    async def scaffold(*_args):
        nonlocal ticks
        ticks += 1
        if ticks % 2:
            raise RuntimeError("tick failed")
        return 0

    async def sweep(*_args):
        nonlocal sweeps
        sweeps += 1
        if sweeps >= 6 and ticks >= 6:
            enough.set()
        if sweeps % 2:
            raise RuntimeError("sweep failed")
        return _OWNER_NOTIFICATION_COUNTS

    _mock_dispatcher_tick(monkeypatch, task_dispatcher, scaffold=AsyncMock(side_effect=scaffold))
    monkeypatch.setattr(
        owner_notification_loop, "supervise_owed_owner_notifications", AsyncMock(side_effect=sweep)
    )

    loops = [
        asyncio.create_task(task_dispatcher.task_dispatcher_loop()),
        asyncio.create_task(owner_notification_loop.owner_notification_loop()),
    ]
    try:
        await asyncio.wait_for(enough.wait(), timeout=5)
        assert not any(loop.done() for loop in loops)
    finally:
        for loop in loops:
            loop.cancel()
        await asyncio.gather(*loops, return_exceptions=True)

    tick_errors = dispatcher_log.exception.call_args_list
    assert len(tick_errors) >= 3
    assert all(c.args == ("dispatcher_cycle_error",) for c in tick_errors)
    assert any(c.args[0] == "dispatcher_cycle" for c in dispatcher_log.info.call_args_list)
    sweep_errors = notification_log.exception.call_args_list
    assert len(sweep_errors) >= 3
    assert all(c.args == ("owner_notifications_cycle_error",) for c in sweep_errors)
    notification_log.info.assert_any_call("owner_notifications_cycle", **_OWNER_NOTIFICATION_COUNTS)
    dispatcher_redis.close.assert_awaited_once()
    notification_redis.close.assert_awaited_once()


_STATE_AGE_COUNTS = {"parked": 1, "failed": 2, "skipped": 3}
_STAGE_NOTICE_COUNTS = {"entered": 4, "still_there": 5, "unaddressable": 6}
_STATE_AGE_CYCLE = {f"state_age_{name}": n for name, n in _STATE_AGE_COUNTS.items()}
_STAGE_NOTICE_CYCLE = {f"stage_notices_{name}": n for name, n in _STAGE_NOTICE_COUNTS.items()}


@pytest.mark.asyncio
async def test_dispatcher_tick_does_not_run_story_supervision(monkeypatch):
    import asyncio

    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setenv("API_BASE_URL", "http://127.0.0.1:9")
    from src.clients import api as api_module
    from src.tasks import supervisor, task_dispatcher

    api = AsyncMock()
    redis = AsyncMock()
    log = MagicMock()
    monkeypatch.setattr(api_module, "api_client", api)
    monkeypatch.setattr(task_dispatcher, "RedisStreamClient", lambda: redis)
    monkeypatch.setattr(task_dispatcher, "_dispatch_interval", lambda: 0)
    monkeypatch.setattr(task_dispatcher, "logger", log)
    _mock_dispatcher_tick(monkeypatch, task_dispatcher, scaffold=AsyncMock(return_value=0))
    watchdog = AsyncMock(side_effect=RuntimeError("the tick must not run the watchdog"))
    notices = AsyncMock(side_effect=RuntimeError("the tick must not announce stages"))
    monkeypatch.setattr(supervisor, "supervise_state_age_bounds", watchdog)
    monkeypatch.setattr(supervisor, "supervise_stage_notices", notices)
    monkeypatch.setattr(
        task_dispatcher.asyncio,
        "sleep",
        AsyncMock(side_effect=asyncio.CancelledError),
    )

    with pytest.raises(asyncio.CancelledError):
        await task_dispatcher.task_dispatcher_loop()

    assert not hasattr(task_dispatcher, "supervise_state_age_bounds")
    assert not hasattr(task_dispatcher, "supervise_stage_notices")
    watchdog.assert_not_awaited()
    notices.assert_not_awaited()
    api.get_stories_by_status.assert_not_awaited()
    log.exception.assert_not_called()
    assert any(call.args[0] == "dispatcher_cycle" for call in log.info.call_args_list)


@pytest.mark.asyncio
async def test_story_supervision_sweeps_fail_independently_within_a_cycle(monkeypatch):
    """A failing watchdog still lets that cycle's notices run, and the reverse."""
    import asyncio

    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setenv("API_BASE_URL", "http://127.0.0.1:9")
    from src.clients import api as api_module
    from src.tasks import story_supervision_loop, task_dispatcher

    api = AsyncMock()
    redis = AsyncMock()
    log = MagicMock()
    # Cycle 1: the watchdog raises. Cycle 2: the notices raise.
    watchdog = AsyncMock(side_effect=[RuntimeError("watchdog failed"), _STATE_AGE_COUNTS])
    notices = AsyncMock(side_effect=[_STAGE_NOTICE_COUNTS, RuntimeError("notices failed")])
    monkeypatch.setattr(api_module, "api_client", api)
    monkeypatch.setattr(story_supervision_loop, "RedisStreamClient", lambda: redis)
    monkeypatch.setattr(story_supervision_loop, "supervise_state_age_bounds", watchdog)
    monkeypatch.setattr(story_supervision_loop, "supervise_stage_notices", notices)
    monkeypatch.setattr(story_supervision_loop, "_story_supervision_interval", lambda: 0)
    monkeypatch.setattr(story_supervision_loop, "logger", log)
    monkeypatch.setattr(
        story_supervision_loop.asyncio,
        "sleep",
        AsyncMock(side_effect=[None, asyncio.CancelledError]),
    )
    # The dispatcher tick is a separate loop: nothing here reaches it.
    scaffold = AsyncMock(return_value=0)
    monkeypatch.setattr(task_dispatcher, "trigger_scaffolds", scaffold)

    with pytest.raises(asyncio.CancelledError):
        await story_supervision_loop.story_supervision_loop()

    assert watchdog.await_count == 2
    assert notices.await_count == 2
    watchdog.assert_awaited_with(api, redis)
    notices.assert_awaited_with(api, redis)
    assert [c.args for c in log.exception.call_args_list] == [
        ("story_supervision_cycle_error",),
        ("story_supervision_cycle_error",),
    ]
    assert [c.kwargs for c in log.exception.call_args_list] == [
        {"sweep": "state_age"},
        {"sweep": "stage_notices"},
    ]
    cycles = [c for c in log.info.call_args_list if c.args == ("story_supervision_cycle",)]
    assert [c.kwargs for c in cycles] == [_STAGE_NOTICE_CYCLE, _STATE_AGE_CYCLE]
    log.info.assert_any_call("story_supervision_started", interval=0)
    log.info.assert_any_call("story_supervision_stopped")
    redis.connect.assert_awaited_once()
    redis.close.assert_awaited_once()
    scaffold.assert_not_awaited()


@pytest.mark.asyncio
async def test_story_supervision_logs_one_cycle_line_with_both_sweeps_counts(monkeypatch):
    import asyncio

    from src.tasks import story_supervision_loop

    redis = AsyncMock()
    log = MagicMock()
    monkeypatch.setattr(story_supervision_loop, "RedisStreamClient", lambda: redis)
    monkeypatch.setattr(
        story_supervision_loop,
        "supervise_state_age_bounds",
        AsyncMock(return_value=_STATE_AGE_COUNTS),
    )
    monkeypatch.setattr(
        story_supervision_loop,
        "supervise_stage_notices",
        AsyncMock(return_value=_STAGE_NOTICE_COUNTS),
    )
    monkeypatch.setattr(story_supervision_loop, "_story_supervision_interval", lambda: 0)
    monkeypatch.setattr(story_supervision_loop, "logger", log)
    monkeypatch.setattr(
        story_supervision_loop.asyncio,
        "sleep",
        AsyncMock(side_effect=asyncio.CancelledError),
    )

    with pytest.raises(asyncio.CancelledError):
        await story_supervision_loop.story_supervision_loop()

    cycles = [c for c in log.info.call_args_list if c.args == ("story_supervision_cycle",)]
    assert [c.kwargs for c in cycles] == [{**_STATE_AGE_CYCLE, **_STAGE_NOTICE_CYCLE}]
    log.exception.assert_not_called()


@pytest.mark.asyncio
async def test_story_supervision_failure_and_dispatcher_tick_failure_isolate_each_other(
    monkeypatch,
):
    """Both loops run side by side; each one's failures stay inside its own cycle."""
    import asyncio

    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setenv("API_BASE_URL", "http://127.0.0.1:9")
    from src.clients import api as api_module
    from src.tasks import story_supervision_loop, task_dispatcher

    api = AsyncMock()
    monkeypatch.setattr(api_module, "api_client", api)
    dispatcher_redis = AsyncMock()
    supervision_redis = AsyncMock()
    dispatcher_log = MagicMock()
    supervision_log = MagicMock()
    monkeypatch.setattr(task_dispatcher, "RedisStreamClient", lambda: dispatcher_redis)
    monkeypatch.setattr(task_dispatcher, "_dispatch_interval", lambda: 0)
    monkeypatch.setattr(task_dispatcher, "logger", dispatcher_log)
    monkeypatch.setattr(story_supervision_loop, "RedisStreamClient", lambda: supervision_redis)
    monkeypatch.setattr(story_supervision_loop, "_story_supervision_interval", lambda: 0)
    monkeypatch.setattr(story_supervision_loop, "logger", supervision_log)

    # Every other dispatcher tick fails, and so does every other supervision
    # cycle, in both of its sweeps.
    ticks = cycles = 0
    enough = asyncio.Event()

    async def scaffold(*_args):
        nonlocal ticks
        ticks += 1
        if ticks % 2:
            raise RuntimeError("tick failed")
        return 0

    async def watchdog(*_args):
        nonlocal cycles
        cycles += 1
        if cycles >= 6 and ticks >= 6:
            enough.set()
        if cycles % 2:
            raise RuntimeError("watchdog failed")
        return _STATE_AGE_COUNTS

    async def notices(*_args):
        if cycles % 2:
            raise RuntimeError("notices failed")
        return _STAGE_NOTICE_COUNTS

    _mock_dispatcher_tick(monkeypatch, task_dispatcher, scaffold=AsyncMock(side_effect=scaffold))
    monkeypatch.setattr(
        story_supervision_loop, "supervise_state_age_bounds", AsyncMock(side_effect=watchdog)
    )
    monkeypatch.setattr(
        story_supervision_loop, "supervise_stage_notices", AsyncMock(side_effect=notices)
    )

    loops = [
        asyncio.create_task(task_dispatcher.task_dispatcher_loop()),
        asyncio.create_task(story_supervision_loop.story_supervision_loop()),
    ]
    try:
        await asyncio.wait_for(enough.wait(), timeout=5)
        assert not any(loop.done() for loop in loops)
    finally:
        for loop in loops:
            loop.cancel()
        await asyncio.gather(*loops, return_exceptions=True)

    tick_errors = dispatcher_log.exception.call_args_list
    assert len(tick_errors) >= 3
    assert all(c.args == ("dispatcher_cycle_error",) for c in tick_errors)
    assert any(c.args[0] == "dispatcher_cycle" for c in dispatcher_log.info.call_args_list)
    sweep_errors = supervision_log.exception.call_args_list
    assert len(sweep_errors) >= 6
    assert all(c.args == ("story_supervision_cycle_error",) for c in sweep_errors)
    supervision_log.info.assert_any_call(
        "story_supervision_cycle", **_STATE_AGE_CYCLE, **_STAGE_NOTICE_CYCLE
    )
    dispatcher_redis.close.assert_awaited_once()
    supervision_redis.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_worker_reconciliation_cycle_runs_both_durable_scans(monkeypatch):
    from src.tasks import worker_reconciliation

    api = AsyncMock()
    redis = AsyncMock()
    terminal = AsyncMock(return_value=2)
    gave_up = AsyncMock(return_value=3)
    monkeypatch.setattr(worker_reconciliation, "reconcile_terminal_story_workers", terminal)
    monkeypatch.setattr(worker_reconciliation, "reconcile_gave_up_attempt_workers", gave_up)

    counts = await worker_reconciliation.reconcile_workers_once(api, redis)

    assert counts == {
        "terminal_workers_requested": 2,
        "gave_up_workers_requested": 3,
    }
    terminal.assert_awaited_once_with(api, redis)
    gave_up.assert_awaited_once_with(api, redis)


@pytest.mark.asyncio
async def test_worker_reconciliation_scan_failure_does_not_skip_sibling(monkeypatch):
    from src.tasks import worker_reconciliation

    api = AsyncMock()
    redis = AsyncMock()
    terminal = AsyncMock(side_effect=RuntimeError("terminal scan failed"))
    gave_up = AsyncMock(return_value=1)
    monkeypatch.setattr(worker_reconciliation, "reconcile_terminal_story_workers", terminal)
    monkeypatch.setattr(worker_reconciliation, "reconcile_gave_up_attempt_workers", gave_up)

    counts = await worker_reconciliation.reconcile_workers_once(api, redis)

    assert counts == {
        "terminal_workers_requested": 0,
        "gave_up_workers_requested": 1,
    }
    gave_up.assert_awaited_once_with(api, redis)


@pytest.mark.asyncio
async def test_worker_reconciliation_loop_owns_redis_lifecycle(monkeypatch):
    import asyncio

    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setenv("API_BASE_URL", "http://127.0.0.1:9")
    from src.clients import api as api_module
    from src.tasks import worker_reconciliation

    api = AsyncMock()
    monkeypatch.setattr(api_module, "api_client", api)

    redis = AsyncMock()
    redis.redis = AsyncMock()
    monkeypatch.setattr(worker_reconciliation, "RedisStreamClient", lambda: redis)
    monkeypatch.setattr(worker_reconciliation, "_reconciliation_interval", lambda: 0)
    cycle = AsyncMock(
        return_value={
            "terminal_workers_requested": 0,
            "gave_up_workers_requested": 0,
        }
    )
    monkeypatch.setattr(worker_reconciliation, "reconcile_workers_once", cycle)
    monkeypatch.setattr(
        worker_reconciliation.asyncio,
        "sleep",
        AsyncMock(side_effect=asyncio.CancelledError),
    )

    with pytest.raises(asyncio.CancelledError):
        await worker_reconciliation.worker_reconciliation_loop()

    redis.connect.assert_awaited_once()
    cycle.assert_awaited_once_with(api, redis)
    redis.close.assert_awaited_once()


class TestDispatchTodoTasks:
    """Dispatch unblocked todo tasks to engineering queue."""

    @pytest.mark.asyncio
    async def test_message_carries_the_run_the_project_was_created_for(
        self, api_client, redis_client
    ):
        """The message owns the work by the initiating run, not by this attempt.

        The dispatcher creates an engineering Run row per attempt, and the old
        contract had nothing else on the message to own a worker by. The run
        that *asked* for the work is a different, longer-lived identity, read
        off the project where its initiator wrote it; the attempt travels
        beside it as `task_id`, never as its substitute.
        """
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(
                id="task-1",
                title="Add user model",
                description="Create User SQLAlchemy model",
                type="feature",
                project_id=PROJ_ID,
                story_id="story-1",
                blocked_by_task_id=None,
                status="todo",
            )
        ]
        api_client.get_task_events.return_value = []
        api_client.get_story.return_value = _story(id="story-1", project_id=PROJ_ID)

        await dispatch_todo_tasks(api_client, redis_client)

        decision = api_client.admit_engineering_dispatch.return_value
        msg = redis_client.publish_message.call_args[0][1]
        assert msg.initiating_run_id == decision.initiating_run_id
        assert msg.task_id == decision.run_id
        assert msg.task_id != msg.initiating_run_id

    #: Every refusal the admission point reaches before anything is counted.
    #: Each of these was an inline condition of `dispatch_todo_tasks` with a test
    #: of its own here — a project that predates run ownership, an
    #: unresolved blocker, a draft project, a workspace that is not ready, a busy
    #: story, a story with a sibling in human review, and the internal project.
    #: What the dispatcher owes them is now one behaviour, so they are pinned
    #: here as one property; which *state* produces which reason is pinned in
    #: services/api/tests/service/test_engineering_dispatch_admission.py, where
    #: the conditions moved.
    _UNCOUNTED_REFUSALS = [
        EngineeringDispatchRefusal.TASK_NOT_DISPATCHABLE,
        EngineeringDispatchRefusal.PRODUCT_BRIEF_NOT_ADMITTED,
        EngineeringDispatchRefusal.INTERNAL_PROJECT,
        EngineeringDispatchRefusal.BLOCKER_UNRESOLVED,
        EngineeringDispatchRefusal.PROJECT_HAS_NO_INITIATING_RUN,
        EngineeringDispatchRefusal.PROJECT_NOT_SCAFFOLDED,
        EngineeringDispatchRefusal.WORKSPACE_NOT_READY,
        EngineeringDispatchRefusal.STORY_BUSY,
        EngineeringDispatchRefusal.STORY_WAITING_HUMAN_REVIEW,
    ]

    @pytest.mark.parametrize("reason", _UNCOUNTED_REFUSALS)
    @pytest.mark.asyncio
    async def test_a_refusal_that_counted_nothing_leaves_the_task_untouched(
        self, api_client, redis_client, reason
    ):
        """No message, no transition, no compensation — and a later tick may retry.

        These refusals are decided before the paid gate, so no attempt exists to
        release and the task keeps its place in the todo queue.
        """
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(id="task-1", project_id=PROJ_ID, story_id="story-1", status="todo")
        ]
        api_client.admit_engineering_dispatch.return_value = _refused(reason)

        assert await dispatch_todo_tasks(api_client, redis_client) == 0

        redis_client.publish_message.assert_not_called()
        api_client.transition_task.assert_not_called()
        api_client.transition_story.assert_not_called()
        api_client.abort_paid_run_pre_handoff.assert_not_called()

    @pytest.mark.asyncio
    async def test_every_todo_task_is_asked_about_by_id(self, api_client, redis_client):
        """The dispatcher selects candidates and asks; it decides nothing itself."""
        from shared.contracts.dto.engineering_dispatch import EngineeringDispatchCommand
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(id="task-1", project_id=PROJ_ID, story_id="story-1", status="todo"),
            _task(id="task-2", project_id=PROJ_ID, story_id="story-2", status="todo"),
        ]
        api_client.get_task_events.return_value = []

        await dispatch_todo_tasks(api_client, redis_client)

        assert [call.args[0] for call in api_client.admit_engineering_dispatch.await_args_list] == [
            EngineeringDispatchCommand(task_id="task-1"),
            EngineeringDispatchCommand(task_id="task-2"),
        ]

    @pytest.mark.asyncio
    async def test_an_unanswered_question_dispatches_nothing(self, api_client, redis_client):
        """A failed admission call decided nothing, so nothing was counted or owed."""
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(id="task-1", project_id=PROJ_ID, story_id="story-1", status="todo")
        ]
        api_client.admit_engineering_dispatch.side_effect = RuntimeError("API unavailable")

        assert await dispatch_todo_tasks(api_client, redis_client) == 0

        redis_client.publish_message.assert_not_called()
        api_client.transition_task.assert_not_called()
        api_client.abort_paid_run_pre_handoff.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatches_unblocked_task(self, api_client, redis_client):
        """Task with no blocker gets a run created and published."""
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(
                id="task-1",
                title="Add user model",
                description="Create User SQLAlchemy model",
                type="feature",
                project_id=PROJ_ID,
                story_id="story-1",
                blocked_by_task_id=None,
                status="todo",
            )
        ]
        api_client.get_task_events.return_value = []
        api_client.transition_task.return_value = {}
        api_client.get_story.return_value = _story(id="story-1", project_id=PROJ_ID)

        await dispatch_todo_tasks(api_client, redis_client)

        # The attempt was created by the admission point, which was asked about
        # this task and nothing else.
        api_client.admit_engineering_dispatch.assert_awaited_once()
        assert api_client.admit_engineering_dispatch.await_args.args[0].task_id == "task-1"
        api_client.start_paid_run.assert_not_called()

        # Should publish to engineering queue
        redis_client.publish_message.assert_called_once()
        assert redis_client.publish_message.call_args[0][1].task_id == "eng-test"

        # Should transition task to in_dev
        api_client.transition_task.assert_called_once_with("task-1", "in_dev", "dispatcher")

    @pytest.mark.asyncio
    async def test_budget_denial_moves_task_to_human_review_without_a_retry(
        self, api_client, redis_client
    ):
        """Denial is terminal for automatic dispatch until a human resumes it."""
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        state = {
            "task": _task(
                id="task-1",
                project_id=PROJ_ID,
                story_id="story-1",
                status="todo",
            )
        }

        async def list_todo_tasks(*_args, **_kwargs):
            return [state["task"]] if state["task"].status == "todo" else []

        async def apply_transition(task_id, status, *_args, **_kwargs):
            assert task_id == "task-1"
            state["task"] = state["task"].model_copy(update={"status": status})
            return {}

        api_client.get_tasks_by_status.side_effect = list_todo_tasks
        api_client.transition_task.side_effect = apply_transition
        api_client.get_task_events.return_value = []
        api_client.admit_engineering_dispatch.return_value = _paid_refusal(
            EngineeringDispatchRefusal.ENGINEERING_BUDGET_DENIED,
            budget=_admission(EngineeringBudgetAdmissionOutcome.DENIED),
        )

        assert await dispatch_todo_tasks(api_client, redis_client) == 0
        assert await dispatch_todo_tasks(api_client, redis_client) == 0

        api_client.admit_engineering_dispatch.assert_awaited_once()
        redis_client.publish_message.assert_not_called()
        first, second = api_client.transition_task.await_args_list
        assert first.args == ("task-1", "in_dev", "dispatcher")
        assert second.args == ("task-1", "waiting_human_review", "dispatcher")
        assert second.kwargs["details"] == {
            "reason": "engineering_budget_denied",
            "attempt_id": "eng-budget-test",
            "known_spend_microusd": 0,
            "active_held_microusd": 0,
            "available_microusd": 90,
        }
        assert state["task"].status == "waiting_human_review"

    @pytest.mark.parametrize(
        "reason",
        [
            EngineeringDispatchRefusal.EXECUTOR_UNAVAILABLE,
            EngineeringDispatchRefusal.EXECUTOR_CONFIRMATION_REQUIRED,
        ],
    )
    @pytest.mark.parametrize("story_id", ["story-1", None])
    @pytest.mark.asyncio
    async def test_infrastructure_refusal_parked_by_admission_sequences_nothing(
        self, api_client, redis_client, reason, story_id
    ):
        """Admission parked the refusal in its own transaction; the tick adds no write."""
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        task = _task(id="task-1", project_id=PROJ_ID, story_id=story_id, status="todo")
        api_client.get_tasks_by_status.return_value = [task]
        api_client.admit_engineering_dispatch.return_value = _paid_refusal(
            reason, message=_REFUSAL_DETAIL
        ).model_copy(
            update={"infrastructure_park": EngineeringInfrastructureParkDisposition.PARKED}
        )

        assert await dispatch_todo_tasks(api_client, redis_client) == 0

        for call in (
            api_client.park_infrastructure_refusal,
            api_client.update_task,
            api_client.update_story,
            api_client.update_run,
            api_client.update_story_owner_notification,
            api_client.transition_task,
            api_client.transition_story,
            api_client.get_run,
        ):
            call.assert_not_awaited()
        redis_client.publish_message.assert_not_awaited()
        redis_client.publish_flat.assert_not_awaited()
        assert task.current_iteration == 0

    @pytest.mark.asyncio
    async def test_a_lost_refusal_response_is_contained_and_the_next_task_dispatches(
        self, api_client, redis_client
    ):
        """The committed admission owns the park; an unanswered call routes nothing."""
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        refused = _task(id="task-1", project_id=PROJ_ID, story_id="story-1", status="todo")
        healthy = _task(id="task-2", project_id=PROJ_ID, status="todo")
        api_client.get_tasks_by_status.return_value = [refused, healthy]
        api_client.admit_engineering_dispatch.side_effect = [
            httpx.ReadTimeout("the refusal committed but its answer never arrived"),
            _admitted("eng-healthy"),
        ]

        assert await dispatch_todo_tasks(api_client, redis_client) == 1

        api_client.park_infrastructure_refusal.assert_not_awaited()
        assert [call.args[0] for call in api_client.transition_task.await_args_list] == ["task-2"]
        assert [
            call.args[1].planning_task_id for call in redis_client.publish_message.await_args_list
        ] == ["task-2"]
        assert refused.current_iteration == 0

    @pytest.mark.asyncio
    async def test_dispatches_refactor_task_as_feature_action(self, api_client, redis_client):
        """Planning refactors use the engineering feature action."""
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(
                type="refactor",
                project_id=PROJ_ID,
                story_id="story-1",
            )
        ]
        api_client.get_task_events.return_value = []

        dispatched = await dispatch_todo_tasks(api_client, redis_client)

        assert dispatched == 1
        eng_msg = redis_client.publish_message.call_args[0][1]
        assert eng_msg.action is ActionType.FEATURE
        api_client.transition_task.assert_called_once_with("task-1", "in_dev", "dispatcher")

    @pytest.mark.asyncio
    async def test_dispatches_task_when_blocker_done(self, api_client, redis_client):
        """Task whose blocker is done gets dispatched."""
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(
                id="task-2",
                title="Add API endpoint",
                description="REST endpoint",
                type="feature",
                project_id=PROJ_ID,
                story_id="story-1",
                blocked_by_task_id="task-1",
                status="todo",
            )
        ]
        api_client.get_task.return_value = _task(id="task-1", status="done")
        api_client.get_task_events.return_value = [
            _task_event(
                event_type="iteration_end",
                details={"commit_sha": "abc", "summary": "Done"},
            )
        ]
        api_client.transition_task.return_value = {}
        api_client.get_story.return_value = _story(id="story-1", project_id=PROJ_ID)

        await dispatch_todo_tasks(api_client, redis_client)

        api_client.admit_engineering_dispatch.assert_awaited_once()
        redis_client.publish_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_includes_cumulative_context(self, api_client, redis_client):
        """Dispatched task includes context from completed sibling tasks."""
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(
                id="task-2",
                title="Add API endpoint",
                description="REST endpoint",
                type="feature",
                project_id=PROJ_ID,
                story_id="story-1",
                blocked_by_task_id="task-1",
                status="todo",
            )
        ]
        api_client.get_task.return_value = _task(id="task-1", status="done")
        # Sibling tasks for story-1: task-1 (done) and task-2 (todo)
        api_client.get_tasks_by_story.return_value = [
            _task(id="task-1", status="done", story_id="story-1", project_id=PROJ_ID),
            _task(id="task-2", status="todo", story_id="story-1", project_id=PROJ_ID),
        ]
        # Events for task-1 (the done sibling)
        api_client.get_task_events.return_value = [
            _task_event(
                event_type="iteration_end",
                details={
                    "commit_sha": "abc123",
                    "summary": "Created User model with email field",
                },
            )
        ]
        api_client.transition_task.return_value = {}
        api_client.get_story.return_value = _story(id="story-1", project_id=PROJ_ID)

        await dispatch_todo_tasks(api_client, redis_client)

        # The engineering message should have enriched description
        eng_msg = redis_client.publish_message.call_args[0][1]
        assert "User model" in eng_msg.description
        assert eng_msg.planning_task_id == "task-2"

    @pytest.mark.asyncio
    async def test_includes_story_id_in_engineering_message(self, api_client, redis_client):
        """Dispatched task includes story_id for worker reuse."""
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(
                id="task-1",
                title="Add user model",
                description="Create model",
                type="feature",
                project_id=PROJ_ID,
                story_id="story-1",
                blocked_by_task_id=None,
                status="todo",
            )
        ]
        api_client.get_task_events.return_value = []
        api_client.transition_task.return_value = {}
        api_client.get_story.return_value = _story(id="story-1", project_id=PROJ_ID)

        await dispatch_todo_tasks(api_client, redis_client)

        eng_msg = redis_client.publish_message.call_args[0][1]
        assert eng_msg.story_id == "story-1"

    @pytest.mark.asyncio
    async def test_standalone_task_publishes_one_run_owned_worker(self, api_client, redis_client):
        """A released storyless task dispatches without aborting its admitted run."""
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(
                id="task-1",
                title="Standalone task",
                description="No story",
                type="feature",
                project_id=PROJ_ID,
                story_id=None,
                blocked_by_task_id=None,
                status="todo",
            )
        ]
        api_client.transition_task.return_value = {}

        assert await dispatch_todo_tasks(api_client, redis_client) == 1

        message = redis_client.publish_message.await_args.args[1]
        assert message.story_id is None
        assert message.branch is None
        api_client.abort_paid_run_pre_handoff.assert_not_awaited()
        api_client.transition_task.assert_awaited_once_with("task-1", "in_dev", "dispatcher")

    @pytest.mark.asyncio
    async def test_dispatches_when_sibling_failed_normally(self, api_client, redis_client):
        """Todo task with a normally-failed sibling (no reject) -> still dispatched."""
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(
                id="task-2",
                title="Add endpoint",
                description="REST API",
                type="feature",
                project_id=PROJ_ID,
                story_id="story-1",
                blocked_by_task_id=None,
                status="todo",
            )
        ]
        # Sibling task-1 failed normally (no reject metadata)
        api_client.get_tasks_by_story.return_value = [
            _task(id="task-1", status="failed", story_id="story-1", project_id=PROJ_ID),
            _task(id="task-2", status="todo", story_id="story-1", project_id=PROJ_ID),
        ]
        api_client.get_task_events.return_value = []
        api_client.transition_task.return_value = {}
        api_client.get_story.return_value = _story(id="story-1", project_id=PROJ_ID)

        await dispatch_todo_tasks(api_client, redis_client)

        # Should dispatch — normal failure doesn't block siblings
        redis_client.publish_message.assert_called_once()


class TestBranchInDispatch:
    """Tests that branch is included in EngineeringMessage."""

    @pytest.mark.asyncio
    async def test_dispatch_includes_branch_for_story_task(self, api_client, redis_client):
        """Task with story_id gets branch=story/{story_id} in EngineeringMessage."""
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(
                id="task-1",
                title="Add user model",
                description="Create User SQLAlchemy model",
                type="feature",
                project_id=PROJ_ID,
                story_id="story-abc",
                blocked_by_task_id=None,
                status="todo",
            )
        ]
        api_client.get_task_events.return_value = []
        api_client.transition_task.return_value = {}
        api_client.get_story.return_value = _story(id="story-abc", project_id=PROJ_ID)

        await dispatch_todo_tasks(api_client, redis_client)

        redis_client.publish_message.assert_called_once()
        eng_msg = redis_client.publish_message.call_args[0][1]
        assert eng_msg.branch == "story/story-abc"

    @pytest.mark.asyncio
    async def test_standalone_task_has_a_branchless_engineering_handoff(
        self, api_client, redis_client
    ):
        """Standalone work keeps run ownership and does not invent a story branch."""
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(
                id="task-1",
                title="Fix bug",
                description="Fix it",
                type="fix",
                project_id=PROJ_ID,
                story_id=None,
                blocked_by_task_id=None,
                status="todo",
            )
        ]
        api_client.transition_task.return_value = {}

        assert await dispatch_todo_tasks(api_client, redis_client) == 1

        message = redis_client.publish_message.await_args.args[1]
        assert message.story_id is None
        assert message.branch is None
        api_client.abort_paid_run_pre_handoff.assert_not_awaited()


class TestDispatchPartialFailure:
    """Dispatch is three non-atomic steps; a failure must not leave debris."""

    @staticmethod
    def _todo_task(**overrides):
        return _task(
            id="task-1",
            title="Add user model",
            description="Create User SQLAlchemy model",
            type="feature",
            project_id=PROJ_ID,
            story_id="story-1",
            status="todo",
            **overrides,
        )

    @pytest.mark.asyncio
    async def test_publish_failure_keeps_the_run_owned_for_recovery(self, api_client, redis_client):
        """A lost publish response is not evidence that a worker did not start."""
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [self._todo_task()]
        api_client.get_task_events.return_value = []
        redis_client.publish_message.side_effect = RuntimeError("redis is down")

        dispatched = await dispatch_todo_tasks(api_client, redis_client)

        assert dispatched == 0
        api_client.abort_paid_run_pre_handoff.assert_not_awaited()
        # The task stays in todo until unfinished-run recovery confirms ownership.
        api_client.transition_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_recipient_failure_releases_the_pre_handoff_reservation(
        self, api_client, redis_client, monkeypatch
    ):
        """Recipient resolution is before queue handoff and must compensate its hold."""
        from src.tasks import task_dispatcher

        api_client.get_tasks_by_status.return_value = [self._todo_task()]
        api_client.get_task_events.return_value = []
        monkeypatch.setattr(
            task_dispatcher,
            "resolve_project_recipient",
            AsyncMock(side_effect=RuntimeError("recipient unavailable")),
        )

        assert await task_dispatcher.dispatch_todo_tasks(api_client, redis_client) == 0

        api_client.abort_paid_run_pre_handoff.assert_awaited_once()
        assert (
            api_client.abort_paid_run_pre_handoff.await_args.args[0]
            == api_client.admit_engineering_dispatch.return_value.run_id
        )
        redis_client.publish_message.assert_not_called()
        api_client.transition_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_transition_failure_is_retried_once(self, api_client, redis_client):
        """A flaky transition is retried in the same tick, without republishing."""
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [self._todo_task()]
        api_client.get_task_events.return_value = []
        api_client.transition_task.side_effect = [RuntimeError("api hiccup"), {}]

        dispatched = await dispatch_todo_tasks(api_client, redis_client)

        assert dispatched == 1
        api_client.admit_engineering_dispatch.assert_awaited_once()
        redis_client.publish_message.assert_called_once()
        assert api_client.transition_task.call_count == 2

    @staticmethod
    def _prior_run_from_patch(run_id: str, patch: dict):
        """Rebuild the run a compensating PATCH leaves in the API."""
        from _run_routing_factories import _make_run

        from shared.contracts.dto.run import RunType

        return _make_run(
            id=run_id,
            type=RunType.ENGINEERING,
            status=patch["status"],
            result=patch["result"],
            run_metadata=patch["run_metadata"],
        )

    @staticmethod
    def _prior_run(status, *, iteration: int = 0, result=None, pre_handoff_aborted: bool = False):
        from _run_routing_factories import _make_run

        from shared.contracts.dto.run import RunType

        return _make_run(
            id="eng-abc",
            type=RunType.ENGINEERING,
            status=status,
            result=result,
            run_metadata={
                "triggered_by": "dispatcher",
                "iteration": iteration,
                "pre_handoff_aborted": pre_handoff_aborted,
            },
        )

    async def test_next_tick_after_transition_failure_only_transitions(
        self, api_client, redis_client
    ):
        """This task's own live run means: finish the transition, dispatch nothing.

        The message went out on an earlier tick, so the tick counts it — the work
        is real and running — but it creates no second attempt for it.
        """
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [self._todo_task()]
        api_client.get_task_events.return_value = []
        api_client.admit_engineering_dispatch.return_value = _repair(
            EngineeringDispatchRepair.RECOVER_OWN_ATTEMPT
        )

        dispatched = await dispatch_todo_tasks(api_client, redis_client)

        assert dispatched == 1
        redis_client.publish_message.assert_not_called()
        api_client.transition_task.assert_called_once_with("task-1", "in_dev", "dispatcher")

    async def test_a_live_foreign_attempt_is_adopted_without_being_counted(
        self, api_client, redis_client
    ):
        """Somebody else's attempt still holds the branch: adopt it, dispatch nothing.

        The task leaves todo so nothing tries to dispatch it again, and the tick
        does not count it: this tick put no work behind it.
        """
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [self._todo_task()]
        api_client.get_task_events.return_value = []
        api_client.admit_engineering_dispatch.return_value = _repair(
            EngineeringDispatchRepair.ADOPT_LIVE_ATTEMPT
        )

        assert await dispatch_todo_tasks(api_client, redis_client) == 0

        redis_client.publish_message.assert_not_called()
        api_client.transition_task.assert_called_once_with("task-1", "in_dev", "dispatcher")

    async def test_next_tick_replays_completed_run_onto_task(self, api_client, redis_client):
        """Worker finished before the tick: replay its outcome, don't redispatch."""
        from shared.contracts.dto.run import RunStatus
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [self._todo_task()]
        api_client.get_task_events.return_value = []
        api_client.admit_engineering_dispatch.return_value = _repair(
            EngineeringDispatchRepair.REPLAY_FINISHED_RUN
        )
        api_client.get_run.return_value = self._prior_run(
            RunStatus.COMPLETED,
            result={"engineering_status": "done", "commit_sha": "abc123"},
        )

        dispatched = await dispatch_todo_tasks(api_client, redis_client)

        assert dispatched == 1
        api_client.get_run.assert_awaited_once_with("eng-abc")
        redis_client.publish_message.assert_not_called()
        assert [c[0][1] for c in api_client.transition_task.call_args_list] == [
            "in_dev",
            "in_ci",
            "testing",
            "done",
        ]

    async def test_next_tick_replays_failed_run_onto_task(self, api_client, redis_client):
        """A finished-and-failed run leaves the task failed, for the supervisor to retry."""
        from shared.contracts.dto.run import RunStatus
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [self._todo_task()]
        api_client.get_task_events.return_value = []
        api_client.admit_engineering_dispatch.return_value = _repair(
            EngineeringDispatchRepair.REPLAY_FINISHED_RUN
        )
        api_client.get_run.return_value = self._prior_run(
            RunStatus.FAILED, result={"engineering_status": "failed"}
        )

        await dispatch_todo_tasks(api_client, redis_client)

        redis_client.publish_message.assert_not_called()
        assert [c[0][1] for c in api_client.transition_task.call_args_list] == [
            "in_dev",
            "failed",
        ]

    async def test_next_tick_replays_gave_up_run_onto_task(self, api_client, redis_client):
        """A worker that gave up sends the task to human review, not back to the queue."""
        from shared.contracts.dto.run import RunStatus
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [self._todo_task()]
        api_client.get_task_events.return_value = []
        api_client.admit_engineering_dispatch.return_value = _repair(
            EngineeringDispatchRepair.REPLAY_FINISHED_RUN
        )
        api_client.get_run.return_value = self._prior_run(
            RunStatus.FAILED, result={"engineering_status": "gave_up"}
        )

        await dispatch_todo_tasks(api_client, redis_client)

        assert [c[0][1] for c in api_client.transition_task.call_args_list] == [
            "in_dev",
            "waiting_human_review",
        ]

    async def test_a_retried_task_the_point_admits_is_dispatched_normally(
        self, api_client, redis_client
    ):
        """A retry is an ordinary admitted dispatch on this side of the seam.

        Whether the task's own bumped `current_iteration` may hide a live run is
        decided inside the admission point, and pinned there:
        services/api/tests/service/test_engineering_dispatch_admission.py.
        """
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [self._todo_task(current_iteration=1)]
        api_client.get_task_events.return_value = []

        dispatched = await dispatch_todo_tasks(api_client, redis_client)

        assert dispatched == 1
        redis_client.publish_message.assert_called_once()
        api_client.transition_task.assert_called_once_with("task-1", "in_dev", "dispatcher")


class TestParseOwnerRepo:
    """Parse owner/repo from GitHub git URLs."""

    def test_https_url(self):
        from src.tasks.task_dispatcher import _parse_owner_repo

        assert _parse_owner_repo("https://github.com/my-org/my-repo") == ("my-org", "my-repo")

    def test_https_url_with_git_suffix(self):
        from src.tasks.task_dispatcher import _parse_owner_repo

        assert _parse_owner_repo("https://github.com/my-org/my-repo.git") == ("my-org", "my-repo")

    def test_token_url(self):
        from src.tasks.task_dispatcher import _parse_owner_repo

        url = "https://x-access-token:ghs_abc@github.com/my-org/my-repo.git"
        assert _parse_owner_repo(url) == ("my-org", "my-repo")

    def test_trailing_slash(self):
        from src.tasks.task_dispatcher import _parse_owner_repo

        assert _parse_owner_repo("https://github.com/org/repo/") == ("org", "repo")


class TestCompleteStories:
    """Complete stories when all tasks are done."""

    @pytest.mark.asyncio
    async def test_open_pr_is_resolved_again_over_multiple_teardown_ticks(
        self, api_client, redis_client
    ):
        """The GitHub resolver returns the same open PR until teardown finishes."""

        from src.tasks.task_dispatcher import complete_stories

        story = _story(id="story-1", project_id=PROJ_ID, title="Add weather API", pr_number=42)
        api_client.get_stories_by_status.side_effect = lambda status: (
            [story] if status == StoryStatus.IN_PROGRESS else []
        )
        api_client.get_tasks_by_story.return_value = [
            _task(id="task-1", status="done", story_id="story-1", project_id=PROJ_ID),
        ]
        api_client.get_primary_repository.return_value = _repo(project_id=PROJ_ID)
        github = AsyncMock()
        github.create_pull_request.return_value = {
            "number": 42,
            "node_id": "PR_existing",
            "head": {"ref": "story/story-1", "sha": STORY_HEAD_SHA},
        }
        github.get_ref_sha.return_value = STORY_HEAD_SHA
        github.enable_auto_merge.return_value = True

        with (
            patch("src.tasks.story_completion.GitHubAppClient", return_value=self_entering(github)),
            patch(
                "src.tasks.story_completion.finalize_story_worker_teardown",
                new_callable=AsyncMock,
                side_effect=[False, True],
            ),
        ):
            assert await complete_stories(api_client, redis_client) == 0
            api_client.transition_story.assert_not_awaited()
            assert await complete_stories(api_client, redis_client) == 1

        assert github.create_pull_request.await_count == 2
        github.get_pull_request.assert_not_awaited()
        assert [call.args[1] for call in api_client.update_story.await_args_list] == [
            {"pr_number": 42},
            {"pr_number": 42},
        ]
        api_client.transition_story.assert_awaited_once_with("story-1", "pr_review")
        redis_client.publish_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pr_merged_during_teardown_resumes_on_the_second_tick(
        self, api_client, redis_client
    ):
        """A same-head merged PR recovers no-commits instead of false quarantine."""

        from shared.clients.github import NoCommitsBetweenError
        from src.tasks.task_dispatcher import complete_stories

        story = _story(id="story-1", project_id=PROJ_ID, title="Add weather API")
        api_client.get_stories_by_status.side_effect = lambda status: (
            [story] if status == StoryStatus.IN_PROGRESS else []
        )
        api_client.get_tasks_by_story.return_value = [
            _task(id="task-1", status="done", story_id="story-1", project_id=PROJ_ID),
        ]
        api_client.get_primary_repository.return_value = _repo(project_id=PROJ_ID)

        async def persist_pr(_story_id, patch):
            if "pr_number" in patch:
                story.pr_number = patch["pr_number"]

        api_client.update_story.side_effect = persist_pr
        github = AsyncMock()
        github.get_ref_sha.return_value = STORY_HEAD_SHA
        github.create_pull_request.side_effect = [
            {
                "number": 42,
                "node_id": "PR_current",
                "head": {"ref": "story/story-1", "sha": STORY_HEAD_SHA},
            },
            NoCommitsBetweenError("No commits between main and story/story-1"),
        ]
        github.get_pull_request.return_value = {
            "number": 42,
            "node_id": "PR_current",
            "merged_at": "2026-09-13T15:00:00Z",
            "head": {"ref": "story/story-1", "sha": STORY_HEAD_SHA},
        }
        github.enable_auto_merge.return_value = True

        with (
            patch("src.tasks.story_completion.GitHubAppClient", return_value=self_entering(github)),
            patch(
                "src.tasks.story_completion.finalize_story_worker_teardown",
                new_callable=AsyncMock,
                side_effect=[False, True],
            ),
        ):
            assert await complete_stories(api_client, redis_client) == 0
            assert await complete_stories(api_client, redis_client) == 1

        github.get_pull_request.assert_awaited_once_with("my-org", "weather-bot", 42)
        assert [call.args[1] for call in api_client.update_story.await_args_list] == [
            {"pr_number": 42},
            {"pr_number": 42},
        ]
        api_client.transition_story.assert_awaited_once_with("story-1", "pr_review")
        assert not any(
            "quarantine_reason" in call.args[1] for call in api_client.update_story.await_args_list
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fix_kind", ["qa_fix", "deploy_fix"])
    async def test_fix_commits_resolve_and_persist_a_successor_to_the_stored_merged_pr(
        self, api_client, redis_client, fix_kind
    ):
        """A merged PR number is poller output, never authority over new branch state."""

        from src.tasks.task_dispatcher import complete_stories

        story = _story(id="story-1", project_id=PROJ_ID, title="Repair weather API", pr_number=3)
        api_client.get_stories_by_status.side_effect = lambda status: (
            [story] if status == StoryStatus.IN_PROGRESS else []
        )
        api_client.get_tasks_by_story.return_value = [
            _task(id="task-1", status="done", story_id="story-1", project_id=PROJ_ID),
        ]
        api_client.get_primary_repository.return_value = _repo(project_id=PROJ_ID)
        api_client.list_runs.return_value = (
            [
                RunDTO(
                    id="completed-deploy-fix",
                    project_id=PROJ_ID,
                    type=RunType.ENGINEERING,
                    status=RunStatus.CANCELLED,
                    story_id="story-1",
                    run_metadata={"deploy_fix_attempt": 1},
                    created_at=_NOW,
                )
            ]
            if fix_kind == "deploy_fix"
            else []
        )
        github = AsyncMock()
        github.get_ref_sha.return_value = STORY_HEAD_SHA
        github.create_pull_request.return_value = {
            "number": 5,
            "node_id": "PR_fix",
            "head": {"ref": "story/story-1", "sha": STORY_HEAD_SHA},
        }
        github.enable_auto_merge.return_value = True

        with (
            patch("src.tasks.story_completion.GitHubAppClient", return_value=self_entering(github)),
            patch(
                "src.tasks.story_completion.finalize_story_worker_teardown",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            assert await complete_stories(api_client, redis_client) == 1

        github.create_pull_request.assert_awaited_once()
        github.get_pull_request.assert_not_awaited()
        api_client.update_story.assert_awaited_once_with("story-1", {"pr_number": 5})
        github.enable_auto_merge.assert_awaited_once_with(
            "my-org", "weather-bot", pr_node_id="PR_fix"
        )
        api_client.transition_story.assert_awaited_once_with("story-1", "pr_review")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "failure_stage", ["missing_branch", "resolve", "ambiguous_response", "teardown"]
    )
    async def test_completion_failure_never_transitions_or_triggers_later_work(
        self, api_client, redis_client, failure_stage
    ):

        from src.tasks.task_dispatcher import complete_stories

        story = _story(id="story-1", project_id=PROJ_ID, title="Add weather API")
        api_client.get_stories_by_status.side_effect = lambda status: (
            [story] if status == StoryStatus.IN_PROGRESS else []
        )
        api_client.get_tasks_by_story.return_value = [
            _task(id="task-1", status="done", story_id="story-1", project_id=PROJ_ID),
        ]
        api_client.get_primary_repository.return_value = _repo(project_id=PROJ_ID)
        github = AsyncMock()
        github.get_ref_sha.return_value = (
            None if failure_stage == "missing_branch" else STORY_HEAD_SHA
        )
        if failure_stage == "resolve":
            github.create_pull_request.side_effect = RuntimeError("GitHub unavailable")
        elif failure_stage == "ambiguous_response":
            github.create_pull_request.return_value = {
                "node_id": "PR_without_number",
                "head": {"ref": "story/story-1", "sha": STORY_HEAD_SHA},
            }
        else:
            github.create_pull_request.return_value = {
                "number": 42,
                "node_id": "PR_new",
                "head": {"ref": "story/story-1", "sha": STORY_HEAD_SHA},
            }
            github.enable_auto_merge.return_value = True

        with (
            patch("src.tasks.story_completion.GitHubAppClient", return_value=self_entering(github)),
            patch(
                "src.tasks.story_completion.finalize_story_worker_teardown",
                new_callable=AsyncMock,
                return_value=failure_stage != "teardown",
            ) as finalize,
        ):
            assert await complete_stories(api_client, redis_client) == 0

        api_client.transition_story.assert_not_awaited()
        redis_client.publish_message.assert_not_awaited()
        if failure_stage in {"missing_branch", "resolve", "ambiguous_response"}:
            finalize.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_auto_merge_refusal_still_finalizes_teardown_and_handoff(
        self, api_client, redis_client
    ):
        """A visible open PR must not retain the story's project worker forever."""

        from src.tasks.task_dispatcher import complete_stories

        story = _story(id="story-1", project_id=PROJ_ID, title="Add weather API")
        api_client.get_stories_by_status.side_effect = lambda status: (
            [story] if status == StoryStatus.IN_PROGRESS else []
        )
        api_client.get_tasks_by_story.return_value = [
            _task(id="task-1", status="done", story_id="story-1", project_id=PROJ_ID),
        ]
        api_client.get_primary_repository.return_value = _repo(project_id=PROJ_ID)
        github = AsyncMock()
        github.get_ref_sha.return_value = STORY_HEAD_SHA
        github.create_pull_request.return_value = {
            "number": 42,
            "node_id": "PR_new",
            "head": {"ref": "story/story-1", "sha": STORY_HEAD_SHA},
        }
        github.enable_auto_merge.return_value = False

        with (
            patch("src.tasks.story_completion.GitHubAppClient", return_value=self_entering(github)),
            patch(
                "src.tasks.story_completion.finalize_story_worker_teardown",
                new_callable=AsyncMock,
                return_value=True,
            ) as finalize,
        ):
            assert await complete_stories(api_client, redis_client) == 1

        finalize.assert_awaited_once()
        api_client.transition_story.assert_awaited_once_with("story-1", "pr_review")

    @pytest.mark.asyncio
    async def test_auto_merge_refusal_persists_pr_for_the_poller_handoff(
        self, api_client, redis_client
    ):
        from src.tasks.task_dispatcher import complete_stories

        story = _story(id="story-1", project_id=PROJ_ID, title="Add weather API")
        api_client.get_stories_by_status.return_value = [story]
        api_client.get_tasks_by_story.return_value = [
            _task(id="task-1", status="done", story_id="story-1", project_id=PROJ_ID),
        ]
        api_client.get_primary_repository.return_value = _repo(project_id=PROJ_ID)
        github = AsyncMock()
        github.get_ref_sha.return_value = STORY_HEAD_SHA
        github.create_pull_request.return_value = {
            "number": 42,
            "node_id": "PR_new",
            "head": {"ref": "story/story-1", "sha": STORY_HEAD_SHA},
        }
        github.enable_auto_merge.return_value = False

        with (
            patch("src.tasks.story_completion.GitHubAppClient", return_value=self_entering(github)),
            patch(
                "src.tasks.story_completion.finalize_story_worker_teardown",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            assert await complete_stories(api_client, redis_client) == 1

        api_client.update_story.assert_awaited_once_with("story-1", {"pr_number": 42})
        api_client.transition_story.assert_awaited_once_with("story-1", "pr_review")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("run_status", [RunStatus.QUEUED, RunStatus.RUNNING])
    async def test_does_not_complete_while_deploy_fix_engineering_run_is_live(
        self, api_client, redis_client, run_status
    ):
        """A taskless deploy-fix still owns the story branch and its worker."""

        from src.tasks.task_dispatcher import complete_stories

        api_client.get_stories_by_status.return_value = [
            _story(id="story-1", project_id=PROJ_ID, title="Add weather API")
        ]
        api_client.get_tasks_by_story.return_value = [
            _task(id="task-1", status="done", story_id="story-1", project_id=PROJ_ID),
        ]
        api_client.list_runs.return_value = [
            RunDTO(
                id=f"deploy-fix-{run_status.value}",
                project_id=PROJ_ID,
                type=RunType.ENGINEERING,
                status=run_status,
                story_id="story-1",
                run_metadata={"deploy_fix_attempt": 1},
                created_at=_NOW,
            )
        ]

        with patch("src.tasks.story_completion.GitHubAppClient") as github:
            completed = await complete_stories(api_client, redis_client)

        assert completed == 0
        api_client.list_runs.assert_awaited_once_with(
            story_id="story-1", run_type=RunType.ENGINEERING.value
        )
        api_client.transition_story.assert_not_called()
        redis_client.redis.hget.assert_not_called()
        github.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "run",
        [
            RunDTO(
                id="completed-deploy-fix",
                project_id=PROJ_ID,
                type=RunType.ENGINEERING,
                status=RunStatus.CANCELLED,
                story_id="story-1",
                run_metadata={"deploy_fix_attempt": 1},
                created_at=_NOW,
            ),
            RunDTO(
                id="ordinary-engineering",
                project_id=PROJ_ID,
                type=RunType.ENGINEERING,
                status=RunStatus.QUEUED,
                story_id="story-1",
                run_metadata={},
                created_at=_NOW,
            ),
            RunDTO(
                id="queued-deploy",
                project_id=PROJ_ID,
                type=RunType.DEPLOY,
                status=RunStatus.QUEUED,
                story_id="story-1",
                run_metadata={"deploy_fix_attempt": 1},
                created_at=_NOW,
            ),
        ],
    )
    async def test_completes_when_story_run_is_not_a_live_deploy_fix(
        self, api_client, redis_client, run
    ):
        """Historical fixes and ordinary engineering runs do not delay completion."""

        from src.tasks.task_dispatcher import complete_stories

        api_client.get_stories_by_status.return_value = [
            _story(id="story-1", project_id=PROJ_ID, title="Add weather API")
        ]
        api_client.get_tasks_by_story.return_value = [
            _task(id="task-1", status="done", story_id="story-1", project_id=PROJ_ID),
        ]
        api_client.list_runs.return_value = [run]
        api_client.get_primary_repository.return_value = _repo(project_id=PROJ_ID)

        github = AsyncMock()
        github.get_ref_sha.return_value = STORY_HEAD_SHA
        github.create_pull_request.return_value = {
            "number": 42,
            "node_id": "PR_abc",
            "head": {"ref": "story/story-1", "sha": STORY_HEAD_SHA},
        }
        github.enable_auto_merge.return_value = True
        with patch(
            "src.tasks.story_completion.GitHubAppClient", return_value=self_entering(github)
        ):
            completed = await complete_stories(api_client, redis_client)

        assert completed == 1
        api_client.transition_story.assert_awaited_once_with("story-1", "pr_review")

    @pytest.mark.asyncio
    async def test_completes_story_creates_pr_when_all_tasks_done(self, api_client, redis_client):
        """Story with all tasks done -> creates PR, enables auto-merge, transitions to pr_review."""

        from src.tasks.task_dispatcher import complete_stories

        api_client.get_stories_by_status.return_value = [
            _story(id="story-1", project_id=PROJ_ID, title="Add weather API")
        ]
        api_client.get_tasks_by_story.return_value = [
            _task(id="task-1", status="done", story_id="story-1", project_id=PROJ_ID),
            _task(id="task-2", status="done", story_id="story-1", project_id=PROJ_ID),
        ]
        api_client.get_primary_repository.return_value = _repo(
            id="repo-1",
            name="weather-bot",
            git_url="https://github.com/my-org/weather-bot",
            project_id=PROJ_ID,
        )
        api_client.transition_story.return_value = {}

        mock_github = AsyncMock()
        mock_github.create_pull_request.return_value = {
            "number": 42,
            "node_id": "PR_abc",
            "html_url": "https://github.com/my-org/weather-bot/pull/42",
            "head": {"ref": "story/story-1", "sha": STORY_HEAD_SHA},
        }
        mock_github.get_ref_sha.return_value = STORY_HEAD_SHA
        redis_client.redis.hget.return_value = b"dev-story-worker"
        redis_client.redis.hget.side_effect = lambda key, *args: (
            b"dev-story-worker" if key == "story:workers" else None
        )
        redis_client.redis.hgetall.return_value = {}
        redis_client.redis.eval.return_value = 1
        redis_client.redis.get.return_value = None
        mock_github.enable_auto_merge.return_value = True

        with patch(
            "src.tasks.story_completion.GitHubAppClient", return_value=self_entering(mock_github)
        ):
            await complete_stories(api_client, redis_client)

        # Should transition story to pr_review (not deploying)
        api_client.transition_story.assert_called_once_with("story-1", "pr_review")

        # Should create PR from story branch to main
        mock_github.create_pull_request.assert_called_once_with(
            "my-org",
            "weather-bot",
            head="story/story-1",
            base="main",
            title="Add weather API",
            body="All tasks completed. Auto-merge enabled.",
        )

        # Should enable auto-merge
        mock_github.enable_auto_merge.assert_called_once_with(
            "my-org", "weather-bot", pr_node_id="PR_abc"
        )

        # Should NOT publish deploy message (webhook handles it after merge)
        deploy_calls = [
            c for c in redis_client.publish_message.call_args_list if "deploy" in str(c).lower()
        ]
        assert len(deploy_calls) == 0

    @pytest.mark.asyncio
    async def test_no_complete_when_tasks_pending(self, api_client, redis_client):
        """Story with pending tasks -> no action."""
        from src.tasks.task_dispatcher import complete_stories

        api_client.get_stories_by_status.return_value = [_story(id="story-1", project_id=PROJ_ID)]
        api_client.get_tasks_by_story.return_value = [
            _task(id="task-1", status="done", story_id="story-1", project_id=PROJ_ID),
            _task(id="task-2", status="in_dev", story_id="story-1", project_id=PROJ_ID),
        ]

        await complete_stories(api_client, redis_client)

        api_client.transition_story.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_complete_when_no_tasks(self, api_client, redis_client):
        """Story with zero tasks -> no action (architect may not have run yet)."""
        from src.tasks.task_dispatcher import complete_stories

        api_client.get_stories_by_status.return_value = [_story(id="story-1", project_id=PROJ_ID)]
        api_client.get_tasks_by_story.return_value = []

        await complete_stories(api_client, redis_client)

        api_client.transition_story.assert_not_called()

    @pytest.mark.asyncio
    async def test_pr_already_merged_transitions_to_pr_review(self, api_client, redis_client):
        """PR already merged (QA fix cycle) -> transition to pr_review for poller.

        When a PR is already merged (e.g. QA fix task pushed commits and PR
        auto-merged while story was in_progress), complete_stories transitions
        to pr_review so poll_merged_prs() can detect the merge and trigger deploy.
        """

        from src.tasks.task_dispatcher import complete_stories

        api_client.get_stories_by_status.return_value = [
            _story(id="story-1", project_id=PROJ_ID, title="Add weather API")
        ]
        api_client.get_tasks_by_story.return_value = [
            _task(id="task-1", status="done", story_id="story-1", project_id=PROJ_ID),
        ]
        api_client.get_primary_repository.return_value = _repo(
            id="repo-1",
            git_url="https://github.com/my-org/weather-bot",
            project_id=PROJ_ID,
        )

        mock_github = AsyncMock()
        # PR already merged (e.g., QA fix cycle)
        mock_github.create_pull_request.return_value = {
            "number": 42,
            "node_id": "PR_abc",
            "merged_at": "2026-03-19T01:00:00Z",
            "head": {"ref": "story/story-1", "sha": STORY_HEAD_SHA},
        }
        mock_github.get_ref_sha.return_value = STORY_HEAD_SHA
        redis_client.redis.hget.return_value = b"dev-story-worker"
        redis_client.redis.hget.side_effect = lambda key, *args: (
            b"dev-story-worker" if key == "story:workers" else None
        )
        redis_client.redis.hgetall.return_value = {}
        redis_client.redis.eval.return_value = 1
        redis_client.redis.get.return_value = None

        with patch(
            "src.tasks.story_completion.GitHubAppClient", return_value=self_entering(mock_github)
        ):
            result = await complete_stories(api_client, redis_client)

        # Must transition to pr_review so poller picks up the merge
        api_client.transition_story.assert_called_once_with("story-1", "pr_review")
        assert redis_client.publish.await_args.args[1]["worker_id"] == "dev-story-worker"
        redis_client.redis.hdel.assert_not_called()
        assert result == 1


class TestCompletionIgnoresCancelledTasks:
    """A cancelled task is not outstanding work, so it cannot be waited on."""

    @pytest.mark.asyncio
    async def test_recovered_story_completes_with_the_cancelled_corpse_present(
        self, api_client, redis_client
    ):
        """The property card 1243 exists for: a recovered plan can finish.

        A takeover voids the superseded attempt's unadmitted tasks — they are in
        nobody's release set, so nothing would ever dispatch them. Before this
        change those cancelled rows sat in the story for ever and `all(done)`
        could never hold, so a recovered story could never reach `pr_review`
        however well its new plan went.
        """

        from src.tasks.task_dispatcher import complete_stories

        api_client.get_stories_by_status.return_value = [
            _story(id="story-1", project_id=PROJ_ID, title="Recovered story")
        ]
        api_client.get_tasks_by_story.return_value = [
            # The corpse of the superseded plan: never admitted, now cancelled.
            _task(
                id="task-old",
                status="cancelled",
                story_id="story-1",
                project_id=PROJ_ID,
                dispatch_admitted=False,
            ),
            # The replacement architect's plan, admitted and finished.
            _task(id="task-new", status="done", story_id="story-1", project_id=PROJ_ID),
        ]
        api_client.get_primary_repository.return_value = _repo(
            id="repo-1",
            git_url="https://github.com/my-org/weather-bot",
            project_id=PROJ_ID,
        )

        mock_github = AsyncMock()
        mock_github.get_ref_sha.return_value = STORY_HEAD_SHA
        mock_github.create_pull_request.return_value = {
            "number": 7,
            "node_id": "PR_x",
            "head": {"ref": "story/story-1", "sha": STORY_HEAD_SHA},
        }
        mock_github.enable_auto_merge.return_value = True

        with patch(
            "src.tasks.story_completion.GitHubAppClient", return_value=self_entering(mock_github)
        ):
            completed = await complete_stories(api_client, redis_client)

        assert completed == 1
        api_client.transition_story.assert_called_once_with("story-1", "pr_review")

    @pytest.mark.asyncio
    async def test_a_cancelled_task_still_does_not_hide_unfinished_work(
        self, api_client, redis_client
    ):
        """Ignoring cancelled tasks is not ignoring the live ones."""
        from src.tasks.task_dispatcher import complete_stories

        api_client.get_stories_by_status.return_value = [_story(id="story-1", project_id=PROJ_ID)]
        api_client.get_tasks_by_story.return_value = [
            _task(id="task-1", status="cancelled", story_id="story-1", project_id=PROJ_ID),
            _task(id="task-2", status="done", story_id="story-1", project_id=PROJ_ID),
            _task(id="task-3", status="in_dev", story_id="story-1", project_id=PROJ_ID),
        ]

        await complete_stories(api_client, redis_client)

        api_client.transition_story.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_story_whose_tasks_are_all_cancelled_does_not_complete(
        self, api_client, redis_client
    ):
        """Nothing was built, so there is no branch to open a PR for."""
        from src.tasks.task_dispatcher import complete_stories

        api_client.get_stories_by_status.return_value = [_story(id="story-1", project_id=PROJ_ID)]
        api_client.get_tasks_by_story.return_value = [
            _task(id="task-1", status="cancelled", story_id="story-1", project_id=PROJ_ID),
            _task(id="task-2", status="cancelled", story_id="story-1", project_id=PROJ_ID),
        ]

        completed = await complete_stories(api_client, redis_client)

        assert completed == 0
        api_client.transition_story.assert_not_called()
        api_client.get_primary_repository.assert_not_called()


class TestSuperviseFailedTasks:
    """Supervisor retries failed tasks or escalates to WHR."""

    @pytest.mark.asyncio
    async def test_retries_failed_task_with_iterations_left(self, api_client, redis_client):
        """Failed task with retries left → retry (backlog → todo)."""
        from src.tasks.supervisor import supervise_failed_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(
                id="task-1",
                story_id="story-1",
                current_iteration=0,
                max_iterations=3,
                status="failed",
                project_id=PROJ_ID,
            )
        ]

        result = await supervise_failed_tasks(api_client, redis_client)

        assert result["retried"] == 1
        assert result["escalated"] == 0

    @pytest.mark.asyncio
    async def test_escalates_failed_task_retries_exhausted(self, api_client, redis_client):
        """Failed task with retries exhausted → waiting_human_review."""
        from src.tasks.supervisor import supervise_failed_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(
                id="task-1",
                story_id="story-1",
                current_iteration=3,
                max_iterations=3,
                status="failed",
                project_id=PROJ_ID,
            )
        ]
        api_client.transition_task.return_value = {}
        api_client.transition_story.return_value = {}

        result = await supervise_failed_tasks(api_client, redis_client)

        assert result["retried"] == 0
        assert result["escalated"] == 1
        # Task should be transitioned to WHR
        api_client.transition_task.assert_called_once_with(
            "task-1", "waiting_human_review", "supervisor"
        )


class TestPollMergedPRs:
    """Poll GitHub for merged PRs on stories in pr_review."""

    @pytest.mark.asyncio
    async def test_triggers_create_deploy_for_first_story(self, api_client, redis_client):
        """First story merge -> action='create'."""

        from src.tasks.task_dispatcher import poll_merged_prs

        api_client.get_stories_by_status.return_value = [
            _story(id="story-1", project_id=PROJ_ID, status="pr_review", pr_number=42)
        ]
        api_client.get_primary_repository.return_value = _repo(
            id="repo-1",
            git_url="https://github.com/my-org/weather-bot",
            project_id=PROJ_ID,
        )
        # No completed stories — first deploy
        api_client.get_stories_by_project.return_value = [
            _story(id="story-1", project_id=PROJ_ID, status="pr_review", pr_number=42),
        ]
        api_client.transition_story.return_value = {}

        mock_github = AsyncMock()
        mock_github.get_latest_workflow_run.return_value = _published_ci_run()
        mock_github.get_pull_request.return_value = {
            "number": 42,
            "merged_at": "2026-03-16T12:00:00Z",
            "merge_commit_sha": "e" * 40,
            "head": {"sha": "a" * 40},
        }

        with patch("src.tasks.pr_poller.GitHubAppClient", return_value=self_entering(mock_github)):
            result = await poll_merged_prs(api_client, redis_client)

        assert result == 1
        api_client.transition_story.assert_called_once_with("story-1", "deploy")
        redis_client.publish_message.assert_called_once()

        deploy_msg = redis_client.publish_message.call_args[0][1]
        assert deploy_msg.project_id == PROJ_ID
        assert deploy_msg.story_id == "story-1"
        assert deploy_msg.action == "create"

    @pytest.mark.asyncio
    async def test_triggers_feature_deploy_when_previous_story_completed(
        self, api_client, redis_client
    ):
        """Project with a completed story -> action='feature'."""

        from src.tasks.task_dispatcher import poll_merged_prs

        api_client.get_stories_by_status.return_value = [
            _story(id="story-2", project_id=PROJ_ID, status="pr_review", pr_number=43)
        ]
        api_client.get_primary_repository.return_value = _repo(
            id="repo-1",
            git_url="https://github.com/my-org/weather-bot",
            project_id=PROJ_ID,
        )
        # Has a previously completed story
        api_client.get_stories_by_project.return_value = [
            _story(id="story-1", project_id=PROJ_ID, status="completed"),
            _story(id="story-2", project_id=PROJ_ID, status="pr_review", pr_number=43),
        ]
        api_client.transition_story.return_value = {}

        mock_github = AsyncMock()
        mock_github.get_latest_workflow_run.return_value = _published_ci_run()
        mock_github.get_pull_request.return_value = {
            "number": 43,
            "merged_at": "2026-03-16T13:00:00Z",
            "merge_commit_sha": "e" * 40,
            "head": {"sha": "d" * 40},
        }

        with patch("src.tasks.pr_poller.GitHubAppClient", return_value=self_entering(mock_github)):
            result = await poll_merged_prs(api_client, redis_client)

        assert result == 1
        deploy_msg = redis_client.publish_message.call_args[0][1]
        assert deploy_msg.action == "feature"

    @pytest.mark.asyncio
    async def test_no_action_when_pr_not_merged(self, api_client, redis_client):
        """Story in pr_review with open (not merged) PR -> no action."""

        from src.tasks.task_dispatcher import poll_merged_prs

        api_client.get_stories_by_status.return_value = [
            _story(id="story-1", project_id=PROJ_ID, status="pr_review", pr_number=42)
        ]
        api_client.get_primary_repository.return_value = _repo(
            id="repo-1",
            git_url="https://github.com/my-org/weather-bot",
            project_id=PROJ_ID,
        )

        mock_github = AsyncMock()
        mock_github.get_latest_workflow_run.return_value = _published_ci_run()
        mock_github.get_pull_request.return_value = {
            "number": 42,
            "merged_at": None,
            "head": {"sha": "a" * 40},
        }

        with patch("src.tasks.pr_poller.GitHubAppClient", return_value=self_entering(mock_github)):
            result = await poll_merged_prs(api_client, redis_client)

        assert result == 0
        api_client.transition_story.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_action_when_no_stories_in_pr_review(self, api_client, redis_client):
        """No stories in pr_review -> nothing to poll."""
        from src.tasks.task_dispatcher import poll_merged_prs

        api_client.get_stories_by_status.return_value = []

        result = await poll_merged_prs(api_client, redis_client)

        assert result == 0

    @pytest.mark.asyncio
    async def test_continues_on_github_error(self, api_client, redis_client):
        """GitHub API error for one story doesn't block others."""

        from src.tasks.task_dispatcher import poll_merged_prs

        proj2_id = "00000000-0000-0000-0000-000000000002"
        api_client.get_stories_by_status.return_value = [
            _story(id="story-1", project_id=PROJ_ID, status="pr_review", pr_number=9),
            _story(id="story-2", project_id=proj2_id, status="pr_review", pr_number=10),
        ]
        api_client.get_primary_repository.side_effect = [
            _repo(
                id="repo-1",
                git_url="https://github.com/my-org/repo1",
                project_id=PROJ_ID,
            ),
            _repo(
                id="repo-2",
                git_url="https://github.com/my-org/repo2",
                project_id=proj2_id,
            ),
        ]
        # First story for this project → action=create
        api_client.get_stories_by_project.return_value = [
            _story(id="story-2", project_id=proj2_id, status="pr_review", pr_number=10),
        ]
        api_client.transition_story.return_value = {}

        mock_github = AsyncMock()
        mock_github.get_latest_workflow_run.return_value = _published_ci_run()
        mock_github.get_pull_request.side_effect = [
            Exception("GitHub API error"),
            {
                "number": 10,
                "merged_at": "2026-03-16T12:00:00Z",
                "merge_commit_sha": "e" * 40,
                "head": {"sha": "d" * 40},
            },
        ]

        with patch("src.tasks.pr_poller.GitHubAppClient", return_value=self_entering(mock_github)):
            result = await poll_merged_prs(api_client, redis_client)

        assert result == 1
        api_client.transition_story.assert_called_once_with("story-2", "deploy")


class _RefusalWorld:
    """The API and stream a paid refusal's owner notification actually meets.

    The pieces the delivery consults are real — the story whose status the
    record is checked against, the project and user the recipient comes from,
    and both places a record can live — so a test can tell a message that was
    published from one that was only intended.
    """

    OWNER_USER_ID = 4242
    OWNER_CHAT_ID = "900004242"

    def __init__(self, api_client, *, initiating_run: RunDTO | None):
        from unittest.mock import AsyncMock as _AsyncMock

        self.story = _story(id="story-1", project_id=PROJ_ID, status="in_progress")
        self.initiating_run = initiating_run
        self.story_record: dict | None = None
        self.run_records: list[tuple[str, dict]] = []
        self.published: list[dict] = []
        self.task_transitions: list[tuple[tuple, dict]] = []
        api_client.get_run = _AsyncMock(side_effect=self._get_run)
        api_client.get_story = _AsyncMock(side_effect=self._get_story)
        api_client.transition_story = _AsyncMock(side_effect=self._transition_story)
        api_client.transition_task = _AsyncMock(side_effect=self._transition_task)
        api_client.update_story_owner_notification = _AsyncMock(side_effect=self._write_story)
        api_client.update_run = _AsyncMock(side_effect=self._write_run)
        api_client.get_project = _AsyncMock(return_value=self._project())
        api_client.get_user = _AsyncMock(return_value=self._owner())
        api_client.claim_story_owner_notification_attempt = _AsyncMock(
            side_effect=self._claim_story
        )
        api_client.claim_run_owner_notification_attempt = _AsyncMock(side_effect=self._claim_run)
        self.clock = ClaimClock()

    async def _claim_story(self, story_id: str):
        assert story_id == self.story.id
        return claim(
            self.clock,
            lambda: self.story_record,
            lambda stamped: setattr(self, "story_record", stamped),
        )

    async def _claim_run(self, run_id: str):
        assert self.initiating_run is not None and run_id == self.initiating_run.id

        def stamp(stamped: dict) -> None:
            self.initiating_run = self.initiating_run.model_copy(
                update={"run_metadata": {"owner_notification": stamped}}
            )

        return claim(
            self.clock, lambda: self.initiating_run.run_metadata.get("owner_notification"), stamp
        )

    def _project(self):
        from uuid import UUID

        from shared.contracts.dto.project import ProjectDTO, ProjectStatus

        return ProjectDTO(
            id=UUID(PROJ_ID),
            initiating_run_id="po-1e07a3205c84",
            title="Test Project",
            slug="test-project",
            status=ProjectStatus.ACTIVE,
            config={"workspace_ready": True},
            owner_id=self.OWNER_USER_ID,
            created_at=_NOW,
        )

    def _owner(self):
        from shared.contracts.dto.user import UserDTO

        return UserDTO(
            id=self.OWNER_USER_ID,
            telegram_id=int(self.OWNER_CHAT_ID),
            is_admin=False,
            created_at=_NOW,
        )

    async def _get_run(self, run_id: str) -> RunDTO:
        if self.initiating_run is not None and run_id == self.initiating_run.id:
            return self.initiating_run
        raise _not_found_error(f"runs/{run_id}")

    async def _get_story(self, story_id: str) -> StoryDTO:
        assert story_id == self.story.id
        return self.story

    async def _transition_story(self, story_id: str, action: str):
        assert (story_id, action) == (self.story.id, "human-review")
        self.story = self.story.model_copy(update={"status": StoryStatus.WAITING_HUMAN_REVIEW})
        return self.story

    async def _transition_task(self, task_id, status, actor, **kwargs):
        self.task_transitions.append(((task_id, status, actor), kwargs))
        return {}

    async def _write_story(self, story_id: str, record: dict) -> None:
        assert story_id == self.story.id
        self.story_record = record

    async def _write_run(self, run_id: str, data: dict) -> None:
        record = data["run_metadata"]["owner_notification"]
        self.run_records.append((run_id, record))
        assert self.initiating_run is not None
        self.initiating_run = self.initiating_run.model_copy(
            update={"run_metadata": {"owner_notification": record}}
        )

    def redis(self):
        from unittest.mock import AsyncMock as _AsyncMock

        client = _AsyncMock()
        client.publish_flat = _AsyncMock(side_effect=self._publish_flat)
        return client

    async def _publish_flat(self, queue: str, fields: dict) -> None:
        from shared.queues import PO_INPUT_QUEUE

        assert queue == PO_INPUT_QUEUE
        self.published.append(fields)

    @property
    def owner_message(self) -> dict:
        assert len(self.published) == 1, self.published
        return self.published[0]


def _not_found_error(path: str):
    """The error the API client raises for a GET that does not resolve."""
    import httpx

    request = httpx.Request("GET", f"http://api/{path}")
    return httpx.HTTPStatusError(
        "404 Not Found", request=request, response=httpx.Response(404, request=request)
    )


class TestRefusalWithoutARun:
    """A paid refusal parks its story whether or not a Run initiated it."""

    @staticmethod
    def _todo_task():
        return _task(id="task-1", project_id=PROJ_ID, story_id="story-1", status="todo")

    @pytest.mark.asyncio
    async def test_a_refusal_whose_initiator_is_not_a_run_still_parks_the_story(self, api_client):
        """A project born from a PO brief has a request id, not a Run, behind it.

        Nothing dispatched this work, so there is no Run to hang the record on
        and the story carries it instead — the same place the PR poller puts one.
        The owner still hears why their story stopped.
        """
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        world = _RefusalWorld(api_client, initiating_run=None)
        redis_client = world.redis()
        api_client.get_tasks_by_status.return_value = [self._todo_task()]
        api_client.get_task_events.return_value = []
        api_client.admit_engineering_dispatch.return_value = _paid_refusal(
            EngineeringDispatchRefusal.PAID_WORK_LIMIT,
            message="Your plan does not cover more work on this story.",
            initiating_run_id="po-1e07a3205c84",
        )

        assert await dispatch_todo_tasks(api_client, redis_client) == 0

        assert world.story.status is StoryStatus.WAITING_HUMAN_REVIEW
        message = world.owner_message
        assert message["story_id"] == "story-1"
        assert message["telegram_chat_id"] == world.OWNER_CHAT_ID
        assert message["text"] == "Your plan does not cover more work on this story."
        # Owed on the story and settled there: no Run was invented to hold it.
        assert world.story_record["state"] == "delivered"
        assert world.run_records == []
        # The rest of the refusal ran: the task is out of todo and in review.
        assert [call[0] for call in world.task_transitions] == [
            ("task-1", "in_dev", "dispatcher"),
            ("task-1", "waiting_human_review", "dispatcher"),
        ]

    @pytest.mark.asyncio
    async def test_a_refusal_initiated_by_a_real_run_keeps_the_run_backed_record(self, api_client):
        """A Run that exists is still where its own refusal is recorded."""
        from _run_routing_factories import _make_run

        from src.tasks.task_dispatcher import dispatch_todo_tasks

        run = _make_run(
            id="live-run-1", project_id=PROJ_ID, type=RunType.ENGINEERING, status=RunStatus.RUNNING
        )
        world = _RefusalWorld(api_client, initiating_run=run)
        redis_client = world.redis()
        api_client.get_tasks_by_status.return_value = [self._todo_task()]
        api_client.get_task_events.return_value = []
        api_client.admit_engineering_dispatch.return_value = _paid_refusal(
            EngineeringDispatchRefusal.PAID_WORK_LIMIT,
            message="Your plan does not cover more work on this story.",
        )

        assert await dispatch_todo_tasks(api_client, redis_client) == 0

        assert world.story.status is StoryStatus.WAITING_HUMAN_REVIEW
        assert world.owner_message["story_id"] == "story-1"
        assert world.story_record is None
        assert [run_id for run_id, _ in world.run_records] == ["live-run-1", "live-run-1"]
        assert world.run_records[-1][1]["state"] == "delivered"

    @pytest.mark.asyncio
    async def test_one_task_that_raises_does_not_skip_the_rest_of_the_cycle(
        self, api_client, redis_client
    ):
        """A cycle serves every candidate; one broken task is not a cycle failure."""
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(id="task-1", project_id=PROJ_ID, story_id="story-1", status="todo"),
            _task(id="task-2", project_id=PROJ_ID, story_id="story-2", status="todo"),
        ]
        api_client.get_task_events.return_value = []
        api_client.get_story.return_value = _story(id="story-2", project_id=PROJ_ID)
        api_client.admit_engineering_dispatch.side_effect = [
            _paid_refusal(
                EngineeringDispatchRefusal.PAID_WORK_LIMIT,
                message="Your plan does not cover more work on this story.",
            ),
            _admitted(),
        ]
        api_client.get_run.side_effect = RuntimeError("api unavailable")

        assert await dispatch_todo_tasks(api_client, redis_client) == 1

        redis_client.publish_message.assert_called_once()
        assert redis_client.publish_message.call_args[0][1].task_id == "eng-test"
