"""Typed worker-turn stream helpers shared by LangGraph worker clients."""

from __future__ import annotations

import redis.asyncio as redis

from shared.contracts.worker_turn import WorkerTurnInput
from shared.queues import worker_input_stream, worker_output_stream
from shared.redis.client import DEFAULT_STREAM_MAXLEN


async def _publish_engineering_turn(redis_client, worker_id, turn):
    from .api import api_client

    await api_client.post(
        f"runs/{turn.attempt_id}/publish-worker-turn",
        json={"worker_id": worker_id, "turn": turn.model_dump(mode="json", exclude_none=True)},
    )


async def ensure_worker_output_group(
    redis_client: redis.Redis,
    worker_id: str,
    group_name: str,
    *,
    start_id: str = "0",
) -> str:
    """Create the consumer group for one worker output stream and return its name."""
    output_stream = worker_output_stream(worker_id)
    try:
        await redis_client.xgroup_create(output_stream, group_name, id=start_id, mkstream=True)
    except redis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise
    return output_stream


async def publish_worker_turn(
    redis_client: redis.Redis,
    worker_id: str,
    turn: WorkerTurnInput,
) -> None:
    """Serialize one validated turn onto the worker's input stream."""
    if turn.attempt_id is not None:
        await _publish_engineering_turn(redis_client, worker_id, turn)
        return
    await redis_client.xadd(
        worker_input_stream(worker_id),
        {"data": turn.model_dump_json(exclude_none=True)},
        maxlen=DEFAULT_STREAM_MAXLEN,
        approximate=True,
    )
