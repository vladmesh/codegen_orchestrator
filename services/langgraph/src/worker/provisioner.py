"""Provisioner trigger handling via Redis pub/sub."""

import asyncio
from datetime import UTC, datetime, timedelta
import json

from langchain_core.messages import HumanMessage
import redis.asyncio as redis
import structlog

from ..config.settings import get_settings
from ..graph import OrchestratorState, create_graph

logger = structlog.get_logger()

PROVISIONER_TRIGGER_CHANNEL = "provisioner:trigger"
PROVISIONING_TRIGGER_COOLDOWN_SECONDS = 120

# Track active provisioning and cooldowns
active_provisioning: set[str] = set()
provisioning_cooldowns: dict[str, datetime] = {}

# Create graph instance for provisioning
graph = create_graph()


async def listen_provisioner_triggers() -> None:
    """Listen for provisioning triggers from Redis pub/sub."""
    settings = get_settings()
    try:
        client = redis.from_url(settings.redis_url, decode_responses=True)
        pubsub = client.pubsub()
        await pubsub.subscribe(PROVISIONER_TRIGGER_CHANNEL)

        logger.info("provisioner_subscribed", channel=PROVISIONER_TRIGGER_CHANNEL)

        async for message in pubsub.listen():
            if message["type"] == "message":
                try:
                    data = json.loads(message["data"])
                    await process_provisioning_trigger(data)
                except Exception as e:
                    logger.error(
                        "provisioner_trigger_processing_failed",
                        error=str(e),
                        error_type=type(e).__name__,
                        exc_info=True,
                    )
    except asyncio.CancelledError:
        logger.info("provisioner_listener_cancelled")
    except Exception as e:
        logger.error(
            "provisioner_listener_failed",
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
    finally:
        await client.close()


async def process_provisioning_trigger(data: dict) -> None:
    """Run the graph for provisioning."""
    server_handle = data.get("server_handle")
    is_incident_recovery = data.get("is_incident_recovery", False)

    if not server_handle:
        logger.warning("provisioner_trigger_missing_handle", payload=data)
        return

    structlog.contextvars.bind_contextvars(server_handle=server_handle, trigger="provisioner")

    logger.info("provisioner_trigger_received", is_incident_recovery=is_incident_recovery)

    now = datetime.now(UTC)
    if server_handle in active_provisioning:
        logger.info("provisioner_trigger_deduped", reason="active")
        structlog.contextvars.clear_contextvars()
        return

    last_complete = provisioning_cooldowns.get(server_handle)
    if last_complete and (now - last_complete) < timedelta(
        seconds=PROVISIONING_TRIGGER_COOLDOWN_SECONDS
    ):
        logger.info(
            "provisioner_trigger_deduped",
            reason="cooldown",
            last_complete_at=last_complete.isoformat(),
            cooldown_seconds=PROVISIONING_TRIGGER_COOLDOWN_SECONDS,
        )
        structlog.contextvars.clear_contextvars()
        return

    active_provisioning.add(server_handle)

    state: OrchestratorState = {
        "messages": [HumanMessage(content=f"Provision server {server_handle}")],
        "server_to_provision": server_handle,
        "is_incident_recovery": is_incident_recovery,
        "current_agent": "provisioner",
        "errors": [],
        # Initialize required fields
        "current_project": None,
        "project_spec": None,
        "project_intent": None,
        "po_intent": None,
        "allocated_resources": {},
        "deployed_url": None,
        "repo_info": None,
        "architect_complete": False,
        "project_complexity": None,
        "provisioning_result": None,
        # Dynamic PO Phase 2 fields
        "skip_intent_parser": True,  # Provisioner skips intent parser
        "thread_id": None,
        "active_capabilities": [],
        "task_summary": None,
        # Dynamic PO Phase 3 fields
        "chat_id": None,
        "correlation_id": None,
        "awaiting_user_response": False,
        "user_confirmed_complete": False,
        "po_iterations": 0,
    }

    config = {"configurable": {"thread_id": f"provisioner-{server_handle}"}, "recursion_limit": 60}

    try:
        await graph.ainvoke(state, config)
        logger.info("provisioning_graph_complete")
    except Exception as e:
        logger.error("provisioning_graph_failed", error=str(e), exc_info=True)
    finally:
        active_provisioning.discard(server_handle)
        provisioning_cooldowns[server_handle] = datetime.now(UTC)
        structlog.contextvars.clear_contextvars()
