# Dynamic ProductOwner with Intent-Based Tool Loading

## Problem Statement

### Current Issues

1. **Token Waste**: PO has 20+ tools, but 95% of requests need only 2-3. We burn tokens on tool descriptions for every request.

2. **Lack of Autonomy**: PO cannot trigger arbitrary graph nodes. If user says "deploy hello-world-bot", PO can't check readiness, allocate resources, and trigger DevOps on its own.

3. **No Feedback Loop**: PO can't retry failed nodes, view logs, or handle errors autonomously.

### Goals

- PO uses minimal tools for simple requests, full toolset only when needed
- PO can orchestrate complex multi-step tasks autonomously
- Cheap intent parsing (~$0.001/request) gates expensive PO calls
- User controls task completion, not PO

---

## Architecture Overview

```
User Message
     |
     v
+----------------------------------+
|  Intent Parser (gpt-4o-mini)     |
|  - Classifies intent             |
|  - Selects initial capabilities  |
|  - Generates/retrieves thread_id |
|  Cost: ~$0.001/request           |
+----------------------------------+
     |
     v
+----------------------------------+
|  ProductOwner Agentic Loop       |
|                                  |
|  Base Tools (always):            |
|    - respond_to_user             |
|    - search_knowledge (RAG)      |
|    - request_capabilities        |
|                                  |
|  Dynamic Tools (on demand):      |
|    - deploy_*, diagnose_*, etc.  |
|                                  |
|  Loops until USER confirms done  |
+----------------------------------+
     |
     v
+----------------------------------+
|  Task Completion                 |
|  - User confirms task is done    |
|  - PO calls finish_task tool     |
|  - Session ends, new thread_id   |
+----------------------------------+
```

---

## Components

### 1. Intent Parser

Lightweight LLM call (gpt-4o-mini or deepseek) that:
- Analyzes user message + minimal state context
- Returns initial capabilities needed
- Manages thread_id for RAG and session continuity

**Input:**
```python
{
    "message": "Задеплой hello-world-bot",
    "user_id": 625038902,
    "has_current_project": true,
    "current_project_name": "hello-world-bot",
    "has_allocated_resources": false,
    "last_thread_id": "user_625038902_42",  # Previous thread
}
```

**Output:**
```python
{
    "capabilities": ["deploy", "infrastructure"],
    "thread_id": "user_625038902_43",  # Incremented
    "task_summary": "Deploy hello-world-bot project",
    "reasoning": "User wants to deploy, need deploy tools and infrastructure for resource allocation"
}
```

**Thread ID Generation:**
- Format: `user_{user_id}_{sequence}`
- Sequence increments per user
- Stored in Redis: `thread:sequence:{user_id}` → current sequence number
- New task = new thread_id (increment)
- Continuation = same thread_id

### 2. ProductOwner Node

Agentic loop with dynamic tool loading.

**State:**
```python
class POSessionState(TypedDict):
    # Identity
    thread_id: str                    # For RAG, checkpoints, logging
    user_id: int                      # Telegram user ID

    # Context
    current_project: str | None       # Active project ID
    task_summary: str | None          # What we're doing (from parser)

    # Dynamic capabilities
    active_capabilities: list[str]    # ["deploy", "infrastructure"]

    # Conversation
    messages: list                    # Conversation history

    # Control flow
    awaiting_user_response: bool      # Waiting for user input?
    user_confirmed_complete: bool     # User said "done"?
```

**Loop Logic:**
```
while not user_confirmed_complete:
    1. Build tools list (base + dynamic based on capabilities)
    2. Invoke LLM with tools
    3. Execute tool calls
    4. If respond_to_user called with awaiting_response=True:
       - Send message to user
       - Wait for user reply
       - Add reply to messages
    5. If user says "done", "ok", "thanks" etc:
       - Set user_confirmed_complete = True
    6. Continue loop
```

### 3. Capability Registry

Groups of related tools that can be loaded together.

