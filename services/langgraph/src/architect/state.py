"""Architect agent state."""

from langgraph.prebuilt.chat_agent_executor import AgentState


class ArchitectState(AgentState):
    """State for the architect ReAct agent.

    Inherits messages from AgentState. Adds story/project context
    that the consumer injects before the first LLM call.
    """

    story_id: str
    project_id: str
    user_id: str
