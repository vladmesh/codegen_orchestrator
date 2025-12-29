"""Deploy Worker — consumes from jobs:deploy queue and runs DevOps.

Run standalone: python -m src.workers.deploy_worker
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import os
import signal

import structlog

from shared.logging_config import setup_logging
from shared.queues import DEPLOY_QUEUE, WORKER_GROUP, ensure_consumer_groups
from shared.redis_client import RedisStreamClient

from ..clients.api import api_client
from ..subgraphs.devops import create_devops_subgraph

logger = structlog.get_logger(__name__)

# Worker identification
CONSUMER_NAME = f"deploy-worker-{os.getpid()}"

# Shutdown flag
_shutdown = False


def handle_shutdown(signum, frame):
    """Handle shutdown signals gracefully."""
    global _shutdown
    logger.info("shutdown_signal_received", signal=signum)
    _shutdown = True


async def process_deploy_job(job_data: dict) -> dict:
    """Process a single deploy job by running DevOps node.

    Args:
        job_data: Job data from Redis queue

    Returns:
        Result dict with status and details
    """
    job_id = job_data.get("job_id", "unknown")
    project_id = job_data.get("project_id")

    logger.info("deploy_job_started", job_id=job_id, project_id=project_id)

    try:
        # Fetch project details
        project = await api_client.get_project(project_id)
        if not project:
            return {
                "status": "failed",
                "error": f"Project {project_id} not found",
            }

        # Get allocations for the project
        allocations = await api_client.get_project_allocations(project_id)
        if not allocations:
            return {
                "status": "failed",
                "error": "No resources allocated for project",
            }

        allocation = allocations[0]

        # Build state for DevOps node
        # This mirrors what the orchestrator would provide
        state = {
            "project_id": project_id,
            "project_spec": project,
            "repo_info": {
                "full_name": project.get("repository_url", "")
                .replace("https://github.com/", "")
                .rstrip(".git"),
            },
            "allocated_resources": {
                f"{allocation['server_handle']}:{allocation['port']}": {
                    "port": allocation["port"],
                    "server_handle": allocation["server_handle"],
                    "server_ip": allocation.get("server_ip"),
                }
            },
            "correlation_id": job_data.get("correlation_id"),
        }

        # If server_ip not in allocation, fetch from server
        if not state["allocated_resources"][
            f"{allocation['server_handle']}:{allocation['port']}"
        ].get("server_ip"):
            server = await api_client.get_server(allocation["server_handle"])
            if server:
                state["allocated_resources"][f"{allocation['server_handle']}:{allocation['port']}"][
                    "server_ip"
                ] = server.get("public_ip")

        # Run DevOps subgraph
        devops_subgraph = create_devops_subgraph()

        # Prepare subgraph input
        subgraph_input = {
            "project_id": project_id,
            "project_spec": project,
            "repo_info": {
                "full_name": project.get("repository_url", "")
                .replace("https://github.com/", "")
                .rstrip(".git"),
                "html_url": project.get("repository_url"),
            },
            "allocated_resources": {
                f"{allocation['server_handle']}:{allocation['port']}": {
                    "port": allocation["port"],
                    "server_handle": allocation["server_handle"],
                    "server_ip": allocation.get("server_ip"),
                }
            },
            "provided_secrets": job_data.get("provided_secrets", {}),
            # Initialize empty fields
            "messages": [],
            "env_variables": [],
            "env_analysis": {},
            "resolved_secrets": {},
            "missing_user_secrets": [],
            "deployment_result": None,
            "deployed_url": None,
            "errors": [],
        }

        result = await devops_subgraph.ainvoke(subgraph_input)

        if result.get("deployed_url"):
            logger.info(
                "deploy_job_success",
                job_id=job_id,
                deployed_url=result["deployed_url"],
            )
            return {
                "status": "success",
                "deployed_url": result["deployed_url"],
                "finished_at": datetime.now(UTC).isoformat(),
            }
        elif result.get("missing_user_secrets"):
            missing = result.get("missing_user_secrets")
            logger.info("deploy_job_missing_secrets", job_id=job_id, missing=missing)
            return {
                "status": "failed",
                "error": f"Missing secrets: {', '.join(missing)}",
                "missing_secrets": missing,
                "finished_at": datetime.now(UTC).isoformat(),
            }
        else:
            errors = result.get("errors", ["Unknown deployment error"])
            logger.error("deploy_job_failed", job_id=job_id, errors=errors)
            return {
                "status": "failed",
                "error": "; ".join(errors),
                "finished_at": datetime.now(UTC).isoformat(),
            }

    except Exception as e:
        logger.error(
            "deploy_job_exception",
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
    setup_logging(service_name="deploy-worker")

    redis = RedisStreamClient()
    await redis.connect()

    # Ensure consumer groups exist
    await ensure_consumer_groups(redis.redis)

    logger.info("deploy_worker_started", consumer=CONSUMER_NAME)

    try:
        while not _shutdown:
            try:
                # Read from consumer group
                messages = await redis.redis.xreadgroup(
                    groupname=WORKER_GROUP,
                    consumername=CONSUMER_NAME,
                    streams={DEPLOY_QUEUE: ">"},
                    count=1,
                    block=5000,  # 5 second block
                )

                if not messages:
                    continue

                for _stream_name, entries in messages:
                    for entry_id, raw_data in entries:
                        try:
                            # Parse JSON data if needed
                            import json

                            if "data" in raw_data:
                                job_data = json.loads(raw_data["data"])
                            else:
                                job_data = raw_data

                            # Process the job
                            result = await process_deploy_job(job_data)

                            # Update job status in stream (for polling)
                            # Note: In production, we'd update checkpointer
                            job_data.update(result)

                            # ACK the message
                            await redis.redis.xack(DEPLOY_QUEUE, WORKER_GROUP, entry_id)
                            logger.debug("deploy_job_acked", entry_id=entry_id)

                        except Exception as e:
                            logger.error(
                                "deploy_job_processing_error",
                                entry_id=entry_id,
                                error=str(e),
                            )
                            # Don't ACK — job will be redelivered

            except asyncio.CancelledError:
                logger.info("worker_cancelled")
                break
            except Exception as e:
                logger.error("worker_loop_error", error=str(e))
                await asyncio.sleep(1)

    finally:
        await redis.close()
        await api_client.close()
        logger.info("deploy_worker_shutdown")


def main():
    """Entry point for running as module."""
    # Register signal handlers
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
