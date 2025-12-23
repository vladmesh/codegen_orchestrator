"""Zavhoz (Resource Manager) agent node."""

from langchain_core.messages import AIMessage


def run(state: dict) -> dict:
    """Run zavhoz agent.

    Allocates resources for the project.
    LLM decides WHICH resources, Python code fetches the actual secrets.
    """
    project_spec = state.get("project_spec", {})
    project_name = project_spec.get("name", "unknown")

    # TODO: Implement actual resource allocation
    # 1. LLM decides what resources are needed
    # 2. Python code calls API to get/create resource handles
    # 3. Return handles (not secrets!) in state

    allocated = {
        "server": "prod_vps_1",
        "telegram_bot": f"handle_{project_name}",
    }

    return {
        "messages": [AIMessage(content=f"Allocated resources: {allocated}")],
        "current_agent": "zavhoz",
        "allocated_resources": allocated,
    }
