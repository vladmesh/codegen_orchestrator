"""DevOps agent node.

Orchestrates the deployment of the application using Ansible.
Runs after the Developer agent/worker has completed implementation.
"""

from typing import Any

from langchain_core.messages import SystemMessage
import structlog

from ..tools.devops_tools import (
    analyze_env_requirements,
    generate_infra_secret,
    get_project_context,
    run_ansible_deploy,
)
from .base import LLMNode, log_node_execution

logger = structlog.get_logger()


# Tools available to devops
tools = [
    analyze_env_requirements,
    generate_infra_secret,
    get_project_context,
    run_ansible_deploy,
]


class DevOpsNode(LLMNode):
    """DevOps agent node for deployment."""

    def handle_tool_result(self, tool_name: str, result: Any, state: dict) -> dict[str, Any]:
        """Handle tool results and update state accordingly."""
        updates = {}

        if tool_name == "run_ansible_deploy":
            # If deployment succeeded, we might want to update some state
            # but usually the tool result message is enough for the LLM to know
            if isinstance(result, dict) and result.get("status") == "success":
                updates["deployed_url"] = result.get("deployed_url")

        return updates


# Create singleton instance
devops_node = DevOpsNode("devops", tools)


@log_node_execution("devops")
async def run(state: dict) -> dict:
    """Run devops agent.

    1. Get system prompt from DB
    2. Build context
    3. Invoke LLM
    """
    messages = state.get("messages", [])
    project_spec = state.get("project_spec") or {}
    allocated_resources = state.get("allocated_resources") or {}
    repo_info = state.get("repo_info") or {}

    # Build context for LLM
    # We provide minimal context because the LLM can use tools to get more
    context_str = f"""
Project Name: {project_spec.get("name", "unknown")}
Repository: {repo_info.get("full_name") or repo_info.get("html_url", "unknown")}
Allocated Resources: {allocated_resources}
"""

    # Get dynamic prompt from database
    system_prompt_template = await devops_node.get_system_prompt()

    # Replace placeholders in prompt
    system_content = system_prompt_template.format(project_context=context_str)

    llm_messages = [SystemMessage(content=system_content)]
    llm_messages.extend(messages)

    # Get configured LLM
    llm_with_tools = await devops_node.get_llm_with_tools()

    # Invoke LLM
    response = await llm_with_tools.ainvoke(llm_messages)

    return {
        "messages": [response],
        "current_agent": "devops",
    }


@log_node_execution("devops_tools")
async def execute_tools(state: dict) -> dict:
    """Execute tool calls from DevOps LLM."""
    return await devops_node.execute_tools(state)