```python
CAPABILITY_REGISTRY = {
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
            "list_servers",
            "find_suitable_server",
            "allocate_port",
            "list_allocations",
            "release_port",
        ],
    },
    "project_management": {
        "description": "Create and manage projects",
        "tools": [
            "list_projects",
            "get_project_status",
            "create_project_intent",
            "update_project",
        ],
    },
    "engineering": {
        "description": "Trigger code implementation pipeline",
        "tools": [
            "trigger_engineering",
            "get_engineering_status",
            "view_latest_pr",
        ],
    },
    "diagnose": {
        "description": "Debug issues, view logs, check health",
        "tools": [
            "get_service_logs",
            "get_node_logs",
            "check_service_health",
            "get_error_history",
            "retry_failed_node",
        ],
    },
    "admin": {
        "description": "System administration and manual control",
        "tools": [
            "list_graph_nodes",
            "get_node_state",
            "trigger_node_manually",
            "clear_project_state",
        ],
    },
}
```

---

## Base Tools (Always Available)

### 1. respond_to_user

```python
@tool
def respond_to_user(
    message: str,
    awaiting_response: bool = False,
) -> dict:
    """
    Send a message to the user.

    Args:
        message: Text to send to user (supports markdown)
        awaiting_response: If True, wait for user reply before continuing.
                          Use for questions or when user input is needed.

    Returns:
        {"sent": True, "awaiting": awaiting_response}

    Examples:
        Progress update (no wait):
            respond_to_user("Checking deployment readiness...")

        Ask question (wait):
            respond_to_user("Which project? I found: proj-a, proj-b", awaiting_response=True)

        Final result (no wait, user will confirm separately):
            respond_to_user("Done! Deployed to http://example.com:8080")
    """
```

### 2. search_knowledge (RAG)

```python
@tool
def search_knowledge(
    query: str,
    scope: str = "all",
) -> dict:
    """
    Search project documentation, code, conversation history, or logs.
    Uses thread_id for context-aware search.

    Args:
        query: Natural language search query
        scope: Where to search
            - "docs": Project documentation and specs
            - "code": Source code in repositories
            - "history": Previous conversations in this thread
            - "logs": Service and deployment logs
            - "all": Search everywhere

    Returns:
        {"results": [{"source": "...", "content": "...", "relevance": 0.95}, ...]}
    """
```

### 3. request_capabilities

```python
@tool
def request_capabilities(
    capabilities: list[str],
    reason: str,
) -> dict:
    """
    Request additional tools for the current task.
    New tools will be available immediately after this call.

    Available capabilities:
        - "deploy": Deploy projects (check, trigger, logs)
        - "infrastructure": Manage servers and ports
        - "project_management": Create/update projects
        - "engineering": Trigger code implementation
        - "diagnose": View logs, debug issues
        - "admin": Manual system control

    Args:
        capabilities: List of capability groups to enable
        reason: Why these capabilities are needed

    Returns:
        {"enabled": ["deploy", "infrastructure"], "new_tools": ["trigger_deploy", ...]}
    """
```

### 4. finish_task

```python
@tool
def finish_task(
    summary: str,
) -> dict:
    """
    Mark the current task as complete.
    ONLY call this AFTER user has confirmed the task is done.

    This ends the current session. Next user message starts fresh
    with a new thread_id.

    Args:
        summary: Brief summary of what was accomplished

    Returns:
        {"finished": True, "thread_id": "...", "summary": "..."}
    """
```

---

## Flow Examples

### Example 1: Simple Question

```
User: "Привет, какие проекты у меня есть?"

Intent Parser:
  capabilities: ["project_management"]
  thread_id: "user_625038902_43"

PO receives tools:
  - respond_to_user, search_knowledge, request_capabilities, finish_task
  - list_projects, get_project_status (from project_management)

PO:
  1. list_projects() → [{"name": "hello-world-bot", "status": "created"}]
  2. respond_to_user("У тебя один проект: hello-world-bot (создан, не задеплоен)")

User: "Спасибо"

PO:
  1. finish_task("Listed user's projects")

Session ends.
```

### Example 2: Complex Deploy Task

