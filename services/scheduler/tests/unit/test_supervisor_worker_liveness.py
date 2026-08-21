"""The supervisor reconciles a fenced lease and confirmed worker removal."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from _run_routing_factories import _make_run, _make_task
import pytest

from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.run_result import EngineeringRunResult
from shared.contracts.worker_turn import WorkerActiveTurn, active_turn_key

WORKER_ID = "dev-story-1"
RUN_ID = "eng-attempt-1"
OWNER_RUN = "live-run-1"


def _run(metadata: dict | None = None):
    return _make_run(
        id=RUN_ID,
        type=RunType.ENGINEERING,
        status=RunStatus.RUNNING,
        run_metadata={"worker_id": WORKER_ID, "initiating_run_id": OWNER_RUN, **(metadata or {})},
        created_at=datetime.now(UTC) - timedelta(hours=3),
    )


def _redis(*, active: WorkerActiveTurn | None = None, status: str | None = "RUNNING"):
    client = AsyncMock()
    client.redis = AsyncMock()

    async def hgetall(key):
        return active.as_redis_fields() if key == active_turn_key(WORKER_ID) and active else {}

    async def hget(key, field):
        return status if key == f"worker:status:{WORKER_ID}" else None

    client.redis.hgetall.side_effect = hgetall
    client.redis.hget.side_effect = hget
    client.publish = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_a_live_lease_survives_far_past_the_old_900_second_ceiling():
    now = datetime.now(UTC)
    active = WorkerActiveTurn(
        worker_id=WORKER_ID,
        attempt_id=RUN_ID,
        request_id="request-1",
        lease_id="1-0",
        started_at=now - timedelta(hours=2),
        deadline_at=now + timedelta(minutes=30),
    )
    api = AsyncMock()
    api.get_tasks_by_status.return_value = [_make_task(id="task-1", status="in_dev")]
    api.list_runs.return_value = [_run({"active_turn_request_id": "request-1"})]

    from src.tasks.supervisor import supervise_stuck_tasks

    assert await supervise_stuck_tasks(api, _redis(active=active)) == {
        "timed_out": 0,
        "working": 1,
        "stopping": 0,
    }
    api.update_run.assert_not_called()
    api.transition_task.assert_not_called()


@pytest.mark.asyncio
async def test_missing_redis_status_is_unknown_not_proof_that_docker_died():
    api = AsyncMock()
    api.get_tasks_by_status.return_value = [_make_task(id="task-1", status="in_dev")]
    api.list_runs.return_value = [_run()]

    from src.tasks.supervisor import supervise_stuck_tasks

    assert await supervise_stuck_tasks(api, _redis(status=None)) == {
        "timed_out": 0,
        "working": 0,
        "stopping": 0,
    }
    api.update_run.assert_not_called()
    api.transition_task.assert_not_called()


@pytest.mark.asyncio
async def test_terminal_worker_status_outranks_a_stale_active_lease():
    now = datetime.now(UTC)
    active = WorkerActiveTurn(
        worker_id=WORKER_ID,
        attempt_id=RUN_ID,
        request_id="request-1",
        lease_id="1-0",
        started_at=now - timedelta(minutes=5),
        deadline_at=now + timedelta(hours=1),
    )
    api = AsyncMock()
    api.get_tasks_by_status.return_value = [_make_task(id="task-1", status="in_dev")]
    api.list_runs.return_value = [_run({"active_turn_request_id": "request-1"})]

    from src.tasks.supervisor import supervise_stuck_tasks

    assert await supervise_stuck_tasks(api, _redis(active=active, status="DEAD")) == {
        "timed_out": 0,
        "working": 0,
        "stopping": 1,
    }
    api.transition_task.assert_not_called()


@pytest.mark.asyncio
async def test_timeout_only_requests_stop_until_worker_manager_records_removal():
    api = AsyncMock()
    api.get_tasks_by_status.return_value = [_make_task(id="task-1", status="in_dev")]
    api.list_runs.return_value = [
        _run(
            {
                "active_turn_requested_at": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
                "active_turn_backstop_seconds": 4500,
            }
        )
    ]

    from src.tasks.supervisor import supervise_stuck_tasks

    result = await supervise_stuck_tasks(api, _redis())
    assert result == {"timed_out": 0, "working": 0, "stopping": 1}
    api.transition_task.assert_not_called()
    patch = api.update_run.await_args.args[1]
    assert patch["run_metadata"]["worker_stop_requested_at"]
    assert patch["run_metadata"]["stop_reason"] == "turn_deadline_exceeded"


@pytest.mark.asyncio
async def test_missing_status_is_unknown_but_becomes_a_bounded_stop_request():
    api = AsyncMock()
    api.get_tasks_by_status.return_value = [_make_task(id="task-1", status="in_dev")]
    api.list_runs.return_value = [
        _run(
            {
                "active_turn_requested_at": (datetime.now(UTC) - timedelta(days=7)).isoformat(),
                "active_turn_backstop_seconds": 4500,
            }
        )
    ]

    from src.tasks.supervisor import supervise_stuck_tasks

    assert await supervise_stuck_tasks(api, _redis(status=None)) == {
        "timed_out": 0,
        "working": 0,
        "stopping": 1,
    }
    api.transition_task.assert_not_called()
    assert api.update_run.await_args.args[1]["run_metadata"]["worker_stop_attempts"] == 1


@pytest.mark.asyncio
async def test_failed_teardown_is_republished_after_the_durable_backoff():
    api = AsyncMock()
    task = _make_task(id="task-1", status="in_dev")
    run = _run(
        {
            "worker_stop_requested_at": (datetime.now(UTC) - timedelta(minutes=10)).isoformat(),
            "worker_stop_attempts": 1,
            "worker_stop_next_retry_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        }
    )
    from src.tasks.worker_liveness import WorkerAttemptState, request_stuck_attempt_stop

    redis_client = _redis()

    assert await request_stuck_attempt_stop(
        api,
        redis_client,
        task,
        run,
        WorkerAttemptState.TIMED_OUT,
        WORKER_ID,
        datetime.now(UTC),
    )
    patch = api.update_run.await_args.args[1]["run_metadata"]
    assert patch["worker_stop_attempts"] == 2
    redis_client.publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_terminal_run_is_replayed_without_clobbering_its_outcome():
    completed = _run()
    completed.status = RunStatus.COMPLETED
    completed.result = EngineeringRunResult(engineering_status="done", commit_sha="abc123")
    api = AsyncMock()
    api.get_tasks_by_status.return_value = [_make_task(id="task-1", status="in_dev")]
    api.list_runs.return_value = [completed]

    from src.tasks.supervisor import supervise_stuck_tasks

    assert await supervise_stuck_tasks(api, _redis()) == {
        "timed_out": 0,
        "working": 0,
        "stopping": 0,
    }
    api.update_run.assert_not_called()
    assert api.transition_task.await_count == 3


@pytest.mark.asyncio
async def test_deadline_without_a_recorded_worker_can_close_immediately():
    api = AsyncMock()
    api.get_tasks_by_status.return_value = [_make_task(id="task-1", status="in_dev")]
    run = _run(
        {
            "active_turn_requested_at": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
            "active_turn_backstop_seconds": 4500,
        }
    )
    run.run_metadata.pop("worker_id")
    api.list_runs.return_value = [run]

    from src.tasks.supervisor import supervise_stuck_tasks

    assert await supervise_stuck_tasks(api, _redis()) == {
        "timed_out": 1,
        "working": 0,
        "stopping": 0,
    }
    api.transition_task.assert_awaited_once()
