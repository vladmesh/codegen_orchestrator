"""Provisioner trigger utilities.

Provides functions to publish provisioning triggers to Redis.
LangGraph service listens for these triggers and executes provisioning.
"""

import json

import redis.asyncio as redis
import structlog

from shared.contracts.dto.server import ServerStatus
from shared.provisioning_policy import provider_operation_is_authorized
from src.clients.api import api_client
from src.config import get_settings

logger = structlog.get_logger()

# Configuration from service settings
_settings = get_settings()
REDIS_URL = _settings.redis_url

# Redis channel for provisioning triggers
PROVISIONER_TRIGGER_CHANNEL = "provisioner:trigger"


async def publish_provisioner_trigger(
    server_handle: str, is_incident_recovery: bool = False
) -> bool:
    """Publish a provisioning trigger event to Redis.

    This is called by scheduler provisioning and startup-retry paths.
    LangGraph's provisioner worker listens for these events.

    Args:
        server_handle: Server handle to provision
        is_incident_recovery: True if this is incident recovery
    """
    server = await api_client.get_server(server_handle)
    if not provider_operation_is_authorized(
        provider=server.provider,
        provider_id=server.provider_id,
        is_managed=server.is_managed,
    ):
        logger.warning(
            "provisioner_trigger_not_authorized",
            server_handle=server_handle,
            provider=server.provider,
            provider_id=server.provider_id,
            is_managed=server.is_managed,
        )
        return False

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
        return True
    except Exception as e:
        logger.error(
            "provisioner_trigger_publish_failed",
            server_handle=server_handle,
            is_incident_recovery=is_incident_recovery,
            error=str(e),
            error_type=type(e).__name__,
        )
        raise
    finally:
        await redis_client.close()


async def retry_pending_servers():
    """Re-publish provisioning triggers for servers stuck in pending_setup.

    Called at scheduler startup to handle race conditions where triggers
    were published before LangGraph subscribed to the channel.

    Failures propagate: a scheduler that cannot re-publish the triggers must
    not start as if it had.
    """
    logger.info("retry_pending_servers_start")

    servers = await api_client.get_servers(status=ServerStatus.PENDING_SETUP)

    if not servers:
        logger.info("retry_pending_servers_none_found")
        return

    logger.info("retry_pending_servers_found", count=len(servers))

    triggered = 0
    for server in servers:
        if await publish_provisioner_trigger(server.handle, is_incident_recovery=False):
            triggered += 1
            logger.info("retry_pending_server_triggered", server_handle=server.handle)

    logger.info("retry_pending_servers_complete", triggered=triggered)
