"""Typed worker-turn stream helpers shared by LangGraph worker clients."""

from __future__ import annotations

import redis.asyncio as redis

from shared.contracts.worker_turn import WorkerTurnInput
from shared.queues import worker_input_stream, worker_output_stream
from shared.redis.client import DEFAULT_STREAM_MAXLEN


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
    await redis_client.xadd(
        worker_input_stream(worker_id),
        {"data": turn.model_dump_json(exclude_none=True)},
        maxlen=DEFAULT_STREAM_MAXLEN,
        approximate=True,
    )
