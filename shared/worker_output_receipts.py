"""Finite transport replay retention and owned worker receipt cleanup."""

from redis.asyncio import Redis

# Fixed protocol horizon from acceptance, independent of agent turn duration.
# Covers lost replies and resubmission well beyond the wrapper's 180s timeout.
OUTPUT_RECEIPT_TTL_SECONDS = 24 * 3600
_CLEANUP_BATCH_SIZE = 100


async def delete_worker_output_receipts(redis: Redis, worker_id: str) -> None:
    """Collect every owned lease, including receipts predating native expiry."""
    # Worker IDs are literal Redis key components, never glob expressions.
    literal_id = "".join(f"\\{char}" if char in "\\*?[]" else char for char in worker_id)
    batch = []
    async for key in redis.scan_iter(
        match=f"worker:output-receipt:{literal_id}:*", count=_CLEANUP_BATCH_SIZE
    ):
        batch.append(key)
        # SCAN COUNT is a hint; bound DEL arguments independently.
        if len(batch) == _CLEANUP_BATCH_SIZE:
            await redis.delete(*batch)
            batch.clear()
    if batch:
        await redis.delete(*batch)
