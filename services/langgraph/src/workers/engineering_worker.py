"""Engineering Worker — consumes from jobs:engineering queue.

Run standalone: python -m src.workers.engineering_worker
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import os
import signal

import structlog

from shared.logging_config import setup_logging
from shared.queues import ENGINEERING_QUEUE, WORKER_GROUP, ensure_consumer_groups
from shared.redis_client import RedisStreamClient

from ..clients.api import api_client

logger = structlog.get_logger(__name__)

# Worker identification
CONSUMER_NAME = f"engineering-worker-{os.getpid()}"

# Shutdown flag
_shutdown = False


def handle_shutdown(signum, frame):
    """Handle shutdown signals gracefully."""
    global _shutdown
    logger.info("shutdown_signal_received", signal=signum)
    _shutdown = True


async def process_engineering_job(job_data: dict) -> dict:
    """Process a single engineering job.

    Stub: integration tracked in docs/backlog.md
    "Engineering Pipeline: TesterNode & Worker Integration"
    """
    job_id = job_data.get("job_id", "unknown")
    project_id = job_data.get("project_id")
    task_description = job_data.get("task_description")

    logger.info(
        "engineering_job_started",
        job_id=job_id,
        project_id=project_id,
        task_description=task_description[:100] if task_description else None,
    )

    try:
        # Stub: subgraph integration pending (see docs/backlog.md)
        # result = await engineering_subgraph.ainvoke(state, config)

        # Placeholder - in production this would run the full pipeline
        logger.warning(
            "engineering_job_placeholder",
            job_id=job_id,
            message="Engineering worker not yet integrated with subgraph",
        )

        return {
            "status": "pending_implementation",
            "message": "Engineering worker placeholder - subgraph integration pending",
            "finished_at": datetime.now(UTC).isoformat(),
        }

    except Exception as e:
        logger.error(
            "engineering_job_exception",
            job_id=job_id,
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        return {
            "status": "failed",
            "error": str(e),
            "finished_at": datetime.now(UTC).isoformat(),
        }


async def run_worker():
    """Main worker loop."""
    setup_logging(service_name="engineering-worker")

    redis = RedisStreamClient()
    await redis.connect()

    # Ensure consumer groups exist
    await ensure_consumer_groups(redis.redis)

    logger.info("engineering_worker_started", consumer=CONSUMER_NAME)

    try:
        while not _shutdown:
            try:
                # Read from consumer group
                messages = await redis.redis.xreadgroup(
                    groupname=WORKER_GROUP,
                    consumername=CONSUMER_NAME,
                    streams={ENGINEERING_QUEUE: ">"},
                    count=1,
                    block=5000,
                )

                if not messages:
                    continue

                for _stream_name, entries in messages:
                    for entry_id, raw_data in entries:
                        try:
                            import json

                            if "data" in raw_data:
                                job_data = json.loads(raw_data["data"])
                            else:
                                job_data = raw_data

                            result = await process_engineering_job(job_data)
                            job_data.update(result)

                            await redis.redis.xack(ENGINEERING_QUEUE, WORKER_GROUP, entry_id)
                            logger.debug("engineering_job_acked", entry_id=entry_id)

                        except Exception as e:
                            logger.error(
                                "engineering_job_processing_error",
                                entry_id=entry_id,
                                error=str(e),
                            )

            except asyncio.CancelledError:
                logger.info("worker_cancelled")
                break
            except Exception as e:
                logger.error("worker_loop_error", error=str(e))
                await asyncio.sleep(1)

    finally:
        await redis.close()
        await api_client.close()
        logger.info("engineering_worker_shutdown")


def main():
    """Entry point for running as module."""
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
