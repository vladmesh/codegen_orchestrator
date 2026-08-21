"""Fenced worker-turn observation and teardown reconciliation.

This deliberately contains the lifecycle protocol, leaving ``supervisor`` to
schedule it alongside unrelated story, deploy and access supervision.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

import structlog

from shared.contracts.dto.engineering import EngineeringStatus
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.run_result import EngineeringRunResult
from shared.contracts.dto.task import TaskStatus
from shared.contracts.dto.worker import WORKER_TERMINAL_STATUSES, WorkerStatus
from shared.contracts.queues.worker import DeleteWorkerCommand
from shared.contracts.queues.worker_result import WorkerStopReason
from shared.contracts.worker_evidence import RemovedWorkerEvidence, removed_worker_evidence_key
from shared.contracts.worker_turn import AttemptTurnMetadata, WorkerActiveTurn, active_turn_key
from shared.queues import WORKER_COMMANDS
from shared.redis import decode_redis_fields, decode_redis_value
from shared.redis_client import RedisStreamClient

logger = structlog.get_logger(__name__)

LIVE_ENGINEERING_STATUSES = (RunStatus.QUEUED, RunStatus.RUNNING)


class WorkerAttemptState(StrEnum):
    RUNNING = "running"
    IDLE = "idle"
    DEAD = "dead"
    TIMED_OUT = "timed_out"
    REMOVED = "removed"
    UNKNOWN = "unknown"


def turn_backstop_expired(metadata: AttemptTurnMetadata, now: datetime) -> bool:
    return bool(
        metadata.active_turn_requested_at
        and metadata.active_turn_backstop_seconds
        and now
        >= metadata.active_turn_requested_at
        + timedelta(seconds=metadata.active_turn_backstop_seconds)
    )


def _unknown_or_timed_out(metadata: AttemptTurnMetadata, now: datetime) -> WorkerAttemptState:
    return (
        WorkerAttemptState.TIMED_OUT
        if turn_backstop_expired(metadata, now)
        else WorkerAttemptState.UNKNOWN
    )


async def attempt_state(
    redis_client: RedisStreamClient, run: Any, now: datetime
) -> tuple[WorkerAttemptState, str | None]:
    """Classify one attempt without treating unavailable Redis as Docker proof."""
    try:
        metadata = AttemptTurnMetadata.from_run_metadata(run.run_metadata)
    except Exception:
        return WorkerAttemptState.UNKNOWN, None
    worker_id = metadata.worker_id
    if not worker_id:
        return (_unknown_or_timed_out(metadata, now), None)
    redis = redis_client.redis

    if metadata.initiating_run_id:
        try:
            raw = await redis.hget(
                removed_worker_evidence_key(metadata.initiating_run_id), worker_id
            )
            if raw:
                evidence = RemovedWorkerEvidence.model_validate_json(decode_redis_value(raw))
                if evidence.ownership.attempt_id == run.id:
                    return WorkerAttemptState.REMOVED, worker_id
        except Exception:
            return _unknown_or_timed_out(metadata, now), worker_id

    try:
        raw_status = await redis.hget(f"worker:status:{worker_id}", "status")
        status = decode_redis_value(raw_status)
    except Exception:
        return _unknown_or_timed_out(metadata, now), worker_id
    if status is None:
        return _unknown_or_timed_out(metadata, now), worker_id
    try:
        if WorkerStatus(status) in WORKER_TERMINAL_STATUSES:
            return WorkerAttemptState.DEAD, worker_id
    except ValueError:
        return _unknown_or_timed_out(metadata, now), worker_id

    try:
        active = WorkerActiveTurn.from_redis_fields(
            decode_redis_fields(await redis.hgetall(active_turn_key(worker_id)))
        )
    except Exception:
        return _unknown_or_timed_out(metadata, now), worker_id
    if (
        active
        and active.attempt_id == run.id
        and active.request_id == metadata.active_turn_request_id
    ):
        return (
            WorkerAttemptState.TIMED_OUT
            if now >= active.deadline_at
            else WorkerAttemptState.RUNNING,
            worker_id,
        )
    return (
        _unknown_or_timed_out(metadata, now)
        if turn_backstop_expired(metadata, now)
        else WorkerAttemptState.IDLE,
        worker_id,
    )


async def select_live_engineering_run(api_client: Any, task_id: str) -> Any | None:
    runs = await api_client.list_runs(task_id=task_id, run_type=RunType.ENGINEERING.value)
    return next((run for run in runs if run.status in LIVE_ENGINEERING_STATUSES), None)


async def select_terminal_engineering_run(api_client: Any, task_id: str) -> Any | None:
    runs = await api_client.list_runs(task_id=task_id, run_type=RunType.ENGINEERING.value)
    terminal = [run for run in runs if run.status not in LIVE_ENGINEERING_STATUSES]
    return max(terminal, key=lambda run: run.created_at) if terminal else None


def terminal_task_statuses(run: Any) -> tuple[TaskStatus, ...]:
    if run.status == RunStatus.COMPLETED:
        return (TaskStatus.IN_CI, TaskStatus.TESTING, TaskStatus.DONE)
    if (
        run.status == RunStatus.FAILED
        and run.result.engineering_status == EngineeringStatus.GAVE_UP
    ):
        return (TaskStatus.WAITING_HUMAN_REVIEW,)
    return (TaskStatus.FAILED,)


async def replay_terminal_attempt(api_client: Any, task_id: str, run: Any, actor: str) -> None:
    """Apply a terminal run's already-recorded outcome without changing the run."""
    for status in terminal_task_statuses(run):
        await api_client.transition_task(task_id, status, actor)


