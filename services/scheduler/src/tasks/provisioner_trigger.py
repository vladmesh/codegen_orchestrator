"""Provisioner trigger worker - listens for provisioning requests and triggers LangGraph.

Uses Redis pub/sub to receive trigger events from health_checker and server_sync workers.
"""

import asyncio
import json
import logging
import os

import aiohttp
import redis.asyncio as redis

logger = logging.getLogger(__name__)

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
        async with aiohttp.ClientSession() as session:
            # TODO: Update this URL once LangGraph API is set up
            # For now, just log the intent
            logger.info(
                f"🚀 Would trigger Provisioner for {server_handle} "
                f"(incident_recovery={is_incident_recovery})"
            )
            logger.debug(f"Payload: {json.dumps(payload, indent=2)}")

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
        logger.error(f"Error triggering provisioner for {server_handle}: {e}")
        return False


async def provisioner_trigger_worker():
    """Background worker that listens for provisioning trigger events via Redis pub/sub."""
    logger.info(f"Starting Provisioner Trigger Worker (listening on {PROVISIONER_TRIGGER_CHANNEL})")

    redis_client = None
    pubsub = None

    try:
        # Connect to Redis
        redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

        # Subscribe to provisioner trigger channel
        pubsub = redis_client.pubsub()
        await pubsub.subscribe(PROVISIONER_TRIGGER_CHANNEL)

        logger.info(f"✅ Subscribed to Redis channel: {PROVISIONER_TRIGGER_CHANNEL}")

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
                        logger.warning(f"Received trigger without server_handle: {data}")
                        continue

                    logger.info(
                        f"📩 Received provisioning trigger: {server_handle} "
                        f"(incident_recovery={is_incident_recovery})"
                    )

                    # Trigger provisioner
                    success = await trigger_provisioner(server_handle, is_incident_recovery)

                    if success:
                        logger.info(f"✅ Successfully triggered provisioner for {server_handle}")
                    else:
                        logger.error(f"❌ Failed to trigger provisioner for {server_handle}")

                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse trigger message: {e}, data={message['data']}")
                except Exception as e:
                    logger.error(f"Error processing trigger message: {e}", exc_info=True)

            except asyncio.CancelledError:
                logger.info("Provisioner Trigger Worker cancelled")
                break
            except Exception as e:
                logger.error(f"Error in message loop: {e}", exc_info=True)
                await asyncio.sleep(1)

    except Exception as e:
        logger.error(f"Error in Provisioner Trigger Worker: {e}", exc_info=True)

    finally:
        if pubsub:
            try:
                await pubsub.unsubscribe(PROVISIONER_TRIGGER_CHANNEL)
                await pubsub.close()
            except Exception:
                pass
        if redis_client:
            try:
                await redis_client.close()
            except Exception:
                pass
        logger.info("Provisioner Trigger Worker stopped")


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
            f"📤 Published provisioning trigger for {server_handle} "
            f"(incident_recovery={is_incident_recovery})"
        )
    except Exception as e:
        logger.error(f"Failed to publish trigger for {server_handle}: {e}")
    finally:
        await redis_client.close()

