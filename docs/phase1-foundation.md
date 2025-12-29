# Phase 1: Foundation — Detailed Implementation Plan

> Part of [Dynamic ProductOwner Design](./dynamic-po-design.md)

## Overview

Phase 1 creates the foundation for dynamic PO: thread management, capability registry, and base tools. After this phase, PO can:
- Track conversations with thread_id
- Load tools dynamically based on capabilities
- Send intermediate messages to user
- Wait for user responses (checkpoint-based)
- Finish tasks on user confirmation

---

## Architecture Decisions

### 1. Agentic Loop: Graph Passes (not while loop)

**Decision**: Use multiple passes through graph nodes instead of while loop inside node.

```
intent_parser → product_owner → po_tools → product_owner → po_tools → ... → END
                     ↑_______________|           ↑_______________|
```

**Rationale**:
- Automatic checkpointing after each node
- Each step visible in LangSmith
- `request_capabilities` rebuilds tools naturally on next node entry
- `awaiting_user_response` maps to END with checkpoint
- Already partially implemented in current codebase

**Iteration limit**: Counter in state, checked in router (default: 20)

### 2. Intent Parser: First Node in Graph

**Decision**: Intent parser is a node, not pre-graph logic in worker.py.

```
START → intent_parser → product_owner → ...
```

**Rationale**:
- All flow in one place — easier to debug
- Full trace in LangSmith
- Parser can read checkpoint to detect continuation vs new task
- Conditional edge can skip parser on continuation

### 3. User Response Waiting: Checkpoint-based

**Decision**: When `respond_to_user(awaiting_response=True)` is called:
1. Set `awaiting_user_response = True` in state
2. Router sees flag → returns END
3. Checkpoint saves state
4. Next user message → graph resumes from checkpoint
5. Router clears flag, continues to PO

**No blocking, no long-running processes.**

### 4. Intermediate Messages: Tool-based

**Decision**: PO uses `respond_to_user` tool to send messages anytime. Tool publishes directly to `OUTGOING_STREAM`.

---

## Components

### 1.1 Thread ID Management

**Location**: `services/langgraph/src/thread_manager.py` (new file)

**Redis key**: `thread:sequence:{user_id}` → integer

**Functions**:

```python
async def generate_thread_id(user_id: int) -> str:
    """
    Increment sequence and return new thread_id.
    Called by intent_parser for new tasks.

    Returns: "user_{user_id}_{sequence}"
    Example: "user_625038902_43"
    """
    redis = await get_redis_client()
    sequence = await redis.incr(f"thread:sequence:{user_id}")
    return f"user_{user_id}_{sequence}"


async def get_current_thread_id(user_id: int) -> str | None:
    """
    Get current thread_id without incrementing.
    Returns None if user has no threads yet.
    """
    redis = await get_redis_client()
    sequence = await redis.get(f"thread:sequence:{user_id}")
    if sequence is None:
        return None
    return f"user_{user_id}_{int(sequence)}"


async def get_or_create_thread_id(user_id: int) -> str:
    """
    Get current thread_id or create first one.
    Used when continuing existing session.
    """
    current = await get_current_thread_id(user_id)
    if current is None:
        return await generate_thread_id(user_id)
    return current
```

**Integration with worker.py**:
```python
# Before: thread_id = f"user_{telegram_user_id}"
# After:
from .thread_manager import get_or_create_thread_id

thread_id = await get_or_create_thread_id(telegram_user_id)
```

---

### 1.2 Capability Registry

**Location**: `services/langgraph/src/capabilities/` (new directory)

**Structure**:
```
capabilities/
├── __init__.py      # Registry + get_tools_for_capabilities()
├── base.py          # Base tools (always loaded)
├── deploy.py        # Deploy capability tools
├── infrastructure.py
├── project_management.py
├── diagnose.py
└── admin.py
```

**Registry definition** (`__init__.py`):

