"""Base tools for Dynamic ProductOwner - always available.

These tools are loaded for every PO session regardless of capabilities:
- respond_to_user: Send messages to user (with optional wait)
- search_knowledge: Search project docs/code/history (RAG stub)
- request_capabilities: Request additional tool groups
- finish_task: Mark task as complete
"""

from typing import TYPE_CHECKING, Any

from langchain_core.tools import tool
import structlog

if TYPE_CHECKING:
    pass

logger = structlog.get_logger()


# State injection - these will be set by the tool executor node
_current_state: dict[str, Any] = {}
_redis_client: Any = None


def set_tool_context(state: dict[str, Any], redis_client: Any = None) -> None:
    """Set the current state context for base tools.

    Called by po_tools node before executing tools.
    """
    global _current_state, _redis_client
    _current_state = state
    _redis_client = redis_client


def get_current_state() -> dict[str, Any]:
    """Get current PO state for tools that need context."""
    return _current_state


@tool
async def respond_to_user(
    message: str,
    awaiting_response: bool = False,
) -> dict:
    """Send a message to the user via Telegram.

    Args:
        message: Text to send (supports markdown)
        awaiting_response: If True, pause and wait for user reply.
                          Use when you need user input to continue.

    Returns:
        {"sent": True, "awaiting": awaiting_response}

    Examples:
        Progress update (continue working):
            respond_to_user("Checking deployment readiness...")

        Ask question (wait for answer):
            respond_to_user("Which server? Found: vps-1, vps-2", awaiting_response=True)

        Final result (user will confirm separately):
            respond_to_user("Done! App deployed to http://1.2.3.4:8080")
    """
    from shared.redis_client import RedisStreamClient

    state = get_current_state()

    # Use injected redis client or create new one
    redis = _redis_client
    if redis is None:
        redis = RedisStreamClient()
        await redis.connect()

    await redis.publish(
        RedisStreamClient.OUTGOING_STREAM,
        {
            "user_id": state.get("telegram_user_id"),
            "chat_id": state.get("chat_id"),
            "text": message,
            "correlation_id": state.get("correlation_id"),
        },
    )

    logger.info(
        "respond_to_user_called",
        awaiting=awaiting_response,
        message_length=len(message),
    )

    return {"sent": True, "awaiting": awaiting_response}


@tool
async def search_knowledge(
    query: str,
    scope: str = "all",
) -> dict:
    """Search project documentation, code, conversation history, or logs.

    Args:
        query: Natural language search query
        scope: Where to search
            - "docs": Project documentation
            - "code": Source code
            - "history": Previous conversations
            - "logs": Service logs
            - "all": Search everywhere

    Returns:
        {"results": [{"source": "...", "content": "...", "relevance": 0.95}, ...]}

    Note: Full RAG integration coming in Phase 6. Currently uses basic project context search.
    """
    from ..tools.rag import search_project_context

    state = get_current_state()
    project_id = state.get("current_project")

    # If no project, return empty
    if not project_id:
        return {
            "results": [],
            "note": "No active project. Use project_management tools to select a project.",
        }

    # Use existing RAG tool
    try:
        result = await search_project_context.ainvoke(
            {"project_id": project_id, "query": query, "scope": "public", "limit": 5}
        )
        return {"results": result.get("results", []) if isinstance(result, dict) else []}
    except Exception as e:
        logger.warning("search_knowledge_failed", error=str(e), query=query)
        return {"results": [], "error": str(e)}


@tool
def request_capabilities(
    capabilities: list[str],
    reason: str,
) -> dict:
    """Request additional tools for the current task.

    New tools will be available on your next action.

    Available capabilities:
        - "deploy": Deploy projects (check, trigger, logs)
        - "infrastructure": Manage servers and ports
        - "project_management": Create/update projects
        - "engineering": Trigger code implementation
        - "diagnose": View logs, debug issues
        - "admin": Manual system control

    Args:
        capabilities: List of capability groups to enable
        reason: Brief explanation why these are needed

    Returns:
        {"enabled": [...], "new_tools": [...]}
    """
    from . import CAPABILITY_REGISTRY

    # Validation
    valid_caps = set(CAPABILITY_REGISTRY.keys())
    requested = set(capabilities)
    invalid = requested - valid_caps

    if invalid:
        return {"error": f"Unknown capabilities: {invalid}", "available": list(valid_caps)}

    # Get current capabilities from state
    state = get_current_state()
    current = set(state.get("active_capabilities", []))

    # Merge
    new_caps = requested - current
    all_caps = current | requested

    # Get new tool names
    new_tools = []
    for cap in new_caps:
        new_tools.extend(CAPABILITY_REGISTRY[cap]["tools"])

    logger.info(
        "capabilities_requested",
        requested=list(requested),
        new_caps=list(new_caps),
        reason=reason,
    )

    return {
        "enabled": list(all_caps),
        "new_tools": new_tools,
        "reason_logged": reason,
    }


@tool
def finish_task(summary: str) -> dict:
    """Mark the current task as complete and end the session.

    IMPORTANT: Only call this AFTER the user has confirmed the task is done.
    Look for phrases like "thanks", "got it", "perfect", "done", "ok", etc.

    After this, the next user message starts a fresh session with new thread_id.

    Args:
        summary: Brief summary of what was accomplished

    Returns:
        {"finished": True, "thread_id": "...", "summary": "..."}
    """
    state = get_current_state()
    thread_id = state.get("thread_id", "unknown")

    logger.info(
        "task_finished",
        thread_id=thread_id,
        summary=summary,
        user_id=state.get("telegram_user_id"),
    )

    return {
        "finished": True,
        "thread_id": thread_id,
        "summary": summary,
    }


# Export all base tools
BASE_TOOLS = [
    respond_to_user,
    search_knowledge,
    request_capabilities,
    finish_task,
]
