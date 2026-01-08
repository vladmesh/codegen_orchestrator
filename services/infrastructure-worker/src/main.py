"""Infrastructure Worker — consumes from provisioner:queue and runs provisioning.

Run standalone: python -m src.main
"""

from __future__ import annotations

import asyncio
import json
import os
import signal

import structlog

from shared.logging_config import setup_logging
from shared.queues import ANSIBLE_DEPLOY_QUEUE
from shared.redis_client import RedisStreamClient

from .provisioner.deployment_executor import run_deployment_playbook
from .provisioner.node import ProvisionerNode

logger = structlog.get_logger(__name__)

# Queue configuration
PROVISIONER_QUEUE = "provisioner:queue"
PROVISIONER_GROUP = "infrastructure-workers"
CONSUMER_NAME = f"infra-worker-{os.getpid()}"

# Shutdown flag
_shutdown = False


def handle_shutdown(signum, frame):
    """Handle shutdown signals gracefully."""
    global _shutdown
    logger.info("shutdown_signal_received", signal=signum)
    _shutdown = True


async def ensure_consumer_groups(redis_client) -> None:
    """Ensure Redis consumer groups exist for both queues."""
    queues = [PROVISIONER_QUEUE, ANSIBLE_DEPLOY_QUEUE]

    for queue in queues:
        try:
            await redis_client.xgroup_create(queue, PROVISIONER_GROUP, id="0", mkstream=True)
            logger.info("consumer_group_created", queue=queue, group=PROVISIONER_GROUP)
        except Exception as e:
            # Group already exists
            if "BUSYGROUP" in str(e):
                logger.debug("consumer_group_exists", queue=queue, group=PROVISIONER_GROUP)
            else:
                raise


async def process_deployment_job(job_data: dict) -> dict:
    """Process a deployment job from ansible:deploy:queue.

    Args:
        job_data: Deployment job data matching DeploymentJobRequest schema

    Returns:
        Result dict matching DeploymentJobResult schema
    """
    request_id = job_data.get("request_id", "unknown")
    project_name = job_data.get("project_name", "unknown")
    repo_full_name = job_data.get("repo_full_name", "unknown/unknown")

    logger.info(
        "deployment_job_started",
        request_id=request_id,
        project_name=project_name,
        repo=repo_full_name,
    )

    try:
        # Execute deployment playbook
        result = run_deployment_playbook(
            project_name=job_data["project_name"],
            repo_full_name=job_data["repo_full_name"],
            github_token=job_data["github_token"],
            server_ip=job_data["server_ip"],
            service_port=job_data["port"],
            secrets=job_data.get("secrets", {}),
            modules=job_data.get("modules"),
        )

        if result.get("status") == "success":
            logger.info(
                "deployment_job_success",
                request_id=request_id,
                deployed_url=result.get("deployed_url"),
            )
        else:
            logger.error(
                "deployment_job_failed",
                request_id=request_id,
                error=result.get("error"),
            )

        return result

    except Exception as e:
        logger.error(
            "deployment_job_exception",
            request_id=request_id,
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        return {
            "status": "error",
            "error": str(e),
        }


async def process_provisioner_job(job_data: dict) -> dict:
    """Process a single provisioner job.

    Args:
        job_data: Job data from Redis queue

    Returns:
        Result dict with status and details
    """
    job_id = job_data.get("job_id") or job_data.get("request_id", "unknown")
    server_handle = job_data.get("server_handle")

    logger.info(
        "provisioner_job_started",
        job_id=job_id,
        server_handle=server_handle,
    )

    try:
        # Build state for ProvisionerNode
        state = {
            "server_to_provision": server_handle,
            "is_incident_recovery": job_data.get("is_recovery", False),
            "force_reinstall": job_data.get("force_reinstall", False),
            "errors": [],
        }

        # Run provisioner
        node = ProvisionerNode()
        result = await node.run(state)

        # Extract result
        provisioning_result = result.get("provisioning_result", {})
        status = provisioning_result.get("status", "unknown")

        if status == "success":
            logger.info(
                "provisioner_job_success",
                job_id=job_id,
                server_handle=server_handle,
                server_ip=provisioning_result.get("server_ip"),
            )
            return {
                "status": "success",
                "server_handle": server_handle,
                "server_ip": provisioning_result.get("server_ip"),
                "services_redeployed": provisioning_result.get("services_redeployed", 0),
            }
        else:
            errors = result.get("errors", ["Unknown error"])
            logger.error(
                "provisioner_job_failed",
                job_id=job_id,
                server_handle=server_handle,
                errors=errors,
            )
            return {
                "status": "failed",
                "server_handle": server_handle,
                "errors": errors,
            }

    except Exception as e:
        logger.error(
            "provisioner_job_exception",
            job_id=job_id,
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        return {
            "status": "failed",
            "server_handle": server_handle,
            "error": str(e),
        }


async def run_worker():
    """Main worker loop handling both provisioning and deployment queues."""
    setup_logging(service_name="infrastructure-worker")

    redis = RedisStreamClient()
    await redis.connect()

    # Ensure consumer groups exist
    await ensure_consumer_groups(redis.redis)

    logger.info("infrastructure_worker_started", consumer=CONSUMER_NAME)

    try:
        while not _shutdown:
            try:
                # Read from both queues
                messages = await redis.redis.xreadgroup(
                    groupname=PROVISIONER_GROUP,
                    consumername=CONSUMER_NAME,
                    streams={PROVISIONER_QUEUE: ">", ANSIBLE_DEPLOY_QUEUE: ">"},
                    count=1,
                    block=5000,  # 5 second block
                )

                if not messages:
                    continue

                for stream_name, entries in messages:
                    # Normalize stream_name to string (Redis may return bytes or str)
                    stream_name_str = (
                        stream_name.decode() if isinstance(stream_name, bytes) else stream_name
                    )

                    for entry_id, raw_data in entries:
                        try:
                            # Parse JSON data
                            if "data" in raw_data:
                                job_data = json.loads(raw_data["data"])
                            else:
                                job_data = raw_data

                            # Route to appropriate handler based on queue
                            if stream_name_str == PROVISIONER_QUEUE:
                                result = await process_provisioner_job(job_data)
                                result_prefix = "provisioner"
                            elif stream_name_str == ANSIBLE_DEPLOY_QUEUE:
                                result = await process_deployment_job(job_data)
                                result_prefix = "deploy"
                            else:
                                logger.warning("unknown_queue", stream=stream_name_str)
                                continue

                            # Publish result if request_id provided
                            request_id = job_data.get("request_id")
                            if request_id:
                                result_key = f"{result_prefix}:result:{request_id}"
                                await redis.redis.set(
                                    result_key,
                                    json.dumps(result),
                                    ex=3600,  # 1 hour TTL
                                )

                            # ACK the message
                            await redis.redis.xack(
                                stream_name_str,
                                PROVISIONER_GROUP,
                                entry_id,
                            )
                            logger.debug("job_acked", stream=stream_name, entry_id=entry_id)

                        except Exception as e:
                            logger.error(
                                "job_processing_error",
                                stream=stream_name,
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
        logger.info("infrastructure_worker_shutdown")


def main():
    """Entry point for running as module."""
    # Register signal handlers
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