```python
from ..tools import (
    # Existing tools
    list_managed_servers,
    find_suitable_server,
    allocate_port,
    list_projects,
    get_project_status,
    # ... etc
)
from .base import (
    respond_to_user,
    search_knowledge,
    request_capabilities,
    finish_task,
)

CAPABILITY_REGISTRY: dict[str, dict] = {
    "deploy": {
        "description": "Deploy projects to servers",
        "tools": [
            "check_deploy_readiness",
            "trigger_deploy",
            "get_deploy_status",
            "get_deploy_logs",
            "cancel_deploy",
        ],
    },
    "infrastructure": {
        "description": "Manage servers and resource allocation",
        "tools": [
            "list_managed_servers",    # Exists
            "find_suitable_server",    # Exists
            "allocate_port",           # Exists
            "get_next_available_port", # Exists
            "list_allocations",        # New
            "release_port",            # New
        ],
    },
    "project_management": {
        "description": "Create and manage projects",
        "tools": [
            "list_projects",           # Exists
            "get_project_status",      # Exists
            "create_project_intent",   # Exists
            "update_project",          # New or exists?
        ],
    },
    "engineering": {
        "description": "Trigger code implementation pipeline",
        "tools": [
            "trigger_engineering",     # New
            "get_engineering_status",  # New
            "view_latest_pr",          # New
        ],
    },
    "diagnose": {
        "description": "Debug issues, view logs, check health",
        "tools": [
            "get_service_logs",        # New
            "get_node_logs",           # New
            "check_service_health",    # New
            "get_error_history",       # New
            "retry_failed_node",       # New
        ],
    },
    "admin": {
        "description": "System administration and manual control",
        "tools": [
            "list_graph_nodes",        # New
            "get_node_state",          # New
            "trigger_node_manually",   # New
            "clear_project_state",     # New
        ],
    },
}

# Tool name → Tool function mapping
TOOLS_MAP: dict[str, BaseTool] = {
    "list_managed_servers": list_managed_servers,
    "find_suitable_server": find_suitable_server,
    # ... all tools
}

BASE_TOOLS = [
    respond_to_user,
    search_knowledge,
    request_capabilities,
    finish_task,
]


def get_tools_for_capabilities(capabilities: list[str]) -> list[BaseTool]:
    """
    Build tool list from capability names.
    Always includes BASE_TOOLS.
    """
    tools = list(BASE_TOOLS)
    seen_names = {t.name for t in tools}

    for cap_name in capabilities:
        cap = CAPABILITY_REGISTRY.get(cap_name)
        if not cap:
            continue
        for tool_name in cap["tools"]:
            if tool_name in seen_names:
                continue
            tool = TOOLS_MAP.get(tool_name)
            if tool:
                tools.append(tool)
                seen_names.add(tool_name)

    return tools


def list_available_capabilities() -> dict[str, str]:
    """
    Return capability descriptions for intent parser prompt.
    """
    return {
        name: cap["description"]
        for name, cap in CAPABILITY_REGISTRY.items()
    }
```

---

### 1.3 Base Tools

**Location**: `services/langgraph/src/capabilities/base.py`

#### respond_to_user

```python
from langchain_core.tools import tool
from shared.redis_client import RedisStreamClient


@tool
async def respond_to_user(
    message: str,
    awaiting_response: bool = False,
) -> dict:
    """
    Send a message to the user via Telegram.

    Args:
        message: Text to send (supports markdown)
        awaiting_response: If True, pause and wait for user reply.
                          Use when you need user input to continue.

    Returns:
        {"sent": True, "awaiting": awaiting_response}

    Examples:
        Progress update (continue working):
            respond_to_user("Checking deployment readiness...")

        Ask question (wait for answer):
            respond_to_user("Which server? Found: vps-1, vps-2", awaiting_response=True)

        Final result (user will confirm separately):
            respond_to_user("Done! App deployed to http://1.2.3.4:8080")
    """
    # Get context from state (injected by LangGraph)
    # This requires InjectedState pattern or accessing via config
    state = get_current_state()  # Implementation detail

    redis = RedisStreamClient()
    await redis.connect()

    await redis.publish(
        RedisStreamClient.OUTGOING_STREAM,
        {
            "user_id": state["telegram_user_id"],
            "chat_id": state["chat_id"],
            "text": message,
            "correlation_id": state.get("correlation_id"),
        },
    )

    return {"sent": True, "awaiting": awaiting_response}
```

**State flag handling** (in po_tools node):
```python
if tool_name == "respond_to_user" and result.get("awaiting"):
    return {"awaiting_user_response": True, ...}
```

#### search_knowledge (stub)

```python
@tool
async def search_knowledge(
    query: str,
    scope: str = "all",
) -> dict:
    """
    Search project documentation, code, conversation history, or logs.

    Args:
        query: Natural language search query
        scope: Where to search
            - "docs": Project documentation
            - "code": Source code
            - "history": Previous conversations
            - "logs": Service logs
            - "all": Search everywhere

    Returns:
        {"results": [{"source": "...", "content": "...", "relevance": 0.95}, ...]}

    Note: RAG integration coming in Phase 6. Currently returns empty results.
    """
    # TODO: Phase 6 — connect to actual RAG
    return {
        "results": [],
        "note": "RAG not implemented yet. Use other tools to gather information.",
    }
```

#### request_capabilities

