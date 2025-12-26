"""Base agent node class with common functionality.

Provides:
- Dynamic prompt loading from database (no fallbacks - fail fast)
- Common tool execution logic
- Error handling and logging
- Node execution decorator for structured logging
"""

from collections.abc import Callable
from functools import wraps
import time
from typing import Any, TypeVar

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
import structlog

from ..config.agent_config_cache import agent_config_cache
from ..llm.factory import LLMFactory

logger = structlog.get_logger()

T = TypeVar("T")


def log_node_execution(node_name: str) -> Callable:
    """Decorator to log node start/end and inject context.

    Args:
        node_name: Name of the node for logging context.

    Returns:
        Decorated async function with structured logging.
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            state: dict | None = None
            if args:
                if isinstance(args[0], dict):
                    state = args[0]
                elif len(args) > 1 and isinstance(args[1], dict):
                    state = args[1]

            if state is None:
                state = kwargs.get("state") if isinstance(kwargs.get("state"), dict) else {}

            preexisting_context = structlog.contextvars.get_contextvars()
            bound_thread = False

            bind_kwargs: dict[str, Any] = {"node": node_name}
            thread_id = state.get("thread_id") if isinstance(state, dict) else None
            if thread_id and "thread_id" not in preexisting_context:
                bind_kwargs["thread_id"] = thread_id
                bound_thread = True

            structlog.contextvars.bind_contextvars(**bind_kwargs)

            logger.info("node_start")
            start = time.time()

            try:
                result = await func(*args, **kwargs)
                duration = (time.time() - start) * 1000

                state_updates = list(result.keys()) if isinstance(result, dict) else []
                logger.info(
                    "node_complete",
                    duration_ms=round(duration, 2),
                    state_updates=state_updates,
                )

                return result

            except Exception as e:
                duration = (time.time() - start) * 1000

                logger.error(
                    "node_failed",
                    duration_ms=round(duration, 2),
                    error=str(e),
                    error_type=type(e).__name__,
                    exc_info=True,
                )
                raise
            finally:
                structlog.contextvars.unbind_contextvars("node")
                if bound_thread:
                    structlog.contextvars.unbind_contextvars("thread_id")

        return wrapper

    return decorator


class BaseAgentNode:
    """Base class for all LangGraph agent nodes.

    Handles:
    - Fetching agent config (prompt, model, temperature) from API
    - Common tool execution with error handling
    - State updates based on tool results

    IMPORTANT: No fallbacks - if API is unavailable, we fail fast.
    This ensures configuration issues are caught immediately.
    """

    def __init__(self, agent_id: str, tools: list[BaseTool]):
        """Initialize the agent node.

        Args:
            agent_id: Identifier for fetching config (e.g., "brainstorm", "product_owner")
            tools: List of LangChain tools available to this agent
        """
        self.agent_id = agent_id
        self.tools = tools
        self.tools_map = {tool.name: tool for tool in tools}

    async def get_config(self) -> dict[str, Any]:
        """Get agent configuration from API.

        Returns:
            Config dict with keys: system_prompt, model_name, temperature

        Raises:
            AgentConfigError: If config cannot be fetched
        """
        return await agent_config_cache.get(self.agent_id)

    async def get_llm_with_tools(self):
        """Get LLM with bound tools, configured from API."""
        config = await self.get_config()
        llm = LLMFactory.create_llm(config)
        return llm.bind_tools(self.tools)

    async def get_system_prompt(self) -> str:
        """Get system prompt from config."""
        config = await self.get_config()
        return config["system_prompt"]

    async def execute_tools(self, state: dict) -> dict:
        """Execute tool calls from the last message.

        Common implementation that handles:
        - Extracting tool calls from message
        - Executing each tool
        - Collecting results into ToolMessage objects
        - Calling handle_tool_result for custom state updates

        Args:
            state: Current graph state

        Returns:
            State update dict with messages and any custom updates
        """
        messages = state.get("messages", [])
        if not messages:
            return {"messages": []}

        last_message = messages[-1]

        if not hasattr(last_message, "tool_calls") or not last_message.tool_calls:
            return {"messages": []}

        tool_results = []
        state_updates = {}

        for tool_call in last_message.tool_calls:
            result = await self._execute_single_tool(tool_call, state)
            tool_results.append(result["message"])

            # Merge any state updates from tool handling
            updates = result.get("state_updates", {})
            state_updates.update(updates)

        return {"messages": tool_results, **state_updates}

    async def _execute_single_tool(self, tool_call: dict, state: dict) -> dict[str, Any]:
        """Execute a single tool call with error handling."""
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

            # Let subclass handle the result for custom state updates
            state_updates = self.handle_tool_result(tool_name, result, state)

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

    def handle_tool_result(self, tool_name: str, result: Any, state: dict) -> dict[str, Any]:
        """Handle tool result and return state updates.

        Override in subclasses to add custom logic for specific tools.

        Args:
            tool_name: Name of the executed tool
            result: Tool execution result
            state: Current graph state

        Returns:
            Dict of state updates to apply
        """
        return {}
