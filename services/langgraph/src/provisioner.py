"""Provisioner trigger handling via Redis pub/sub."""

import asyncio
from datetime import UTC, datetime, timedelta
import json

from langchain_core.messages import HumanMessage
import redis.asyncio as redis
import structlog

from .config.settings import get_settings
from .graph import OrchestratorState, create_graph

logger = structlog.get_logger()

PROVISIONER_TRIGGER_CHANNEL = "provisioner:trigger"
PROVISIONING_TRIGGER_COOLDOWN_SECONDS = 120

# Track active provisioning and cooldowns
active_provisioning: set[str] = set()
provisioning_cooldowns: dict[str, datetime] = {}
background_tasks: set[asyncio.Task] = set()  # prevent GC of background tasks

# Create graph instance for provisioning
graph = create_graph()


async def listen_provisioner_triggers() -> None:
    """Listen for provisioning triggers from Redis pub/sub."""
    settings = get_settings()
    while True:
        client = None
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
                            error_type=type(e).__name__,
                        )
        except asyncio.CancelledError:
            logger.info("provisioner_listener_cancelled")
            return
        except TimeoutError:
            logger.debug("provisioner_listener_idle_reconnect")
        except Exception as e:
            logger.warning("provisioner_listener_reconnecting", error_type=type(e).__name__)
            await asyncio.sleep(1)
        finally:
            if client is not None:
                await client.aclose()


async def process_provisioning_trigger(data: dict) -> None:
    """Handle provisioning trigger - spawn background task to not block listener."""
    server_handle = data.get("server_handle")
    is_incident_recovery = data.get("is_incident_recovery", False)

    if not server_handle:
        logger.warning("provisioner_trigger_missing_handle", payload=data)
        return

    logger.info(
        "provisioner_trigger_received",
        server_handle=server_handle,
        is_incident_recovery=is_incident_recovery,
    )

    now = datetime.now(UTC)
    if server_handle in active_provisioning:
        logger.info("provisioner_trigger_deduped", server_handle=server_handle, reason="active")
        return

    last_complete = provisioning_cooldowns.get(server_handle)
    if last_complete and (now - last_complete) < timedelta(
        seconds=PROVISIONING_TRIGGER_COOLDOWN_SECONDS
    ):
        logger.info(
            "provisioner_trigger_deduped",
            server_handle=server_handle,
            reason="cooldown",
            last_complete_at=last_complete.isoformat(),
            cooldown_seconds=PROVISIONING_TRIGGER_COOLDOWN_SECONDS,
        )
        return

    # Mark as active before spawning task to prevent duplicates
    active_provisioning.add(server_handle)

    # Spawn background task to not block the pub/sub listener
    task = asyncio.create_task(
        _run_provisioning_graph(server_handle, is_incident_recovery),
        name=f"provisioner-{server_handle}",
    )
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)

    logger.info("provisioner_task_spawned", server_handle=server_handle)


async def _run_provisioning_graph(server_handle: str, is_incident_recovery: bool) -> None:
    """Execute the provisioning graph for a server."""
    structlog.contextvars.bind_contextvars(server_handle=server_handle, trigger="provisioner")

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
        "project_complexity": None,
        "provisioning_result": None,
        # Dynamic PO Phase 2 fields
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
