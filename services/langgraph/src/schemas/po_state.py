"""POSessionState - state for Dynamic ProductOwner agentic loop.

This state is used within the PO conversation flow.
When PO needs to trigger other nodes (Engineering, DevOps),
state is propagated to OrchestratorState.
"""

from typing import Annotated

from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


def _last_value(left, right):
    """Reducer: keep last non-None value."""
    return right if right is not None else left


def _merge_lists(left: list | None, right: list | None) -> list:
    """Reducer: extend list without duplicates."""
    left = left or []
    right = right or []
    # For capabilities, merge as set to avoid duplicates
    if isinstance(left, list) and isinstance(right, list):
        # If it looks like capabilities list (strings), dedupe
        if all(isinstance(x, str) for x in left + right):
            return list(set(left) | set(right))
    return left + right


class POSessionState(TypedDict):
    """State for Dynamic ProductOwner agentic loop.

    Fields are grouped by purpose:
    - Identity: who is this conversation with
    - Conversation: message history
    - Task Context: what are we working on
    - Dynamic Capabilities: which tools are loaded
    - Control Flow: loop control flags
    - Errors: accumulated errors
    """

    # === Identity ===
    thread_id: str  # For checkpoints, RAG, logging
    telegram_user_id: int  # Telegram user ID
    user_id: int | None  # Internal DB user.id
    chat_id: int  # Telegram chat ID
    correlation_id: str | None  # For distributed tracing

    # === Conversation ===
    messages: Annotated[list, add_messages]  # LangChain message history

    # === Task Context ===
    task_summary: str | None  # Task summary
    current_project: str | None  # Active project ID

    # === Dynamic Capabilities ===
    active_capabilities: Annotated[list[str], _merge_lists]  # ["deploy", "infrastructure"]

    # === Control Flow ===
    awaiting_user_response: bool  # Waiting for user input?
    user_confirmed_complete: bool  # User said done?
    po_iterations: int  # Loop counter (max 20)

    # === Routing ===
    is_continuation: bool  # Continuing previous session?

    # === Errors ===
    errors: Annotated[list[str], _merge_lists]


# Maximum iterations before forcing END
MAX_PO_ITERATIONS = 20