```python
@tool
def request_capabilities(
    capabilities: list[str],
    reason: str,
) -> dict:
    """
    Request additional tools for the current task.
    New tools will be available on your next action.

    Available capabilities:
        - "deploy": Deploy projects (check, trigger, logs)
        - "infrastructure": Manage servers and ports
        - "project_management": Create/update projects
        - "engineering": Trigger code implementation
        - "diagnose": View logs, debug issues
        - "admin": Manual system control

    Args:
        capabilities: List of capability groups to enable
        reason: Brief explanation why these are needed

    Returns:
        {"enabled": [...], "new_tools": [...]}
    """
    # Validation
    valid_caps = set(CAPABILITY_REGISTRY.keys())
    requested = set(capabilities)
    invalid = requested - valid_caps

    if invalid:
        return {"error": f"Unknown capabilities: {invalid}"}

    # Get current capabilities from state
    state = get_current_state()
    current = set(state.get("active_capabilities", []))

    # Merge
    new_caps = requested - current
    all_caps = current | requested

    # Get new tool names
    new_tools = []
    for cap in new_caps:
        new_tools.extend(CAPABILITY_REGISTRY[cap]["tools"])

    return {
        "enabled": list(all_caps),
        "new_tools": new_tools,
        "reason_logged": reason,
    }
```

**State update** (in po_tools node):
```python
if tool_name == "request_capabilities":
    return {"active_capabilities": result.get("enabled", []), ...}
```

#### finish_task

```python
@tool
def finish_task(summary: str) -> dict:
    """
    Mark the current task as complete and end the session.

    IMPORTANT: Only call this AFTER the user has confirmed the task is done.
    Look for phrases like "thanks", "got it", "perfect", "done", etc.

    After this, the next user message starts a fresh session with new thread_id.

    Args:
        summary: Brief summary of what was accomplished

    Returns:
        {"finished": True, "thread_id": "...", "summary": "..."}
    """
    state = get_current_state()
    thread_id = state["thread_id"]

    # Log completion
    logger.info(
        "task_finished",
        thread_id=thread_id,
        summary=summary,
        user_id=state["telegram_user_id"],
    )

    return {
        "finished": True,
        "thread_id": thread_id,
        "summary": summary,
    }
```

**State update** (in po_tools node):
```python
if tool_name == "finish_task":
    return {"user_confirmed_complete": True, ...}
```

---

### 1.4 POSessionState

**Location**: `services/langgraph/src/schemas/po_state.py` (new file)

```python
from typing import Annotated, TypedDict
from langgraph.graph import add_messages


def _last_value(a, b):
    """Reducer: keep last non-None value."""
    return b if b is not None else a


def _merge_lists(a: list, b: list) -> list:
    """Reducer: extend list."""
    return (a or []) + (b or [])


class POSessionState(TypedDict):
    # === Identity ===
    thread_id: str                              # For checkpoints, RAG, logging
    telegram_user_id: int                       # Telegram user ID
    user_id: int | None                         # Internal DB user.id
    chat_id: int                                # Telegram chat ID
    correlation_id: str | None                  # For tracing

    # === Conversation ===
    messages: Annotated[list, add_messages]     # LangChain message history

    # === Task Context ===
    task_summary: str | None                    # From intent parser
    current_project: str | None                 # Active project ID

    # === Dynamic Capabilities ===
    active_capabilities: Annotated[list[str], _merge_lists]  # ["deploy", "infrastructure"]

    # === Control Flow ===
    awaiting_user_response: bool                # Waiting for user input?
    user_confirmed_complete: bool               # User said done?
    po_iterations: int                          # Loop counter (max 20)

    # === Routing ===
    is_continuation: bool                       # Continuing previous session?
    skip_intent_parser: bool                    # Skip parser on continuation

    # === Errors ===
    errors: Annotated[list[str], _merge_lists]
```

**Relationship to OrchestratorState**:
- POSessionState is used within PO agentic loop
- When PO needs to trigger other nodes (Engineering, DevOps), convert to OrchestratorState
- Or: extend OrchestratorState with PO-specific fields

---

### 1.5 Graph Integration

**Router functions**:

```python
def route_after_intent_parser(state: POSessionState) -> str:
    """Intent parser → PO."""
    return "product_owner"


def route_after_product_owner(state: POSessionState) -> str:
    """PO → tools / END."""
    # Finished?
    if state.get("user_confirmed_complete"):
        return END

    # Waiting for user?
    if state.get("awaiting_user_response"):
        return END  # Checkpoint saved, resume on next message

    # Max iterations?
    if state.get("po_iterations", 0) >= 20:
        return END

    # Has tool calls?
    messages = state.get("messages", [])
    if messages:
        last = messages[-1]
        if hasattr(last, "tool_calls") and last.tool_calls:
            return "po_tools"

    return END


def route_after_po_tools(state: POSessionState) -> str:
    """Tools → PO (loop) / END."""
    # Finished?
    if state.get("user_confirmed_complete"):
        return END

    # Waiting for user?
    if state.get("awaiting_user_response"):
        return END

    # Continue loop
    return "product_owner"


def route_from_start(state: POSessionState) -> str:
    """START → intent_parser / product_owner."""
    # If continuing session with checkpoint, skip parser
    if state.get("skip_intent_parser"):
        return "product_owner"
    return "intent_parser"
```

