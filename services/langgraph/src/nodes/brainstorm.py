"""Brainstorm agent node.

First node in the orchestrator graph. Gathers requirements from user,
asks clarifying questions, and creates project spec.

Uses BaseAgentNode for dynamic prompt loading from database.
"""

from typing import Any

from langchain_core.messages import SystemMessage

from ..tools import create_project
from .base import BaseAgentNode


class BrainstormNode(BaseAgentNode):
    """Brainstorm agent that gathers requirements and creates projects."""

    def handle_tool_result(self, tool_name: str, result: Any, state: dict) -> dict[str, Any]:
        """Handle create_project result to update state."""
        if tool_name == "create_project" and result:
            return {
                "current_project": result.get("id"),
                "project_spec": result.get("config", {}),
            }
        return {}


# Create singleton instance
_node = BrainstormNode("brainstorm", [create_project])


async def run(state: dict) -> dict:
    """Run brainstorm agent.

    Analyzes user request and creates project specification.
    Uses LLM to have a conversation and gather requirements.
    """
    messages = state.get("messages", [])

    # Get dynamic prompt from database
    system_prompt = await _node.get_system_prompt()

    # Build message list with system prompt
    llm_messages = [SystemMessage(content=system_prompt)]
    llm_messages.extend(messages)

    # Get configured LLM
    llm_with_tools = await _node.get_llm_with_tools()

    # Invoke LLM
    response = await llm_with_tools.ainvoke(llm_messages)

    return {
        "messages": [response],
        "current_agent": "brainstorm",
    }


async def execute_tools(state: dict) -> dict:
    """Execute tool calls from Brainstorm LLM.

    Delegates to BaseAgentNode.execute_tools which handles:
    - Tool execution with error handling
    - State updates via handle_tool_result
    """
    return await _node.execute_tools(state)
