"""Engineering Worker — consumes from jobs:engineering queue.

Run standalone: python -m src.workers.engineering_worker
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
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


async def process_engineering_job(job_data: dict, redis: RedisStreamClient) -> dict:
    """Process a single engineering job by running Engineering Subgraph.

    Args:
        job_data: Job data from Redis queue (task_id, project_id, user_id, callback_stream)
        redis: Redis client for publishing events

    Returns:
        Result dict with status and details
    """
    from ..subgraphs.engineering import create_engineering_subgraph

    task_id = job_data.get("task_id", "unknown")
    project_id = job_data.get("project_id")
    callback_stream = job_data.get("callback_stream")

    logger.info("engineering_job_started", task_id=task_id, project_id=project_id)

    try:
        # Update task status to running
        await api_client.patch(f"tasks/{task_id}", json={"status": "running"})

        # Publish progress event
        if callback_stream:
            await redis.redis.xadd(
                callback_stream,
                {
                    "data": json.dumps(
                        {
                            "type": "progress",
                            "task_id": task_id,
                            "message": "Engineering task started",
                            "timestamp": datetime.now(UTC).isoformat(),
                        }
                    )
                },
            )

        # Fetch project details
        project = await api_client.get_project(project_id)
        if not project:
            error_msg = f"Project {project_id} not found"
            await api_client.patch(
                f"tasks/{task_id}",
                json={"status": "failed", "error_message": error_msg},
            )
            return {"status": "failed", "error": error_msg}

        # Prepare EngineeringState
        subgraph_input = {
            "messages": [],
            "current_project": project_id,
            "project_spec": project,
            "allocated_resources": {},
            "commit_sha": None,
            "engineering_status": "idle",
            "iteration_count": 0,
            "test_results": None,
            "needs_human_approval": False,
            "human_approval_reason": None,
            "errors": [],
        }

        # Create and run engineering subgraph
        engineering_subgraph = create_engineering_subgraph()
        result = await engineering_subgraph.ainvoke(subgraph_input)

        # Check result status
        if result.get("engineering_status") == "done":
            logger.info(
                "engineering_job_success",
                task_id=task_id,
                commit_sha=result.get("commit_sha"),
            )
            await api_client.patch(
                f"tasks/{task_id}",
                json={
                    "status": "completed",
                    "result": {
                        "engineering_status": result["engineering_status"],
                        "commit_sha": result.get("commit_sha"),
                        "selected_modules": result.get("selected_modules"),
                        "test_results": result.get("test_results"),
                    },
                },
            )

            if callback_stream:
                await redis.redis.xadd(
                    callback_stream,
                    {
                        "data": json.dumps(
                            {
                                "type": "completed",
                                "task_id": task_id,
                                "message": "Engineering task completed successfully",
                                "timestamp": datetime.now(UTC).isoformat(),
                            }
                        )
                    },
                )

            return {
                "status": "success",
                "commit_sha": result.get("commit_sha"),
                "finished_at": datetime.now(UTC).isoformat(),
            }

        elif result.get("engineering_status") == "blocked" or result.get("needs_human_approval"):
            logger.info("engineering_job_blocked", task_id=task_id, errors=result.get("errors"))
            await api_client.patch(
                f"tasks/{task_id}",
                json={
                    "status": "failed",
                    "error_message": "; ".join(result.get("errors", ["Task blocked"])),
                },
            )

            if callback_stream:
                await redis.redis.xadd(
                    callback_stream,
                    {
                        "data": json.dumps(
                            {
                                "type": "failed",
                                "task_id": task_id,
                                "message": "Engineering task blocked or needs approval",
                                "timestamp": datetime.now(UTC).isoformat(),
                            }
                        )
                    },
                )

            return {
                "status": "failed",
                "error": "; ".join(result.get("errors", ["Task blocked"])),
                "finished_at": datetime.now(UTC).isoformat(),
            }

        else:
            # Unknown status
            errors = result.get("errors", ["Unknown engineering status"])
            logger.error("engineering_job_unknown_status", task_id=task_id, errors=errors)
            await api_client.patch(
                f"tasks/{task_id}",
                json={"status": "failed", "error_message": "; ".join(errors)},
            )
            return {
                "status": "failed",
                "error": "; ".join(errors),
                "finished_at": datetime.now(UTC).isoformat(),
            }

    except Exception as e:
        logger.error(
            "engineering_job_exception",
            task_id=task_id,
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        await api_client.patch(
            f"tasks/{task_id}",
            json={"status": "failed", "error_message": str(e), "error_traceback": str(e)},
        )

        if callback_stream:
            await redis.redis.xadd(
                callback_stream,
                {
                    "data": json.dumps(
                        {
                            "type": "failed",
                            "task_id": task_id,
                            "message": f"Engineering task failed: {e!s}",
                            "timestamp": datetime.now(UTC).isoformat(),
                        }
                    )
                },
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
                            if "data" in raw_data:
                                job_data = json.loads(raw_data["data"])
                            else:
                                job_data = raw_data

                            result = await process_engineering_job(job_data, redis)
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
