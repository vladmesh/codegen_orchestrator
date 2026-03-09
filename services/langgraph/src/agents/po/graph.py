"""PO ReactAgent graph.

Creates a LangGraph ReactAgent with PO tools, ChatOpenAI (OpenRouter-compatible),
PostgreSQL or MemorySaver checkpointer, and conversation summarization.
"""

from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode, create_react_agent
from langgraph.prebuilt.chat_agent_executor import AgentState
from langmem.short_term import SummarizationNode
import structlog

from ...prompts.po import SYSTEM_PROMPT
from .tools import get_all_tools

logger = structlog.get_logger(__name__)


class POState(AgentState):
    """PO agent state with context for running summary persistence."""

    context: dict[str, Any]


def _create_summarization_hook(
    llm: ChatOpenAI,
    summarization_model: str | None,
    base_url: str,
    api_key: str,
    max_tokens: int,
    trigger_tokens: int,
    max_summary_tokens: int,
) -> SummarizationNode:
    """Create SummarizationNode for pre_model_hook.

    Uses a separate cheap model for summarization if configured,
    otherwise falls back to the main LLM.
    """
    if summarization_model:
        summary_llm = ChatOpenAI(
            model=summarization_model,
            base_url=base_url,
            api_key=api_key,
        ).bind(max_tokens=max_summary_tokens)
    else:
        summary_llm = llm.bind(max_tokens=max_summary_tokens)

    return SummarizationNode(
        model=summary_llm,
        max_tokens=max_tokens,
        max_tokens_before_summary=trigger_tokens,
        max_summary_tokens=max_summary_tokens,
        output_messages_key="llm_input_messages",
    )


async def _create_postgres_checkpointer(checkpoint_database_url: str) -> BaseCheckpointSaver:
    """Create AsyncPostgresSaver, ensuring the langgraph schema exists."""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    import psycopg
    from psycopg_pool import AsyncConnectionPool

    # psycopg3 sync connection to create schema (DDL, one-time)
    with psycopg.connect(checkpoint_database_url) as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS langgraph")
        conn.commit()

    # Use explicit pool for long-lived consumer (from_conn_string returns context manager).
    # autocommit=True required for setup() which runs CREATE INDEX CONCURRENTLY.
    pool = AsyncConnectionPool(
        conninfo=checkpoint_database_url,
        kwargs={"autocommit": True, "prepare_threshold": 0},
    )
    await pool.open()
    checkpointer = AsyncPostgresSaver(conn=pool)
    await checkpointer.setup()
    logger.info("po_checkpointer_postgres")
    return checkpointer


async def create_po_graph(
    model: str,
    base_url: str,
    api_key: str,
    checkpoint_database_url: str | None = None,
    summarization_model: str | None = None,
    summarization_max_tokens: int = 50_000,
    summarization_trigger_tokens: int = 60_000,
    summarization_max_summary_tokens: int = 2_000,
) -> CompiledStateGraph:
    """Create and compile the PO ReactAgent graph.

    Args:
        model: LLM model name (e.g. "anthropic/claude-sonnet-4-5").
        base_url: LLM API base URL (e.g. "https://openrouter.ai/api/v1").
        api_key: LLM API key.
        checkpoint_database_url: PostgreSQL URL for persistent checkpointer.
            Falls back to MemorySaver if not provided.
        summarization_model: Separate model for summarization (e.g. "anthropic/claude-haiku-4-5").
            Falls back to main model if not provided.
        summarization_max_tokens: Token budget after summarization.
        summarization_trigger_tokens: Threshold to trigger summarization.
        summarization_max_summary_tokens: Max tokens for the summary itself.
    """
    llm = ChatOpenAI(
        model=model,
        base_url=base_url,
        api_key=api_key,
    )

    if checkpoint_database_url:
        checkpointer = await _create_postgres_checkpointer(checkpoint_database_url)
    else:
        logger.warning("po_checkpointer_memory", reason="CHECKPOINT_DATABASE_URL not set")
        checkpointer = MemorySaver()

    summarization_hook = _create_summarization_hook(
        llm=llm,
        summarization_model=summarization_model,
        base_url=base_url,
        api_key=api_key,
        max_tokens=summarization_max_tokens,
        trigger_tokens=summarization_trigger_tokens,
        max_summary_tokens=summarization_max_summary_tokens,
    )

    tool_node = ToolNode(get_all_tools(), handle_tool_errors=True)

    return create_react_agent(
        model=llm,
        tools=tool_node,
        prompt=SYSTEM_PROMPT,
        pre_model_hook=summarization_hook,
        state_schema=POState,
        checkpointer=checkpointer,
    )