```
User: "Задеплой hello-world-bot"

Intent Parser:
  capabilities: ["deploy", "infrastructure"]
  thread_id: "user_625038902_44"

PO receives tools:
  - Base: respond_to_user, search_knowledge, request_capabilities, finish_task
  - Deploy: check_deploy_readiness, trigger_deploy, get_deploy_status, get_deploy_logs
  - Infrastructure: find_suitable_server, allocate_port, list_allocations

PO:
  1. check_deploy_readiness("hello-world-bot")
     → {"ready": false, "missing": ["allocated_resources"]}

  2. respond_to_user("Сначала выделю ресурсы для деплоя...")

  3. find_suitable_server(ram_mb=512)
     → {"handle": "vps-267179", "ip": "1.2.3.4"}

  4. allocate_port("vps-267179", "hello-world-bot")
     → {"port": 8080}

  5. trigger_deploy("hello-world-bot")
     → {"status": "started", "job_id": "deploy_123"}

  6. respond_to_user("Деплой запущен, ожидаю завершения...")

  7. [polls get_deploy_status until done]

  8. respond_to_user("Готово! Приложение доступно: http://1.2.3.4:8080")

User: "Круто, спасибо!"

PO:
  1. finish_task("Deployed hello-world-bot to http://1.2.3.4:8080")

Session ends.
```

### Example 3: Error Handling

```
User: "Почему hello-world-bot не работает?"

Intent Parser:
  capabilities: ["diagnose", "deploy"]
  thread_id: "user_625038902_45"

PO:
  1. get_service_logs("hello-world-bot", lines=50)
     → {"logs": "Error: Connection refused to database..."}

  2. respond_to_user("Вижу ошибку подключения к базе данных. Проверю health...")

  3. check_service_health("hello-world-bot")
     → {"healthy": false, "checks": {"db": "failed", "api": "ok"}}

  4. request_capabilities(["admin"], "need to check database service")
     → PO gets retry_failed_node, etc.

  5. respond_to_user("База данных недоступна. Могу перезапустить сервис. Перезапустить?",
                     awaiting_response=True)

User: "Да"

PO:
  1. retry_failed_node("database")
     → {"restarted": true}

  2. check_service_health("hello-world-bot")
     → {"healthy": true}

  3. respond_to_user("Готово! Сервис восстановлен и работает.")

User: "Отлично"

PO:
  1. finish_task("Diagnosed and fixed database connection issue")
```

---

## Implementation Plan

> Detailed phase documents:
> - [Phase 1: Foundation](./phase1-foundation.md) — Thread ID, Capability Registry, Base Tools
> - [Phase 4: Capability Tools](./phase4-capabilities.md) — Deploy, Infrastructure, Diagnose, Admin (LangGraph + Redis Queue)
> - [Phase 5-6: Integration & RAG](./phase5-6-integration-rag.md) — Session Manager, Telegram Integration, Testing, RAG Search

### Phase 1: Foundation

**1.1. Thread ID Management**
- [x] Add `thread:sequence:{user_id}` key to Redis
- [x] Helper function `generate_thread_id(user_id) -> str`
- [x] Helper function `get_current_thread_id(user_id) -> str`
- [x] Store thread_id in POSessionState

**1.2. Capability Registry**
- [x] Create `services/langgraph/src/capabilities/__init__.py`
- [x] Define CAPABILITY_REGISTRY with tool references
- [x] Function `get_tools_for_capabilities(capabilities: list[str]) -> list[Tool]`
- [x] Function `list_available_capabilities() -> dict`

**1.3. Base Tools**
- [x] Implement `respond_to_user` tool
- [x] Implement `search_knowledge` tool (stub, connect to RAG later)
- [x] Implement `request_capabilities` tool
- [x] Implement `finish_task` tool

### Phase 2: Intent Parser

**2.1. Parser Implementation**
- [x] Create `services/langgraph/src/nodes/intent_parser.py`
- [x] Define parser prompt with capability descriptions
- [x] Use gpt-4o-mini via OpenRouter
- [x] Structured output: `{capabilities, thread_id, task_summary}`

**2.2. Parser Integration**
- [x] Add intent_parser node to graph
- [x] Route: START → intent_parser → product_owner
- [x] Pass parsed capabilities and thread_id to PO state

