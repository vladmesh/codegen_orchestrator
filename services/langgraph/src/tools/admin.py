"""Admin capability tools for Dynamic ProductOwner.

Provides tools for system administration and manual control.
Phase 4.5 addition.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import Annotated
from uuid import uuid4

from langchain_core.tools import tool
import structlog

from shared.queues import ADMIN_QUEUE
from shared.redis_client import RedisStreamClient

# NOTE: get_current_state imported inside functions to avoid circular import

logger = structlog.get_logger(__name__)


@tool
def list_graph_nodes() -> dict:
    """List all available graph nodes that can be triggered manually.

    Returns:
        {
            "nodes": [
                {"name": "intent_parser", "type": "llm", "description": "..."},
                {"name": "product_owner", "type": "llm", "description": "..."},
                {"name": "zavhoz", "type": "function", "description": "..."},
                ...
            ]
        }
    """
    # Define known nodes with descriptions
    # This is a static list for now - could be made dynamic from graph definition
    nodes = [
        {
            "name": "intent_parser",
            "type": "llm",
            "description": "Parses user intent and selects capabilities",
        },
        {
            "name": "product_owner",
            "type": "llm",
            "description": "Main orchestrator agent with dynamic tools",
        },
        {
            "name": "analyst",
            "type": "llm",
            "description": "Analyzes task requirements and creates specifications",
        },
        {
            "name": "architect",
            "type": "llm",
            "description": "Designs system architecture and selects modules",
        },
        {
            "name": "preparer",
            "type": "function",
            "description": "Prepares project structure using copier template",
        },
        {
            "name": "developer",
            "type": "llm",
            "description": "Implements code changes in coding worker container",
        },
        {
            "name": "zavhoz",
            "type": "function",
            "description": "Allocates server resources (ports, secrets)",
        },
        {
            "name": "devops",
            "type": "function",
            "description": "Deploys project using Ansible",
        },
    ]

    return {"nodes": nodes, "count": len(nodes)}


@tool
async def trigger_node_manually(
    node_name: Annotated[str, "Node to trigger (from list_graph_nodes)"],
    project_id: Annotated[str, "Project context for the node"],
    extra_input: Annotated[dict | None, "Additional input for the node"] = None,
) -> dict:
    """Manually trigger a specific graph node.

    WARNING: Admin tool. Use with caution.
    This bypasses normal graph routing and directly triggers a node.

    Args:
        node_name: Node to trigger (from list_graph_nodes)
        project_id: Project context
        extra_input: Additional input for the node (optional)

    Returns:
        {"job_id": "manual_xxx", "status": "triggered"}
    """
    from ..capabilities.base import get_current_state

    state = get_current_state()
    user_id = state.get("telegram_user_id")

    # Validate node name
    valid_nodes = {n["name"] for n in list_graph_nodes.invoke({})["nodes"]}
    if node_name not in valid_nodes:
        return {
            "error": f"Unknown node: {node_name}",
            "available_nodes": list(valid_nodes),
        }

    job_id = f"manual_{node_name}_{uuid4().hex[:8]}"

    # Build job payload
    job_data = {
        "job_id": job_id,
        "node_name": node_name,
        "project_id": project_id,
        "user_id": str(user_id),
        "extra_input": json.dumps(extra_input or {}),
        "queued_at": datetime.now(UTC).isoformat(),
    }

    # Publish to admin queue
    redis = RedisStreamClient()
    await redis.connect()

    try:
        await redis.publish(ADMIN_QUEUE, job_data)

        logger.info(
            "manual_trigger_queued",
            job_id=job_id,
            node_name=node_name,
            project_id=project_id,
            triggered_by=user_id,
        )

        return {
            "job_id": job_id,
            "status": "triggered",
            "node": node_name,
            "project_id": project_id,
        }

    finally:
        await redis.close()


@tool
async def clear_project_state(
    project_id: Annotated[str, "Project to reset"],
    confirm: Annotated[bool, "Must be True to proceed"] = False,
) -> dict:
    """Reset project state in orchestrator.

    WARNING: This clears all in-progress tasks and checkpoints for the project.
    Use this when a project is stuck or needs to be reset.

    Args:
        project_id: Project to reset
        confirm: Must be True to proceed

    Returns:
        {"cleared": True, "items_deleted": N}
    """
    if not confirm:
        return {
            "error": "Set confirm=True to clear state. This is destructive!",
            "project_id": project_id,
            "warning": "This will delete all in-progress jobs and checkpoints for this project.",
        }

    from ..capabilities.base import get_current_state

    state = get_current_state()
    user_id = state.get("telegram_user_id")

    redis = RedisStreamClient()
    await redis.connect()

    try:
        deleted = 0

        # Clear deploy jobs for this project
        # Search stream for entries matching project_id
        from shared.queues import DEPLOY_QUEUE, ENGINEERING_QUEUE

        for queue in [DEPLOY_QUEUE, ENGINEERING_QUEUE]:
            try:
                entries = await redis.redis.xrange(queue, count=1000)
                for entry_id, data in entries:
                    # Check if this entry belongs to the project
                    entry_project = data.get("project_id")
                    if entry_project == project_id:
                        await redis.redis.xdel(queue, entry_id)
                        deleted += 1
            except Exception as e:
                logger.warning(
                    "clear_queue_error",
                    queue=queue,
                    error=str(e),
                )

        logger.warning(
            "project_state_cleared",
            project_id=project_id,
            items_deleted=deleted,
            cleared_by=user_id,
        )

        return {
            "cleared": True,
            "project_id": project_id,
            "items_deleted": deleted,
        }

    finally:
        await redis.close()
