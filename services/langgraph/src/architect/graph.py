"""Architect ReAct agent graph.

Creates a LangGraph ReactAgent for story decomposition into tasks.
Uses MemorySaver only (one-shot sessions, no persistent checkpointing needed).
"""

from __future__ import annotations

from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import create_react_agent
import structlog

from ..prompts.architect import SYSTEM_PROMPT
from .state import ArchitectState
from .tools import get_architect_tools

logger = structlog.get_logger(__name__)


def create_architect_graph(
    model: str,
    base_url: str,
    api_key: str,
) -> CompiledStateGraph:
    """Create and compile the Architect ReactAgent graph.

    Args:
        model: LLM model name (e.g. "anthropic/claude-sonnet-4").
        base_url: LLM API base URL (e.g. "https://openrouter.ai/api/v1").
        api_key: LLM API key.
    """
    llm = ChatOpenAI(
        model=model,
        base_url=base_url,
        api_key=api_key,
    )

    checkpointer = MemorySaver()

    return create_react_agent(
        model=llm,
        tools=get_architect_tools(),
        prompt=SYSTEM_PROMPT,
        state_schema=ArchitectState,
        checkpointer=checkpointer,
    )