**Graph definition**:

```python
from langgraph.graph import StateGraph, START, END

graph = StateGraph(POSessionState)

# Nodes
graph.add_node("intent_parser", intent_parser_node)
graph.add_node("product_owner", product_owner_node)
graph.add_node("po_tools", po_tools_node)

# Edges
graph.add_conditional_edges(START, route_from_start)
graph.add_conditional_edges("intent_parser", route_after_intent_parser)
graph.add_conditional_edges("product_owner", route_after_product_owner)
graph.add_conditional_edges("po_tools", route_after_po_tools)

# Compile with checkpointer
from langgraph.checkpoint.memory import MemorySaver

checkpointer = MemorySaver()  # TODO: PostgresSaver in production
app = graph.compile(checkpointer=checkpointer)
```

---

### 1.6 Worker.py Changes

```python
async def process_message(redis_client: RedisStreamClient, data: dict) -> None:
    telegram_user_id = data.get("user_id")
    chat_id = data.get("chat_id")
    text = data.get("text", "")
    correlation_id = data.get("correlation_id")

    # Get or create thread_id
    thread_id = await get_or_create_thread_id(telegram_user_id)

    # Check for existing checkpoint (continuation)
    config = {"configurable": {"thread_id": thread_id}}
    checkpoint = await checkpointer.aget(config)
    is_continuation = checkpoint is not None

    # Build initial state
    state: POSessionState = {
        "messages": [HumanMessage(content=text)],
        "telegram_user_id": telegram_user_id,
        "user_id": await resolve_internal_user_id(telegram_user_id),
        "chat_id": chat_id,
        "thread_id": thread_id,
        "correlation_id": correlation_id,
        "is_continuation": is_continuation,
        "skip_intent_parser": is_continuation,  # Skip parser on continuation
        "awaiting_user_response": False,        # Reset on new message
        "po_iterations": 0,
    }

    # Run graph
    result = await app.ainvoke(state, config)

    # If user_confirmed_complete, clear checkpoint for new thread next time
    if result.get("user_confirmed_complete"):
        await checkpointer.adelete(config)
        # Next message will get new thread_id
```

---

## Checklist

### 1.1 Thread ID Management
- [ ] Create `services/langgraph/src/thread_manager.py`
- [ ] Implement `generate_thread_id()`
- [ ] Implement `get_current_thread_id()`
- [ ] Implement `get_or_create_thread_id()`
- [ ] Update `worker.py` to use new thread management

### 1.2 Capability Registry
- [ ] Create `services/langgraph/src/capabilities/` directory
- [ ] Create `capabilities/__init__.py` with registry
- [ ] Implement `get_tools_for_capabilities()`
- [ ] Implement `list_available_capabilities()`
- [ ] Map existing tools to capabilities

### 1.3 Base Tools
- [ ] Create `capabilities/base.py`
- [ ] Implement `respond_to_user` tool
- [ ] Implement `search_knowledge` stub
- [ ] Implement `request_capabilities` tool
- [ ] Implement `finish_task` tool

### 1.4 State & Graph
- [ ] Create `schemas/po_state.py` with POSessionState
- [ ] Create router functions
- [ ] Update graph with new nodes and edges
- [ ] Add MemorySaver checkpointer
- [ ] Update worker.py for checkpoint handling

### 1.5 Testing
- [ ] Unit test: thread_id generation
- [ ] Unit test: capability tool building
- [ ] Unit test: base tools
- [ ] Integration test: simple question flow
- [ ] Integration test: continuation flow (awaiting_response)

---

## Open Questions Resolved

| Question | Decision |
|----------|----------|
| Max iterations for PO loop | 20 |
| Session timeout | 30 minutes (handled by checkpoint TTL) |
| Capability dependencies | No auto-include; PO can request more via tool |
| Where to store thread logic | `services/langgraph/src/thread_manager.py` |
| Agentic loop implementation | Graph passes (not while loop) |
| Intent parser location | First node in graph |

---

## Next Phase

After Phase 1, proceed to:
- [Phase 4: Capability Tools](./phase4-capabilities.md) — Deploy, Infrastructure, Diagnose, Admin
- [Phase 5-6: Integration & RAG](./phase5-6-integration-rag.md) — Session Manager, Testing, RAG
