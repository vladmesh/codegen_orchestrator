"""Brainstorm agent node.

First node in the orchestrator graph. Gathers requirements from user,
asks clarifying questions, and creates project spec.

Refactored to use BaseAgentNode for dynamic prompt loading from database.
"""

from typing import Any

from langchain_core.messages import SystemMessage

from ..tools.database import create_project
from .base import BaseAgentNode

# Fallback prompt used when API is unavailable
FALLBACK_PROMPT = """You are Brainstorm, the first agent in the codegen orchestrator.

Your job:
1. Understand what project the user wants to create
2. Ask clarifying questions if requirements are unclear (max 2-3 rounds)
3. When requirements are clear, create the project using the create_project tool

## Available modules (from service-template):
- **backend**: FastAPI REST API with PostgreSQL
- **tg_bot**: Telegram bot message handler
- **notifications_worker**: Background notifications processor

## Entry points:
- **telegram**: Needs a Telegram bot. **YOU MUST ASK FOR THE TELEGRAM BOT TOKEN**.
- **frontend**: Web UI (needs domain allocation)
- **api**: REST API (needs port allocation)

## Guidelines:
- Ask about: main functionality, which entry points needed, any external APIs
- **If user wants a Telegram bot, explicitly ask for the Bot Token.**
- Project name should be snake_case (e.g., weather_bot)
- When ready, call create_project with all gathered info (including telegram_token if applicable)
- Respond in the SAME LANGUAGE as the user
"""


class BrainstormNode(BaseAgentNode):
    """Brainstorm agent that gathers requirements and creates projects."""

    @property
    def fallback_prompt(self) -> str:
        return FALLBACK_PROMPT

    @property
    def fallback_temperature(self) -> float:
        return 0.7  # Brainstorm is more creative

    def handle_tool_result(
        self, tool_name: str, result: Any, state: dict
    ) -> dict[str, Any]:
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
