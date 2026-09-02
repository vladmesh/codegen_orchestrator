"""Architect agent state."""

from langgraph.prebuilt.chat_agent_executor import AgentState

from shared.contracts.dto.product_brief import MustRequirement


class ArchitectState(AgentState):
    """State for the architect ReAct agent.

    Inherits messages from AgentState. Adds story/project context
    that the consumer injects before the first LLM call.

    The three Product Brief fields are the planning identity of this run, and
    the consumer sets all three on every run — `None`, `None` and `[]` for a
    story with no brief. They are state rather than tool arguments because the
    LLM must not be able to invent a planning attempt id: tools take them
    through `InjectedState`, so the model never sees them in a tool schema and
    cannot address a plan it does not own.
    """

    story_id: str
    project_id: str
    telegram_chat_id: str
    #: The brief backing this story, when there is one.
    product_brief_id: str | None
    #: The attempt this run plans under. `None` means this run is not planning
    #: under a brief, and then no task carries an attempt and nothing is admitted.
    planning_attempt_id: str | None
    #: What the user confirmed. Every id here needs exactly one disposition.
    must_requirements: list[MustRequirement]
