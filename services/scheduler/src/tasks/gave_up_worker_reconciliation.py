"""Worker teardown for engineering attempts that gave up."""

from __future__ import annotations

import structlog

from shared.contracts.dto.engineering import EngineeringStatus
from shared.contracts.dto.run import RunStatus, RunType
from shared.queues import STORY_WORKERS_KEY
from shared.redis import decode_redis_fields, decode_redis_value

from .story_worker_teardown import finalize_story_worker_teardown

logger = structlog.get_logger(__name__)

LIVE_RUN_STATUSES = (RunStatus.QUEUED, RunStatus.RUNNING)


def _gave_up(run) -> bool:
    return (
        run.type == RunType.ENGINEERING
        and run.status == RunStatus.FAILED
        and run.result.engineering_status == EngineeringStatus.GAVE_UP
    )


async def reconcile_gave_up_attempt_workers(api_client, redis_client) -> int:
    """Request canonical teardown for every worker a gave-up attempt left behind.

    The rule is keyed on the settled attempt, not on the story's status: a
    gave-up handler that crashed after settling its run leaves the story
    wherever it was, and the run is still the durable fact. A story's workers
    are torn down only while no run of that story is live and its newest
    engineering attempt gave up, so a newer attempt's worker is never a target.
    Worker metadata and the story binding are the retry record: both survive
    until worker-manager has confirmed the removal, so the next tick re-drives a
    lost or unobserved request.
    """
    redis = redis_client.redis
    workers_by_story: dict[str, set[str]] = {}
    storyless_attempts: dict[str, str] = {}
    async for raw_key in redis.scan_iter(match="worker:meta:*"):
        key = decode_redis_value(raw_key)
        meta = decode_redis_fields(await redis.hgetall(key))
        worker_id = key.removeprefix("worker:meta:")
        if meta.get("story_id"):
            workers_by_story.setdefault(meta["story_id"], set()).add(worker_id)
        elif meta.get("attempt_id"):
            storyless_attempts[worker_id] = meta["attempt_id"]

    bound_workers = decode_redis_fields(await redis.hgetall(STORY_WORKERS_KEY))
    for story_id, worker_id in bound_workers.items():
        workers_by_story.setdefault(story_id, set()).add(worker_id)

    finalized = 0
    for story_id, worker_ids in workers_by_story.items():
        runs = await api_client.list_story_runs(story_id)
        if any(run.status in LIVE_RUN_STATUSES for run in runs):
            continue
        attempts = [
            run
            for run in runs
            if run.type == RunType.ENGINEERING and run.status != RunStatus.CANCELLED
        ]
        if not attempts:
            continue
        newest = max(attempts, key=lambda run: run.created_at)
        if not _gave_up(newest):
            continue
        bound_worker = bound_workers.get(story_id)
        ordered = sorted(worker_ids, key=lambda worker_id: worker_id != bound_worker)
        for worker_id in ordered:
            finalized += await _finalize(
                redis_client,
                story_id=story_id,
                project_id=str(newest.project_id),
                attempt_id=newest.id,
                worker_id=worker_id,
            )

    for worker_id, attempt_id in storyless_attempts.items():
        run = await api_client.get_run_if_missing_returns_none(attempt_id)
        if run is None or not _gave_up(run):
            continue
        finalized += await _finalize(
            redis_client,
            story_id=None,
            project_id=str(run.project_id),
            attempt_id=run.id,
            worker_id=worker_id,
        )
    return finalized


async def _finalize(
    redis_client,
    *,
    story_id: str | None,
    project_id: str,
    attempt_id: str,
    worker_id: str,
) -> int:
    complete = await finalize_story_worker_teardown(
        redis_client,
        story_id=story_id,
        project_id=project_id,
        request_id=f"gave-up-{attempt_id}-{worker_id}",
        worker_id=worker_id,
        reason="failed",
        observations=1,
    )
    if not complete:
        return 0
    logger.info(
        "gave_up_attempt_worker_teardown_finalized",
        story_id=story_id,
        attempt_id=attempt_id,
        worker_id=worker_id,
    )
    return 1
