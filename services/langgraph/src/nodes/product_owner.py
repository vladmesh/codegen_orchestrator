"""Product Owner (PO) agent node - Dynamic version.

Phase 3: Uses dynamic tool loading based on capabilities from intent_parser.
Implements agentic loop with base tools always available.

Uses LLMNode for dynamic prompt loading from database.
"""

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from pydantic import BaseModel
import structlog

from shared.redis_client import RedisStreamClient

from ..capabilities import get_tools_for_capabilities
from ..state.context import set_tool_context
from .base import LLMNode, log_node_execution

logger = structlog.get_logger()

# Maximum iterations for PO agentic loop
MAX_PO_ITERATIONS = 20


class ProductOwnerNode(LLMNode):
    """Product Owner agent with dynamic tool loading."""

    pass


# Create singleton instance
_node = ProductOwnerNode("product_owner", [])  # Tools loaded dynamically


@log_node_execution("product_owner")
async def run(state: dict) -> dict:
    """Run Product Owner agent with dynamic tools.

    Phase 3 implementation:
    1. Check iteration limit
    2. Build tools dynamically from active_capabilities
    3. Invoke LLM with tools
    4. Return response for routing
    """
    # Check iteration limit
    iterations = state.get("po_iterations", 0)
    if iterations >= MAX_PO_ITERATIONS:
        logger.warning("po_max_iterations_reached", iterations=iterations)
        return {
            "messages": [
                AIMessage(content="⚠️ Достигнут лимит итераций. Пожалуйста, начните новый диалог.")
            ],
            "current_agent": "product_owner",
        }

    # Build dynamic tools from capabilities
    capabilities = state.get("active_capabilities", [])
    tools = get_tools_for_capabilities(capabilities)

    logger.info(
        "po_building_tools",
        capabilities=capabilities,
        tool_count=len(tools),
        tool_names=[t.name for t in tools],
    )

    # Get dynamic system prompt from database
    system_prompt = await _node.get_system_prompt()

    # Build messages
    messages = state.get("messages", [])
    llm_messages = [SystemMessage(content=system_prompt)]
    llm_messages.extend(messages)

    # Get LLM and bind tools
    llm = await _node.get_llm()
    llm_with_tools = llm.bind_tools(tools)

    # Invoke LLM
    response = await llm_with_tools.ainvoke(llm_messages)

    return {
        "messages": [response],
        "po_iterations": iterations + 1,
        "current_agent": "product_owner",
    }


@log_node_execution("product_owner_tools")
async def execute_tools(state: dict) -> dict:
    """Execute tool calls from Product Owner LLM.

    Phase 3: Handles base tools (respond_to_user, finish_task, request_capabilities)
    and sets appropriate state flags.
    """
    messages = state.get("messages", [])
    last_message = messages[-1]

    if not hasattr(last_message, "tool_calls") or not last_message.tool_calls:
        return {"messages": []}

    # Build tools map from current capabilities
    capabilities = state.get("active_capabilities", [])
    tools = get_tools_for_capabilities(capabilities)
    tools_map = {tool.name: tool for tool in tools}

    # Set context for base tools
    redis_client = RedisStreamClient()
    await redis_client.connect()
    set_tool_context(state, redis_client)

    tool_results = []
    updates: dict = {}

    for tool_call in last_message.tool_calls:
        tool_name = tool_call["name"]
        tool_func = tools_map.get(tool_name)

        if not tool_func:
            tool_results.append(
                ToolMessage(
                    content=f"Unknown tool: {tool_name}",
                    tool_call_id=tool_call["id"],
                )
            )
            continue

        # Execute tool
        try:
            result = await tool_func.ainvoke(tool_call["args"])
        except Exception as e:
            logger.error(
                "po_tool_execution_failed",
                tool=tool_name,
                error=str(e),
                exc_info=True,
            )
            tool_results.append(
                ToolMessage(
                    content=f"Error executing {tool_name}: {e!s}",
                    tool_call_id=tool_call["id"],
                )
            )
            continue

        # Convert Pydantic models to dicts
        if isinstance(result, BaseModel):
            result = result.model_dump()
        elif isinstance(result, list):
            result = [item.model_dump() if isinstance(item, BaseModel) else item for item in result]

        # Add tool result message
        tool_results.append(
            ToolMessage(
                content=str(result),
                tool_call_id=tool_call["id"],
            )
        )

        # Handle base tool side effects
        if tool_name == "respond_to_user":
            if isinstance(result, dict) and result.get("awaiting"):
                updates["awaiting_user_response"] = True
                logger.info("po_awaiting_user_response")

        elif tool_name == "finish_task":
            if isinstance(result, dict) and result.get("finished"):
                updates["user_confirmed_complete"] = True
                logger.info("po_task_finished", summary=result.get("summary"))

        elif tool_name == "request_capabilities":
            if isinstance(result, dict) and result.get("enabled"):
                updates["active_capabilities"] = result["enabled"]
                logger.info(
                    "po_capabilities_updated",
                    new_capabilities=result["enabled"],
                )

    await redis_client.close()

    updates["messages"] = tool_results
    return updates
