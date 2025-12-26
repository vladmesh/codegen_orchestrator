"""Provisioner trigger worker - listens for provisioning requests and triggers LangGraph.

Uses Redis pub/sub to receive trigger events from health_checker and server_sync workers.
"""

import asyncio
import json
import os

import redis.asyncio as redis
import structlog

logger = structlog.get_logger()

# Configuration
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
LANGGRAPH_API_URL = os.getenv("LANGGRAPH_API_URL", "http://langgraph:8001")

# Redis channel for provisioning triggers
PROVISIONER_TRIGGER_CHANNEL = "provisioner:trigger"


async def trigger_provisioner(server_handle: str, is_incident_recovery: bool = False) -> bool:
    """Trigger LangGraph Provisioner node via HTTP API.

    Args:
        server_handle: Server handle to provision
        is_incident_recovery: True if this is incident recovery, False for new setup

    Returns:
        True if triggered successfully
    """
    # Prepare LangGraph invocation payload
    payload = {
        "input": {
            "messages": [{"role": "system", "content": f"Provision server {server_handle}"}],
            "server_to_provision": server_handle,
            "is_incident_recovery": is_incident_recovery,
        },
        "config": {"configurable": {"thread_id": f"provisioner-{server_handle}"}},
    }

    try:
        # TODO: Update this URL once LangGraph API is set up
        # For now, just log the intent
        logger.info(
            "provisioner_trigger_requested",
            server_handle=server_handle,
            is_incident_recovery=is_incident_recovery,
        )
        logger.debug("provisioner_trigger_payload", payload=payload)

        # In production, this would call:
        # async with session.post(
        #     f"{LANGGRAPH_API_URL}/invoke",
        #     json=payload,
        #     timeout=aiohttp.ClientTimeout(total=30)
        # ) as resp:
        #     if resp.status == 200:
        #         logger.info(f"Provisioner triggered for {server_handle}")
        #         return True
        #     else:
        #         logger.error(f"Failed to trigger provisioner: {resp.status}")
        #         return False

        # For MVP: assume success
        return True

    except Exception as e:
        logger.error(
            "provisioner_trigger_failed",
            server_handle=server_handle,
            is_incident_recovery=is_incident_recovery,
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        return False


async def provisioner_trigger_worker():
    """Background worker that listens for provisioning trigger events via Redis pub/sub."""
    logger.info(
        "provisioner_trigger_worker_started",
        channel=PROVISIONER_TRIGGER_CHANNEL,
    )

    redis_client = None
    pubsub = None

    try:
        # Connect to Redis
        redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

        # Subscribe to provisioner trigger channel
        pubsub = redis_client.pubsub()
        await pubsub.subscribe(PROVISIONER_TRIGGER_CHANNEL)

        logger.info("redis_channel_subscribed", channel=PROVISIONER_TRIGGER_CHANNEL)

        # Listen for messages using while loop
        while True:
            try:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)

                if message is None:
                    await asyncio.sleep(0.1)
                    continue

                if message["type"] != "message":
                    continue

                try:
                    # Parse trigger event
                    data = json.loads(message["data"])
                    server_handle = data.get("server_handle")
                    is_incident_recovery = data.get("is_incident_recovery", False)

                    if not server_handle:
                        logger.warning("provisioner_trigger_missing_handle", payload=data)
                        continue

                    logger.info(
                        "provisioner_trigger_received",
                        server_handle=server_handle,
                        is_incident_recovery=is_incident_recovery,
                    )

                    # Trigger provisioner
                    success = await trigger_provisioner(server_handle, is_incident_recovery)

                    if success:
                        logger.info(
                            "provisioner_trigger_succeeded",
                            server_handle=server_handle,
                        )
                    else:
                        logger.error(
                            "provisioner_trigger_failed",
                            server_handle=server_handle,
                        )

                except json.JSONDecodeError as e:
                    logger.error(
                        "provisioner_trigger_parse_failed",
                        error=str(e),
                        error_type=type(e).__name__,
                        raw_message=message.get("data"),
                    )
                except Exception as e:
                    logger.error(
                        "provisioner_trigger_processing_error",
                        error=str(e),
                        error_type=type(e).__name__,
                        exc_info=True,
                    )

            except asyncio.CancelledError:
                logger.info("provisioner_trigger_worker_cancelled")
                break
            except Exception as e:
                logger.error(
                    "provisioner_trigger_worker_loop_error",
                    error=str(e),
                    error_type=type(e).__name__,
                    exc_info=True,
                )
                await asyncio.sleep(1)

    except Exception as e:
        logger.error(
            "provisioner_trigger_worker_error",
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )

    finally:
        if pubsub:
            try:
                await pubsub.unsubscribe(PROVISIONER_TRIGGER_CHANNEL)
                await pubsub.close()
            except Exception as e:
                logger.error(
                    "redis_pubsub_close_failed",
                    error=str(e),
                    error_type=type(e).__name__,
                )
        if redis_client:
            try:
                await redis_client.close()
            except Exception as e:
                logger.error(
                    "redis_client_close_failed",
                    error=str(e),
                    error_type=type(e).__name__,
                )
        logger.info("provisioner_trigger_worker_stopped")


async def publish_provisioner_trigger(server_handle: str, is_incident_recovery: bool = False):
    """Publish a provisioning trigger event to Redis.

    This is called by health_checker and server_sync workers.

    Args:
        server_handle: Server handle to provision
        is_incident_recovery: True if this is incident recovery
    """
    redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

    try:
        payload = json.dumps(
            {
                "server_handle": server_handle,
                "is_incident_recovery": is_incident_recovery,
            }
        )

        await redis_client.publish(PROVISIONER_TRIGGER_CHANNEL, payload)

        logger.info(
            "provisioner_trigger_published",
            server_handle=server_handle,
            is_incident_recovery=is_incident_recovery,
        )
    except Exception as e:
        logger.error(
            "provisioner_trigger_publish_failed",
            server_handle=server_handle,
            is_incident_recovery=is_incident_recovery,
            error=str(e),
            error_type=type(e).__name__,
        )
    finally:
        await redis_client.close()