async def fail_removed_attempt(api_client: Any, task: Any, run: Any) -> None:
    """Close a still-live attempt only after confirmed worker removal."""
    await api_client.update_run(
        run.id,
        {
            "status": RunStatus.FAILED.value,
            "error_message": "engineering worker was removed after its turn timed out",
            "result": EngineeringRunResult(engineering_status=EngineeringStatus.FAILED).model_dump(
                mode="json"
            ),
            "run_metadata": AttemptTurnMetadata(
                stop_reason=WorkerStopReason.TURN_DEADLINE_EXCEEDED.value
            ).as_run_metadata(),
        },
    )
    await api_client.transition_task(task.id, TaskStatus.FAILED, "supervisor")


async def request_stuck_attempt_stop(
    api_client: Any,
    redis_client: RedisStreamClient,
    task: Any,
    run: Any,
    state: WorkerAttemptState,
    worker_id: str | None,
    now: datetime,
) -> bool:
    """Persist a retryable stop intent and re-drive it with capped backoff."""
    metadata = AttemptTurnMetadata.from_run_metadata(run.run_metadata)
    if metadata.worker_stop_next_retry_at and now < metadata.worker_stop_next_retry_at:
        return False
    attempts = (metadata.worker_stop_attempts or 0) + 1
    retry_seconds = min(30 * (2 ** min(attempts - 1, 4)), 300)
    patch = AttemptTurnMetadata(
        worker_stop_requested_at=metadata.worker_stop_requested_at or now,
        worker_stop_attempts=attempts,
        worker_stop_next_retry_at=now + timedelta(seconds=retry_seconds),
        stop_reason=WorkerStopReason.TURN_DEADLINE_EXCEEDED.value,
        worker_state=state.value,
    )
    await api_client.update_run(run.id, {"run_metadata": patch.as_run_metadata()})
    if worker_id:
        command = DeleteWorkerCommand(
            request_id=f"stuck-{task.id}-{attempts}", worker_id=worker_id, reason="timeout"
        )
        await redis_client.publish(WORKER_COMMANDS, command.model_dump(mode="json"))
        logger.warning("stuck_worker_stop_requested", worker_id=worker_id, attempts=attempts)
    return True
