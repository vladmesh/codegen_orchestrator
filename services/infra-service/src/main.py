"""Infrastructure Worker — consumes from provisioner:queue and ansible:deploy:queue.

Run standalone: python -m src.main
"""

from __future__ import annotations

import asyncio
import json
import os
import signal

import structlog

from shared.log_config import setup_logging
from shared.queues import ANSIBLE_DEPLOY_QUEUE, INFRA_GROUP, PROVISIONER_QUEUE, ensure_all_groups
from shared.redis_client import RedisStreamClient

from .deployer import deploy_project
from .provisioner.node import ProvisionerNode

logger = structlog.get_logger(__name__)

# Consumer configuration
CONSUMER_NAME = f"infra-worker-{os.getpid()}"

# Shutdown flag
_shutdown = False


def handle_shutdown(signum, frame):
    """Handle shutdown signals gracefully."""
    global _shutdown
    logger.info("shutdown_signal_received", signal=signum)
    _shutdown = True


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
                "request_id": job_id,
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
                "request_id": job_id,
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
            "request_id": job_id,
            "status": "failed",
            "server_handle": server_handle,
            "error": str(e),
        }


async def process_deploy_job(job_data: dict) -> dict:
    """Process a single deploy job.

    Args:
        job_data: Job data from Redis queue with project deployment info

    Returns:
        Result dict with status and details
    """
    request_id = job_data.get("request_id", "unknown")
    project_id = job_data.get("project_id")
    project_name = job_data.get("project_name", "unknown")
    server_ip = job_data.get("server_ip")
    port = job_data.get("port")

    logger.info(
        "deploy_job_started",
        request_id=request_id,
        project_id=project_id,
        project_name=project_name,
        server_ip=server_ip,
        port=port,
    )

    try:
        success, message = await deploy_project(
            project_name=project_name,
            repo_full_name=job_data.get("repo_full_name", ""),
            github_token=job_data.get("github_token", ""),
            server_ip=server_ip,
            port=port,
            secrets=job_data.get("secrets", {}),
            modules=job_data.get("modules"),
        )

        if success:
            logger.info(
                "deploy_job_success",
                request_id=request_id,
                project_id=project_id,
                project_name=project_name,
                server_ip=server_ip,
                port=port,
            )
            return {
                "status": "success",
                "project_id": project_id,
                "project_name": project_name,
                "server_ip": server_ip,
                "port": port,
                "deployed_url": f"http://{server_ip}:{port}",
                "message": message,
            }
        else:
            logger.error(
                "deploy_job_failed",
                request_id=request_id,
                project_id=project_id,
                project_name=project_name,
                error=message,
            )
            return {
                "status": "failed",
                "project_id": project_id,
                "project_name": project_name,
                "error": message,
            }

    except Exception as e:
        logger.error(
            "deploy_job_exception",
            request_id=request_id,
            project_id=project_id,
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        return {
            "status": "failed",
            "project_id": project_id,
            "project_name": project_name,
            "error": str(e),
        }


async def run_worker():
    """Main worker loop handling provisioning and deploy queues."""
    setup_logging(service_name="infra-service")

    redis = RedisStreamClient()
    await redis.connect()

    # Ensure consumer groups exist
    await ensure_all_groups(redis.redis)

    logger.info("infrastructure_worker_started", consumer=CONSUMER_NAME)

    try:
        while not _shutdown:
            try:
                # Read from both queues
                messages = await redis.redis.xreadgroup(
                    groupname=INFRA_GROUP,
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

                            # Route to appropriate handler
                            if stream_name_str == PROVISIONER_QUEUE:
                                result = await process_provisioner_job(job_data)
                                result_stream = "provisioner:results"
                            elif stream_name_str == ANSIBLE_DEPLOY_QUEUE:
                                result = await process_deploy_job(job_data)
                                result_stream = "deploy:results"
                            else:
                                logger.warning("unknown_queue", stream=stream_name_str)
                                continue

                            # Publish result
                            request_id = job_data.get("job_id") or job_data.get("request_id")
                            if request_id:
                                # Store result in Redis key for polling
                                result_key = f"deploy:result:{request_id}"
                                await redis.redis.set(
                                    result_key,
                                    json.dumps(result),
                                    ex=3600,  # 1 hour TTL
                                )
                                # Also publish to stream
                                await redis.publish(result_stream, result)

                            # ACK the message
                            await redis.redis.xack(
                                stream_name_str,
                                INFRA_GROUP,
                                entry_id,
                            )
                            logger.debug("job_acked", stream=stream_name_str, entry_id=entry_id)

                        except Exception as e:
                            logger.error(
                                "job_processing_error",
                                stream=stream_name_str,
                                entry_id=entry_id,
                                error=str(e),
                                exc_info=True,
                            )
                            # Don't ACK — job will be redelivered

            except asyncio.CancelledError:
                logger.info("worker_cancelled")
                break
            except Exception as e:
                if "NOGROUP" in str(e):
                    logger.warning(
                        "consumer_nogroup_recovering",
                        stream=PROVISIONER_QUEUE,
                        group=INFRA_GROUP,
                    )
                    await ensure_all_groups(redis.redis)
                else:
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
