"""Brainstorm agent node."""

from langchain_core.messages import AIMessage


def run(state: dict) -> dict:
    """Run brainstorm agent.

    Analyzes user request and creates project specification.
    """
    messages = state.get("messages", [])

    # TODO: Implement actual LLM call
    # For now, just echo back
    last_message = messages[-1].content if messages else "No message"

    return {
        "messages": [AIMessage(content=f"Brainstorm received: {last_message}")],
        "current_agent": "brainstorm",
        "project_spec": {"name": "test_project", "modules": ["backend"]},
    }
