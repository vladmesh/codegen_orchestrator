"""Architect agent node.

Simplified Architect that:
1. Creates GitHub repository
2. Selects modules from service-template (backend, tg_bot, etc.)
3. Sets deployment hints and project complexity
4. Passes control to Preparer (copier) and then Developer (Factory.ai)

Uses LLMNode for dynamic prompt loading from database.
"""

from typing import Any

from langchain_core.messages import SystemMessage
import structlog

from ..tools.architect_tools import (
    customize_task_instructions,
    select_modules,
    set_deployment_hints,
    set_project_complexity,
)
from ..tools.github import create_github_repo, get_github_token
from .base import LLMNode, log_node_execution

logger = structlog.get_logger()


# Tools available to architect (simplified - no more Factory.ai spawning)
tools = [
    create_github_repo,
    get_github_token,
    select_modules,
    set_deployment_hints,
    customize_task_instructions,
    set_project_complexity,
]


class ArchitectNode(LLMNode):
    """Architect agent that creates project structure.

    Simplified to only handle:
    - Repository creation
    - Module selection
    - Deployment hints
    - Project complexity

    The actual code scaffolding is done by the Preparer node.
    """

    def handle_tool_result(self, tool_name: str, result: Any, state: dict) -> dict[str, Any]:
        """Handle tool results and update state accordingly."""
        updates = {}

        if tool_name == "create_github_repo" and result:
            updates["repo_info"] = result.model_dump() if hasattr(result, "model_dump") else result

        if tool_name == "select_modules" and result and not result.startswith("Error"):
            # Parse modules from result string: "Selected modules: ['backend', 'tg_bot']"
            import ast

            try:
                modules_str = result.split(": ", 1)[1]
                modules = ast.literal_eval(modules_str)
                updates["selected_modules"] = modules
            except (ValueError, IndexError, SyntaxError):
                logger.warning("failed_to_parse_modules", result=result)

        if tool_name == "set_deployment_hints" and result and not result.startswith("Error"):
            # Parse hints from result: "Deployment hints saved: {...}"
            import ast

            try:
                hints_str = result.split(": ", 1)[1]
                hints = ast.literal_eval(hints_str)
                updates["deployment_hints"] = hints
            except (ValueError, IndexError, SyntaxError):
                logger.warning("failed_to_parse_hints", result=result)

        if tool_name == "customize_task_instructions" and result and not result.startswith("Error"):
            # Store the instructions from tool call args, not the confirmation message
            # The actual instructions are in the tool call, not the result
            pass  # Handled separately in execute_tools

        if tool_name == "set_project_complexity" and result and not result.startswith("Error"):
            # Parse complexity from result: "Project complexity set to: simple"
            try:
                complexity = result.split(": ", 1)[1]
                updates["project_complexity"] = complexity
            except (ValueError, IndexError):
                logger.warning("failed_to_parse_complexity", result=result)

        return updates


# Create singleton instance
_node = ArchitectNode("architect", tools)


@log_node_execution("architect")
async def run(state: dict) -> dict:
    """Run architect agent.

    Creates GitHub repo and prepares for code generation.
    """
    messages = state.get("messages", [])
    project_spec = state.get("project_spec") or {}
    allocated_resources = state.get("allocated_resources") or {}

    # Debug: log what project_spec we received
    logger.info(
        "architect_run_project_spec",
        project_spec_exists=bool(project_spec),
        project_spec_type=type(state.get("project_spec")).__name__,
        project_spec_name=project_spec.get("name") if project_spec else None,
        project_spec_keys=list(project_spec.keys()) if project_spec else None,
        raw_project_spec=state.get("project_spec"),
    )

    # Build context for LLM
    project_info = f"""
Name: {project_spec.get("name", "unknown")}
Description: {project_spec.get("description", "No description")}
Modules: {project_spec.get("modules", [])}
Entry Points: {project_spec.get("entry_points", [])}

Detailed Spec:
{project_spec.get("detailed_spec", "N/A")}
"""

    # Get dynamic prompt from database
    system_prompt_template = await _node.get_system_prompt()

    # Replace placeholders in prompt
    system_content = system_prompt_template.format(
        project_info=project_info,
        allocated_resources=allocated_resources,
    )

    llm_messages = [SystemMessage(content=system_content)]
    llm_messages.extend(messages)

    # Get configured LLM
    llm_with_tools = await _node.get_llm_with_tools()

    # Invoke LLM
    response = await llm_with_tools.ainvoke(llm_messages)

    return {
        "messages": [response],
        "current_agent": "architect",
    }


@log_node_execution("architect_tools")
async def execute_tools(state: dict) -> dict:
    """Execute tool calls from Architect LLM.

    Delegates to LLMNode.execute_tools which handles:
    - Tool execution with error handling
    - State updates via handle_tool_result

    Special handling for customize_task_instructions to extract
    the actual instruction text from tool call args.
    """
    # First, extract custom_task_instructions from tool calls if present
    messages = state.get("messages", [])
    custom_instructions = None

    if messages:
        last_message = messages[-1]
        if hasattr(last_message, "tool_calls"):
            for tool_call in last_message.tool_calls:
                if tool_call["name"] == "customize_task_instructions":
                    custom_instructions = tool_call["args"].get("instructions", "")
                    break

    # Execute tools via LLMNode
    result = await _node.execute_tools(state)

    # Add custom_task_instructions if it was set
    if custom_instructions:
        result["custom_task_instructions"] = custom_instructions

    return result


# Note: ArchitectWorkerNode (spawn_factory_worker) has been removed.
# Project scaffolding is now handled by the Preparer node which runs copier
# and writes TASK.md/AGENTS.md. See nodes/preparer.py.