### Phase 3: Dynamic ProductOwner

**3.1. Refactor PO Node**
- [x] Accept capabilities from state
- [x] Build tool list dynamically: base + capability tools
- [x] Implement agentic loop with tool execution
- [x] Handle `awaiting_response` flow (pause, wait for user)

**3.2. User Response Handling**
- [x] Detect user confirmation phrases ("спасибо", "готово", "ок") — via LLM judgment
- [x] Allow PO to call finish_task only after user confirms
- [x] Clear session state on finish — routing to END

**3.3. Tool Result Handling**
- [x] Pass tool results back to PO for reasoning
- [x] Support intermediate respond_to_user calls
- [x] Handle tool errors gracefully

### Phase 4: Capability Tools

**4.1. Deploy Capability**
- [x] `check_deploy_readiness(project_id)` - checks repo, resources, CI status
- [x] `trigger_deploy(project_id)` - starts DevOps node
- [x] `get_deploy_status(job_id)` - polls deployment progress
- [x] `get_deploy_logs(project_id, lines)` - fetch deployment logs

**4.2. Infrastructure Capability**
- [x] Reuse existing: `find_suitable_server`, `allocate_port`
- [x] Add `list_allocations(project_id)` - show allocated resources
- [x] Add `release_port(allocation_id)` - free up resources

**4.3. Engineering Capability**
- [x] `trigger_engineering(project_id, task)` - starts Developer/Tester flow
- [x] `get_engineering_status(job_id)` - polls pipeline progress
- [x] `view_latest_pr(project_id)` - fetch PR status and URL

**4.4. Diagnose Capability**
- [x] `get_service_logs(project_id, lines)` - container logs via SSH
- [x] `check_service_health(project_id)` - HTTP/Container check via SSH
- [x] `get_error_history(project_id)` - log analysis for patterns

**4.5. Admin Capability**
- [x] `list_graph_nodes()` - for manual triggering
- [x] `trigger_node_manually(node_name, inputs)` - force execution
- [x] `clear_project_state(project_id)` - reset active tasks

### Phase 5: Integration & Testing

**5.1. Graph Integration**
- [ ] Update graph.py with new flow
- [ ] Handle PO loop within single graph invocation
- [ ] Proper state propagation between parser and PO

**5.2. Telegram Integration**
- [ ] Support multi-message conversations within thread
- [ ] Handle `awaiting_response` - don't process other messages
- [ ] Timeout handling for abandoned conversations

**5.3. Testing**
- [ ] Unit tests for intent parser
- [ ] Unit tests for capability loading
- [ ] Unit tests for base tools
- [ ] Integration test: simple question flow
- [ ] Integration test: deploy flow
- [ ] Integration test: error handling flow

### Phase 6: RAG Integration

**6.1. Connect search_knowledge to RAG**
- [ ] Use thread_id for conversation context
- [ ] Implement scope filtering (docs, code, logs, history)
- [ ] Add embeddings for project documentation

---

## Open Questions

1. **Max iterations for PO loop?** - Prevent infinite loops, suggest 15-20 max

2. **Token budget per task?** - Alert if PO uses too many tokens, suggest $0.50 limit

3. **Concurrent tasks per user?** - One active task at a time? Queue others?

4. **Session timeout?** - How long to wait for user response? 30 min?

5. **Capability dependencies?** - Should "deploy" auto-include "infrastructure"?

---

## Migration Path

Current flow:
```
PO → Analyst → Zavhoz → Engineering → DevOps
```

New flow:
```
Intent Parser → PO (dynamic) → [any node via tools] → PO → ... → finish_task
```

**Backward compatibility:**
- Keep existing nodes (Zavhoz, Engineering, DevOps) as-is
- PO tools call these nodes internally
- Gradual migration: start with deploy capability, add others

---

## Success Metrics

1. **Token efficiency**: Average tokens per simple request < 2000 (vs current ~5000+)
2. **Autonomy**: PO can complete deploy task without graph routing changes
3. **User satisfaction**: Tasks complete in single conversation thread
4. **Error recovery**: PO can diagnose and retry failed operations
