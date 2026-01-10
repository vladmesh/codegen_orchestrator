"""Provisioner trigger utilities.

Provides functions to publish provisioning triggers to Redis.
LangGraph service listens for these triggers and executes provisioning.
"""

import json

import httpx
import redis.asyncio as redis
import structlog

from src.config import get_settings

logger = structlog.get_logger()

# Configuration from service settings
_settings = get_settings()
REDIS_URL = _settings.redis_url
API_BASE_URL = _settings.api_base_url

# Redis channel for provisioning triggers
PROVISIONER_TRIGGER_CHANNEL = "provisioner:trigger"


async def publish_provisioner_trigger(server_handle: str, is_incident_recovery: bool = False):
    """Publish a provisioning trigger event to Redis.

    This is called by health_checker and server_sync workers.
    LangGraph's provisioner worker listens for these events.

    Args:
        server_handle: Server handle to provision
        is_incident_recovery: True if this is incident recovery
    """
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)

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


async def retry_pending_servers():
    """Re-publish provisioning triggers for servers stuck in pending_setup.

    Called at scheduler startup to handle race conditions where triggers
    were published before LangGraph subscribed to the channel.
    """
    logger.info("retry_pending_servers_start")

    try:
        async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=30) as client:
            # Fetch servers with pending_setup status
            resp = await client.get("/api/servers/", params={"status": "pending_setup"})
            resp.raise_for_status()
            servers = resp.json()

            if not servers:
                logger.info("retry_pending_servers_none_found")
                return

            logger.info("retry_pending_servers_found", count=len(servers))

            for server in servers:
                server_handle = server.get("handle")
                if server_handle:
                    await publish_provisioner_trigger(server_handle, is_incident_recovery=False)
                    logger.info("retry_pending_server_triggered", server_handle=server_handle)

            logger.info("retry_pending_servers_complete", triggered=len(servers))

    except Exception as e:
        logger.error(
            "retry_pending_servers_failed",
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
