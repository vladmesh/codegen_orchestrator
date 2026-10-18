"""Read the real QA stream for the scheduler service tests."""

import json

from shared.queues import QA_QUEUE
from shared.redis import RedisStreamClient


async def qa_messages_for(redis_client: RedisStreamClient, qa_run_id: str) -> int:
    """How many messages on `qa:queue` name this QA Run."""
    entries = await redis_client.redis.xrange(QA_QUEUE)
    return sum(1 for _, fields in entries if json.loads(fields["data"])["run_id"] == qa_run_id)
