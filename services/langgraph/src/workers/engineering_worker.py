"""Engineering Worker — consumes from jobs:engineering queue.

Run standalone: python -m src.workers.engineering_worker
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import os
import re
import signal
import uuid

import structlog

from shared.contracts.dto.project import ServiceModule
from shared.contracts.queues.scaffolder import ScaffolderMessage
from shared.logging_config import setup_logging
from shared.queues import DEPLOY_QUEUE, ENGINEERING_QUEUE, WORKER_GROUP, ensure_consumer_groups
from shared.redis_client import RedisStreamClient

from ..clients.api import api_client
from ..nodes.resource_allocator import resource_allocator_node

logger = structlog.get_logger(__name__)

# Scaffolder queue
SCAFFOLDER_QUEUE = "scaffolder:queue"


async def _trigger_scaffolding(project: dict, redis: RedisStreamClient) -> None:
    """Trigger scaffolding for a project in draft status.

    Sends ScaffolderMessage to scaffolder:queue and updates project status.
    """
    project_id = project["id"]
    project_name = project.get("name", project_id)

    # Get GITHUB_ORG for repo name
    org_name = os.getenv("GITHUB_ORG")
    if not org_name:
        raise RuntimeError("GITHUB_ORG environment variable is not set")

    # Generate repo name from project name
    repo_name = project_name.lower().replace(" ", "-").replace("_", "-")
    repo_name = re.sub(r"[^a-z0-9-]", "", repo_name)
    repo_name = re.sub(r"-+", "-", repo_name).strip("-")
    if not repo_name:
        repo_name = project_id[:8]
    repo_full_name = f"{org_name}/{repo_name}"

    # Get modules from project config
    project_config = project.get("config") or {}
    modules_list = project_config.get("modules", ["backend"])

    # Convert to ServiceModule enum
    service_modules = []
    for mod in modules_list:
        try:
            service_modules.append(ServiceModule(mod))
        except ValueError:
            logger.warning("unknown_module_skipped", module=mod)
    if not service_modules:
        service_modules = [ServiceModule.BACKEND]

    # Get task description
    task_description = project_config.get("description", "")
    if not task_description:
        task_description = project_config.get("detailed_spec", "")

    # Build scaffolder message
    scaffolder_msg = ScaffolderMessage(
        request_id=str(uuid.uuid4()),
        project_id=project_id,
        project_name=project_name,
        repo_full_name=repo_full_name,
        modules=service_modules,
        task_description=task_description,
    )

    # Send to scaffolder queue
    await redis.redis.xadd(
        SCAFFOLDER_QUEUE,
        {"data": scaffolder_msg.model_dump_json()},
    )

    # Update project status to scaffolding
    await api_client.patch(
        f"projects/{project_id}",
        json={"status": "scaffolding"},
    )

    logger.info(
        "scaffolding_triggered",
        project_id=project_id,
        repo_full_name=repo_full_name,
        modules=[m.value for m in service_modules],
    )


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

        # Trigger scaffolding if project is still in draft status
        project_status = project.get("status")
        if project_status == "draft":
            await _trigger_scaffolding(project, redis)

        # Allocate resources if not already allocated
        existing_allocations = await api_client.get_project_allocations(project_id)
        if existing_allocations:
            # Convert existing allocations to allocated_resources format
            allocated_resources = {
                f"{a['server_handle']}:{a['port']}": a for a in existing_allocations
            }
            logger.info(
                "using_existing_allocations",
                task_id=task_id,
                project_id=project_id,
                count=len(allocated_resources),
            )
        else:
            # Run resource allocator to create allocations
            logger.info(
                "allocating_resources",
                task_id=task_id,
                project_id=project_id,
            )
            alloc_result = await resource_allocator_node.run(
                {
                    "project_id": project_id,
                    "project_spec": project,
                    "allocated_resources": {},
                    "errors": [],
                }
            )

            if alloc_result.get("errors"):
                error_msg = "; ".join(alloc_result["errors"])
                logger.error(
                    "resource_allocation_failed",
                    task_id=task_id,
                    project_id=project_id,
                    errors=alloc_result["errors"],
                )
                await api_client.patch(
                    f"tasks/{task_id}",
                    json={"status": "failed", "error_message": error_msg},
                )
                return {"status": "failed", "error": error_msg}

            allocated_resources = alloc_result.get("allocated_resources", {})
            logger.info(
                "resources_allocated",
                task_id=task_id,
                project_id=project_id,
                count=len(allocated_resources),
            )

        # Prepare EngineeringState
        subgraph_input = {
            "messages": [],
            "current_project": project_id,
            "project_spec": project,
            "allocated_resources": allocated_resources,
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

            # Auto-trigger deploy after successful engineering
            deploy_task_id = f"deploy-{task_id.replace('eng-', '')}"
            try:
                # Create deploy task in API
                await api_client.post(
                    "tasks/",
                    json={
                        "id": deploy_task_id,
                        "type": "deploy",
                        "project_id": project_id,
                        "status": "pending",
                    },
                )
                # Queue deploy job
                await redis.redis.xadd(
                    DEPLOY_QUEUE,
                    {
                        "data": json.dumps(
                            {
                                "task_id": deploy_task_id,
                                "project_id": project_id,
                                "callback_stream": callback_stream,
                            }
                        )
                    },
                )
                logger.info(
                    "deploy_auto_triggered",
                    task_id=task_id,
                    deploy_task_id=deploy_task_id,
                    project_id=project_id,
                )
            except Exception as e:
                logger.error(
                    "deploy_auto_trigger_failed",
                    task_id=task_id,
                    error=str(e),
                )

            return {
                "status": "success",
                "commit_sha": result.get("commit_sha"),
                "deploy_task_id": deploy_task_id,
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
