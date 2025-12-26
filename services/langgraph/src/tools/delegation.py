"""Delegation tools for inter-agent communication."""

from langchain_core.tools import tool


@tool
async def delegate_to_analyst(task_description: str) -> dict:
    """Delegate a requirements analysis task to the Analyst.

    Use this when the user describes a NEW PROJECT or wants to CHANGE/UPDATE
    project requirements. The Analyst will analyze the requirements, ask
    clarifying questions if needed, and create/update the project specification.

    Args:
        task_description: Description of what the user wants (the original request)

    Returns:
        Dict indicating delegation was successful
    """
    return {
        "delegated": True,
        "task_description": task_description,
    }
