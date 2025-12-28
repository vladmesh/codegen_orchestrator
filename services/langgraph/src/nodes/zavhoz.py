"""Zavhoz (Resource Manager) agent node.

Zavhoz manages infrastructure by reading from internal DB.
All external API calls (Time4VPS) are done by background sync workers,
not by the agent directly. This saves tokens and keeps secrets isolated.

Uses LLMNode for dynamic prompt loading from database.
"""

from typing import Any

from langchain_core.messages import SystemMessage
import structlog

from ..tools import (
    allocate_port,
    find_suitable_server,
    get_next_available_port,
    get_project_status,
    get_server_info,
    list_managed_servers,
)
from .base import LLMNode, log_node_execution

logger = structlog.get_logger()

# Initialize Tools
tools = [
    list_managed_servers,
    find_suitable_server,
    get_server_info,
    allocate_port,
    get_next_available_port,
    get_project_status,
]


class ZavhozNode(LLMNode):
    """Resource manager agent that allocates servers and ports."""

    def handle_tool_result(self, tool_name: str, result: Any, state: dict) -> dict[str, Any]:
        """Handle allocate_port result to track allocated resources."""
        if tool_name == "allocate_port" and result:
            # Convert Pydantic model to dict if needed
            result_dict = result.model_dump() if hasattr(result, "model_dump") else result

            allocated_resources = (state.get("allocated_resources") or {}).copy()
            port_key = f"{result_dict.get('server_handle')}:{result_dict.get('port')}"
            allocated_resources[port_key] = result_dict
            return {"allocated_resources": allocated_resources}
        return {}


# Create singleton instance
_node = ZavhozNode("zavhoz", tools)


@log_node_execution("zavhoz")
async def run(state: dict) -> dict:
    """Run zavhoz agent.

    Reads infrastructure data from DB and allocates resources.
    """

    messages = state.get("messages", [])
    project_spec = state.get("project_spec") or {}
    po_intent = state.get("po_intent", "")
    current_project = state.get("current_project", "")

    # Build context for the agent
    context_parts = []
    if project_spec:
        context_parts.append(f"Project Spec: {project_spec}")
    if current_project:
        context_parts.append(f"Project ID: {current_project}")
    if po_intent:
        context_parts.append(f"Intent: {po_intent}")
        if po_intent == "deploy":
            context_parts.append("ACTION REQUIRED: Allocate server resources for deployment!")

    context = "\n".join(context_parts) if context_parts else "No project context."

    # Get dynamic prompt from database
    system_prompt = await _node.get_system_prompt()
    full_prompt = system_prompt + f"\n\n--- Current Context ---\n{context}"

    # Add system message if this is first invocation
    if not any(msg.type == "system" for msg in messages if hasattr(msg, "type")):
        system_msg = SystemMessage(content=full_prompt)
        messages = [system_msg] + list(messages)

    # Get configured LLM
    llm_with_tools = await _node.get_llm_with_tools()

    # Invoke LLM
    response = await llm_with_tools.ainvoke(messages)

    return {
        "messages": [response],
        "current_agent": "zavhoz",
    }


@log_node_execution("zavhoz_tools")
async def execute_tools(state: dict) -> dict:
    """Execute tool calls from Zavhoz LLM.

    Delegates to LLMNode.execute_tools which handles:
    - Tool execution with error handling
    - State updates via handle_tool_result (allocated_resources)
    """
    return await _node.execute_tools(state)
