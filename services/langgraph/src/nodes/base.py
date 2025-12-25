"""Base agent node class with common functionality.

Provides:
- Dynamic prompt loading from database (no fallbacks - fail fast)
- Common tool execution logic
- Error handling and logging
"""

import logging
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI

from ..config.agent_config import get_agent_config

logger = logging.getLogger(__name__)


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
        return await get_agent_config(self.agent_id)

    async def get_llm_with_tools(self):
        """Get LLM with bound tools, configured from API."""
        config = await self.get_config()
        llm = ChatOpenAI(
            model=config.get("model_name", "gpt-4o"),
            temperature=config.get("temperature", 0.0),
        )
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

    async def _execute_single_tool(
        self, tool_call: dict, state: dict
    ) -> dict[str, Any]:
        """Execute a single tool call with error handling."""
        tool_name = tool_call["name"]
        tool_func = self.tools_map.get(tool_name)

        if not tool_func:
            logger.warning(f"Unknown tool called: {tool_name}")
            return {
                "message": ToolMessage(
                    content=f"Unknown tool: {tool_name}",
                    tool_call_id=tool_call["id"],
                )
            }

        try:
            result = await tool_func.ainvoke(tool_call["args"])
            
            # Let subclass handle the result for custom state updates
            state_updates = self.handle_tool_result(tool_name, result, state)
            
            return {
                "message": ToolMessage(
                    content=f"Result: {result}",
                    tool_call_id=tool_call["id"],
                ),
                "state_updates": state_updates,
            }
        except Exception as e:
            logger.exception(f"Tool {tool_name} failed: {e}")
            return {
                "message": ToolMessage(
                    content=f"Error executing {tool_name}: {e!s}",
                    tool_call_id=tool_call["id"],
                )
            }

    def handle_tool_result(
        self, tool_name: str, result: Any, state: dict
    ) -> dict[str, Any]:
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
