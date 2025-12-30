"""Tool executor for LLM nodes.

Handles tool execution logic extracted from LLMNode to improve modularity
and testability.
"""

from collections.abc import Callable
import time
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
import structlog

logger = structlog.get_logger()


class ToolExecutor:
    """Handles tool execution for LLM nodes.

    Provides centralized tool execution with:
    - Error handling and logging
    - Pydantic model serialization
    - Result handler callbacks for custom state updates
    """

    def __init__(
        self,
        tools_map: dict[str, BaseTool],
        result_handler: Callable[[str, Any, dict], dict[str, Any]] | None = None,
    ):
        """Initialize tool executor.

        Args:
            tools_map: Mapping of tool names to tool instances
            result_handler: Optional callback to handle tool results and return state updates.
                          Signature: (tool_name, result, state) -> state_updates
        """
        self.tools_map = tools_map
        self.result_handler = result_handler

    async def execute_tools(self, tool_calls: list, state: dict) -> dict:
        """Execute multiple tool calls and return results.

        Args:
            tool_calls: List of tool call dicts with 'name', 'args', and 'id'
            state: Current graph state

        Returns:
            Dict with 'messages' key containing ToolMessage objects,
            plus any state updates from result_handler
        """
        tool_results = []
        state_updates = {}

        for tool_call in tool_calls:
            result = await self.execute_single_tool(tool_call, state)
            tool_results.append(result["message"])

            # Merge any state updates from tool handling
            updates = result.get("state_updates", {})
            state_updates.update(updates)

        return {"messages": tool_results, **state_updates}

    async def execute_single_tool(self, tool_call: dict, state: dict) -> dict[str, Any]:
        """Execute a single tool call with error handling.

        Args:
            tool_call: Dict with 'name', 'args', and 'id' keys
            state: Current graph state

        Returns:
            Dict with 'message' (ToolMessage) and optional 'state_updates' keys
        """
        tool_name = tool_call["name"]
        tool_func = self.tools_map.get(tool_name)
        tool_call_id = tool_call.get("id")

        if not tool_func:
            logger.warning("unknown_tool_called", tool_name=tool_name)
            return {
                "message": ToolMessage(
                    content=f"Unknown tool: {tool_name}",
                    tool_call_id=tool_call["id"],
                )
            }

        logger.info(
            "tool_execution_start",
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            args=tool_call.get("args", {}),
        )
        start = time.time()

        try:
            result = await tool_func.ainvoke(tool_call["args"])

            duration = (time.time() - start) * 1000

            logger.info(
                "tool_execution_complete",
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                duration_ms=round(duration, 2),
                result_type=type(result).__name__,
            )

            # Let result handler process the result for custom state updates
            state_updates = {}
            if self.result_handler:
                state_updates = self.result_handler(tool_name, result, state)

            # Serialize Pydantic models if necessary
            content_result = result
            if hasattr(result, "model_dump"):
                content_result = result.model_dump()
            elif hasattr(result, "dict"):
                content_result = result.dict()

            return {
                "message": ToolMessage(
                    content=f"Result: {content_result}",
                    tool_call_id=tool_call["id"],
                ),
                "state_updates": state_updates,
            }
        except Exception as e:
            duration = (time.time() - start) * 1000
            logger.error(
                "tool_execution_failed",
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                duration_ms=round(duration, 2),
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            return {
                "message": ToolMessage(
                    content=f"Error executing {tool_name}: {e!s}",
                    tool_call_id=tool_call["id"],
                )
            }
