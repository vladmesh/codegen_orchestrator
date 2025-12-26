"""Analyst agent node.

Analyzes user requirements, asks clarifying questions, and creates project specs.
Handles all new project creation and requirements analysis.

Uses LLMNode for dynamic prompt loading from database.
"""

from typing import Any

from langchain_core.messages import SystemMessage

from ..tools import create_project, create_project_spec_yaml, get_project_spec, update_project_spec
from .base import LLMNode, log_node_execution


class AnalystNode(LLMNode):
    """Analyst agent for requirements analysis and spec generation."""

    def handle_tool_result(self, tool_name: str, result: Any, state: dict) -> dict[str, Any]:
        """Handle create_project result to update state."""
        if tool_name == "create_project" and result:
            return {
                "current_project": result.get("id"),
                "project_spec": result.get("config", {}),
            }
        return {}


# Create singleton instance
_node = AnalystNode(
    "analyst", [create_project, get_project_spec, update_project_spec, create_project_spec_yaml]
)


@log_node_execution("analyst")
async def run(state: dict) -> dict:
    """Run analyst agent.

    Analyzes user requirements and creates project specification.
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
        "current_agent": "analyst",
    }


@log_node_execution("analyst_tools")
async def execute_tools(state: dict) -> dict:
    """Execute tool calls from Analyst LLM.

    Delegates to LLMNode.execute_tools which handles:
    - Tool execution with error handling
    - State updates via handle_tool_result
    """
    return await _node.execute_tools(state)
