"""PO ReactAgent graph.

Creates a LangGraph ReactAgent with PO tools, ChatOpenAI (OpenRouter-compatible),
MemorySaver checkpointer, and message trimming.
"""

from __future__ import annotations

from langchain_core.messages import AnyMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import create_react_agent

from .prompts import SYSTEM_PROMPT
from .tools import get_all_tools

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


def create_po_graph(
    model: str,
    base_url: str,
    api_key: str,
) -> CompiledStateGraph:
    """Create and compile the PO ReactAgent graph.

    Args:
        model: LLM model name (e.g. "anthropic/claude-sonnet-4-5").
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
        tools=get_all_tools(),
        prompt=prompt_with_trimming,
        checkpointer=checkpointer,
    )
