"""PO ReactAgent graph.

Creates a LangGraph ReactAgent with PO tools, ChatOpenAI (OpenRouter-compatible),
PostgreSQL or MemorySaver checkpointer, and message trimming.
"""

from __future__ import annotations

from langchain_core.messages import AnyMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import create_react_agent
import structlog

from .prompts import SYSTEM_PROMPT
from .tools import get_all_tools

logger = structlog.get_logger(__name__)

# Token budget for conversation history trimming
MAX_CONTEXT_TOKENS = 50_000
# Rough estimate: 1 token ≈ 4 chars
CHARS_PER_TOKEN = 4


def _estimate_tokens(msg: AnyMessage) -> int:
    """Rough token estimate for a message."""
    content = msg.content if isinstance(msg.content, str) else str(msg.content)
    return len(content) // CHARS_PER_TOKEN + 1


def prompt_with_trimming(state: dict) -> list[AnyMessage]:
    """Prepend system prompt and trim conversation history to fit token budget.

    Used as `prompt` callable for create_react_agent:
    receives full state, returns messages list for the LLM.
    """
    messages: list[AnyMessage] = state.get("messages", [])

    system_msg = SystemMessage(content=SYSTEM_PROMPT)
    non_system = [m for m in messages if m.type != "system"]

    kept: list[AnyMessage] = []
    tokens = _estimate_tokens(system_msg)

    for msg in reversed(non_system):
        msg_tokens = _estimate_tokens(msg)
        if tokens + msg_tokens > MAX_CONTEXT_TOKENS:
            break
        kept.insert(0, msg)
        tokens += msg_tokens

    return [system_msg, *kept]


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
) -> CompiledStateGraph:
    """Create and compile the PO ReactAgent graph.

    Args:
        model: LLM model name (e.g. "anthropic/claude-sonnet-4-5").
        base_url: LLM API base URL (e.g. "https://openrouter.ai/api/v1").
        api_key: LLM API key.
        checkpoint_database_url: PostgreSQL URL for persistent checkpointer.
            Falls back to MemorySaver if not provided.
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

    return create_react_agent(
        model=llm,
        tools=get_all_tools(),
        prompt=prompt_with_trimming,
        checkpointer=checkpointer,
    )
