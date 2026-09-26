"""Architect ReAct agent graph.

Creates a LangGraph ReactAgent for story decomposition into tasks.
Uses MemorySaver only (one-shot sessions, no persistent checkpointing needed).
"""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import create_react_agent
import structlog

from ...prompts.architect import SYSTEM_PROMPT
from .state import ArchitectState
from .tools import get_architect_tools

logger = structlog.get_logger(__name__)


def create_architect_graph(llm: BaseChatModel) -> CompiledStateGraph:
    """Create and compile the Architect ReactAgent graph.

    Args:
        llm: The Architect's LLM channel chain (`src.llm.build_agent_llm`).
    """
    return create_react_agent(
        model=llm,
        tools=get_architect_tools(),
        prompt=SYSTEM_PROMPT,
        state_schema=ArchitectState,
    )
