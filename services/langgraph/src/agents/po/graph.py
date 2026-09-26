"""PO ReactAgent graph.

Creates a LangGraph ReactAgent with PO tools, the PO's LLM channel chain,
PostgreSQL or MemorySaver checkpointer, and conversation summarization on the
PO summarizer's own channel chain.
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode, create_react_agent
from langgraph.prebuilt.chat_agent_executor import AgentState
from langmem.short_term import SummarizationNode
from pydantic import ValidationError
import structlog

from ...prompts.po import SYSTEM_PROMPT
from .tools import get_all_tools
from .tools_briefs import show_full_brief

logger = structlog.get_logger(__name__)


class POState(AgentState):
    """PO agent state with context for running summary persistence."""

    context: dict[str, Any]


def _not_run_beside_full_brief(name: str) -> str:
    return (
        f"Not run: {name} was called in the same turn as {show_full_brief.name}, which is "
        "called alone and ends the turn with the full brief. Call it again after the "
        "user answers, if it is still needed."
    )


_FULL_BRIEF_ARGUMENTS_REFUSED = (
    f"Not run: {show_full_brief.name} needs `brief_id`, the id present_product_brief "
    "returned. Call it again, alone, with that id."
)


def show_full_brief_alone(state: POState) -> dict:
    """Keep `show_full_brief` the only tool of the turn that shows the full brief.

    Its result is `return_direct`: the turn ends on the last tool message and
    the consumer sends that message to the user. A model that calls it beside
    another tool would otherwise end the turn on whichever result came last. So
    every other call of that turn is answered here with a short tool error and
    never runs, and the brief is the one call left for the tool node — its
    result, the full brief or the apology, is the turn's last message.

    Arguments that do not validate would make the tool node answer the brief
    call with raw validation text, and that would be the reply. They are
    refused here instead, with every other call of the turn, so nothing is left
    to run and the model gets another step.
    """
    last = state["messages"][-1]
    if not isinstance(last, AIMessage):
        return {}
    briefs = [call for call in last.tool_calls if call["name"] == show_full_brief.name]
    if not briefs:
        return {}
    arguments_refused = False
    for call in briefs:
        try:
            show_full_brief.tool_call_schema.model_validate(call["args"])
        except ValidationError:
            arguments_refused = True
    answered = [
        ToolMessage(
            content=(
                _FULL_BRIEF_ARGUMENTS_REFUSED
                if call["name"] == show_full_brief.name
                else _not_run_beside_full_brief(call["name"])
            ),
            name=call["name"],
            tool_call_id=call["id"],
            status="error",
        )
        for call in last.tool_calls
        if arguments_refused or call["name"] != show_full_brief.name
    ]
    if answered:
        logger.warning(
            "po_full_brief_not_alone",
            refused=[message.name for message in answered],
            arguments_refused=arguments_refused,
        )
    return {"messages": answered} if answered else {}


def _create_summarization_hook(
    summarization_llm: BaseChatModel,
    max_tokens: int,
    trigger_tokens: int,
    max_summary_tokens: int,
) -> SummarizationNode:
    """Create SummarizationNode for pre_model_hook on the summarizer's channel chain.

    The summary budget is bound as `max_tokens`; a channel that takes a token
    limit (openrouter) sends it, a CLI turn has no such parameter.
    """
    return SummarizationNode(
        model=summarization_llm.bind(max_tokens=max_summary_tokens),
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
    llm: BaseChatModel,
    summarization_llm: BaseChatModel,
    checkpoint_database_url: str | None = None,
    summarization_max_tokens: int = 50_000,
    summarization_trigger_tokens: int = 60_000,
    summarization_max_summary_tokens: int = 2_000,
) -> CompiledStateGraph:
    """Create and compile the PO ReactAgent graph.

    Args:
        llm: The PO's LLM channel chain (`src.llm.build_agent_llm`).
        summarization_llm: The PO summarizer's LLM channel chain.
        checkpoint_database_url: PostgreSQL URL for persistent checkpointer.
            Falls back to MemorySaver if not provided.
        summarization_max_tokens: Token budget after summarization.
        summarization_trigger_tokens: Threshold to trigger summarization.
        summarization_max_summary_tokens: Max tokens for the summary itself.
    """
    if checkpoint_database_url:
        checkpointer = await _create_postgres_checkpointer(checkpoint_database_url)
    else:
        logger.warning("po_checkpointer_memory", reason="CHECKPOINT_DATABASE_URL not set")
        checkpointer = MemorySaver()

    summarization_hook = _create_summarization_hook(
        summarization_llm=summarization_llm,
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
        post_model_hook=show_full_brief_alone,
        state_schema=POState,
        checkpointer=checkpointer,
    )
