# Phase 5-6: Integration, Telegram & RAG — Detailed Implementation Plan

> Part of [Dynamic ProductOwner Design](./dynamic-po-design.md)

## Overview

Phases 5 and 6 complete the Dynamic PO system by integrating all components, adding proper Telegram conversation handling, comprehensive testing, and connecting the RAG system.

**Current State Analysis:**
- `graph.py`: Intent Parser → PO → PO Tools loop is implemented with all routing
- `worker.py`: Thread management exists but lacks session blocking and cleanup
- Telegram: No multi-message handling, no timeout, no session awareness
- RAG: `search_knowledge` is a stub returning empty results

**After this phase, the system will:**
- Handle multi-turn conversations with proper session isolation
- Block concurrent messages during active processing
- Timeout abandoned sessions (configurable, default 30 min)
- Provide test coverage for all Dynamic PO flows
- Enable context-aware search via RAG (docs, code, history, logs)

---

## Architecture Decisions

### 1. Session Locking: Redis-based

**Decision**: Use Redis for session locking instead of in-memory sets.

```
┌─────────────────────────────────────────────────────────────┐
│                     Session Lifecycle                        │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  User Message → Check Lock → [Locked?]                       │
│                                  │                           │
│                        ┌─────────┴─────────┐                 │
│                        ↓                   ↓                 │
│                     No Lock            Has Lock              │
│                        │                   │                 │
│                        ↓                   ↓                 │
│                   Acquire Lock      Queue or Reject          │
│                        │               message               │
│                        ↓                                     │
│                   Process via                                │
│                   LangGraph                                  │
│                        │                                     │
│              ┌─────────┴─────────┐                           │
│              ↓                   ↓                           │
│       awaiting_user=True    finish_task                      │
│              │                   │                           │
│              ↓                   ↓                           │
│         Keep Lock           Release Lock                     │
│         (wait for           Clear Session                    │
│          response)          Increment thread_id              │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

**Redis Keys:**
```python
# Session lock (with TTL for timeout)
session:lock:{user_id} → {"thread_id": "...", "locked_at": "...", "state": "processing|awaiting"}

# Session state (for awaiting_user_response resumption)
session:state:{user_id} → serialized checkpoint reference
```

**Rationale:**
- Survives worker restart
- Enables distributed workers
- Built-in TTL for timeout
- Atomic operations (SETNX)

### 2. Message Queueing: Reject with Notification

**Decision**: When user sends message while session is locked (processing), reject with friendly notification.

```python
# If locked and NOT awaiting_user_response
"⏳ Подожди, я ещё обрабатываю предыдущий запрос..."

# If locked and awaiting_user_response
# Process as continuation (this is expected flow)
```

**Rationale:**
- Simple implementation
- Clear UX (user knows system is busy)
- No message queue complexity
- Prevents context confusion

### 3. Session Timeout: Redis TTL + Cleanup

**Decision**: 30-minute timeout via Redis TTL. Periodic cleanup job clears stale history.

```python
SESSION_TIMEOUT_SECONDS = 30 * 60  # 30 minutes

# On message receive:
await redis.expire(f"session:lock:{user_id}", SESSION_TIMEOUT_SECONDS)

# If lock expired, session is considered abandoned
# Next message starts fresh session
```

### 4. RAG Scope Implementation

**Decision**: Implement scopes incrementally, starting with most valuable.

| Scope | Priority | Source | Implementation |
|-------|----------|--------|----------------|
| `history` | P0 | Conversation summaries | Already implemented (RAG summaries API) |
| `docs` | P1 | Project specs, TASK.md | pgvector embeddings in API DB |
| `logs` | P2 | Service/deploy logs | Loki/file parsing on demand |
| `code` | P3 | Repository contents | GitHub API + embeddings |

---

## Components

### 5.1 Session Manager

**Location**: `services/langgraph/src/session_manager.py` (new file)

```python
"""Session lifecycle management for Dynamic PO.

Handles:
- Session locking (prevents concurrent processing)
- Awaiting user response state
- Session timeout
- Thread ID lifecycle
"""

import json
from datetime import UTC, datetime
from enum import Enum

import redis.asyncio as redis
import structlog

from .config.settings import get_settings
from .thread_manager import generate_thread_id, get_current_thread_id

logger = structlog.get_logger()

SESSION_TIMEOUT_SECONDS = 30 * 60  # 30 minutes


class SessionState(str, Enum):
    """Session states."""
    PROCESSING = "processing"  # Graph is running
    AWAITING = "awaiting"      # Waiting for user response
    IDLE = "idle"              # No active session


class SessionLock:
    """Session lock data."""
    def __init__(
        self,
        thread_id: str,
        state: SessionState,
        locked_at: datetime,
    ):
        self.thread_id = thread_id
        self.state = state
        self.locked_at = locked_at

    def to_dict(self) -> dict:
        return {
            "thread_id": self.thread_id,
            "state": self.state.value,
            "locked_at": self.locked_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SessionLock":
        return cls(
            thread_id=data["thread_id"],
            state=SessionState(data["state"]),
            locked_at=datetime.fromisoformat(data["locked_at"]),
        )


class SessionManager:
    """Manages user session lifecycle."""

    def __init__(self):
        self._redis: redis.Redis | None = None

    async def _get_redis(self) -> redis.Redis:
        if self._redis is None:
            settings = get_settings()
            self._redis = redis.from_url(settings.redis_url, decode_responses=True)
        return self._redis

    def _lock_key(self, user_id: int) -> str:
        return f"session:lock:{user_id}"

    async def get_session(self, user_id: int) -> SessionLock | None:
        """Get current session lock if exists."""
        r = await self._get_redis()
        data = await r.get(self._lock_key(user_id))
        if not data:
            return None
        return SessionLock.from_dict(json.loads(data))

    async def acquire_lock(
        self,
        user_id: int,
        thread_id: str,
        state: SessionState = SessionState.PROCESSING,
    ) -> bool:
        """Try to acquire session lock.

        Returns True if lock acquired, False if already locked.
        """
        r = await self._get_redis()
        key = self._lock_key(user_id)

        lock = SessionLock(
            thread_id=thread_id,
            state=state,
            locked_at=datetime.now(UTC),
        )

        # SETNX with TTL
        acquired = await r.set(
            key,
            json.dumps(lock.to_dict()),
            nx=True,  # Only set if not exists
            ex=SESSION_TIMEOUT_SECONDS,
        )

        if acquired:
            logger.debug("session_lock_acquired", user_id=user_id, thread_id=thread_id)

        return bool(acquired)

    async def update_state(
        self,
        user_id: int,
        state: SessionState,
    ) -> bool:
        """Update session state (e.g., PROCESSING -> AWAITING)."""
        r = await self._get_redis()
        key = self._lock_key(user_id)

        current = await self.get_session(user_id)
        if not current:
            return False

        current.state = state
        await r.set(
            key,
            json.dumps(current.to_dict()),
            ex=SESSION_TIMEOUT_SECONDS,  # Refresh TTL
        )

        logger.debug(
            "session_state_updated",
            user_id=user_id,
            thread_id=current.thread_id,
            state=state.value,
        )
        return True

    async def release_lock(self, user_id: int) -> bool:
        """Release session lock (on task completion)."""
        r = await self._get_redis()
        key = self._lock_key(user_id)

        deleted = await r.delete(key)

        if deleted:
            logger.debug("session_lock_released", user_id=user_id)

        return bool(deleted)

    async def refresh_timeout(self, user_id: int) -> bool:
        """Refresh session timeout (on new user message)."""
        r = await self._get_redis()
        key = self._lock_key(user_id)
        return bool(await r.expire(key, SESSION_TIMEOUT_SECONDS))

    async def start_new_session(self, user_id: int) -> str:
        """Start a new session: release old, generate new thread_id, acquire lock.

        Returns new thread_id.
        """
        # Release any existing lock
        await self.release_lock(user_id)

        # Generate new thread_id
        thread_id = await generate_thread_id(user_id)

        # Acquire lock
        await self.acquire_lock(user_id, thread_id)

        logger.info("new_session_started", user_id=user_id, thread_id=thread_id)
        return thread_id

    async def continue_session(self, user_id: int) -> str | None:
        """Continue existing session if awaiting user response.

        Returns thread_id if can continue, None if should start new.
        """
        session = await self.get_session(user_id)

        if not session:
            return None

        if session.state == SessionState.AWAITING:
            # Update state to processing and refresh timeout
            await self.update_state(user_id, SessionState.PROCESSING)
            await self.refresh_timeout(user_id)
            logger.debug(
                "session_continued",
                user_id=user_id,
                thread_id=session.thread_id,
            )
            return session.thread_id

        # Session is PROCESSING - cannot continue
        return None

    async def is_locked(self, user_id: int) -> tuple[bool, SessionState | None]:
        """Check if session is locked.

        Returns (is_locked, state).
        """
        session = await self.get_session(user_id)
        if not session:
            return False, None
        return True, session.state


# Global instance
session_manager = SessionManager()
```

---

### 5.2 Worker.py Updates

**Location**: `services/langgraph/src/worker.py`

**Changes:**
1. Integrate SessionManager
2. Handle locked sessions
3. Update session state based on graph result
4. Clear conversation history on finish_task

```python
# Add imports
from .session_manager import session_manager, SessionState

async def process_message(redis_client: RedisStreamClient, data: dict) -> None:
    """Process a single message through the LangGraph.

    Flow:
    1. Check session lock
       - If PROCESSING -> reject with "busy" message
       - If AWAITING -> continue session
       - If no lock -> start new session
    2. Run graph
    3. Update session state based on result
    """
    telegram_user_id = data.get("user_id")
    chat_id = data.get("chat_id")
    text = data.get("text", "")
    correlation_id = data.get("correlation_id")

    # === Session Lock Check ===
    is_locked, lock_state = await session_manager.is_locked(telegram_user_id)

    if is_locked:
        if lock_state == SessionState.PROCESSING:
            # Reject - system is busy
            await redis_client.publish(
                RedisStreamClient.OUTGOING_STREAM,
                {
                    "user_id": telegram_user_id,
                    "chat_id": chat_id,
                    "text": "⏳ Подожди, я ещё обрабатываю предыдущий запрос...",
                    "correlation_id": correlation_id,
                },
            )
            logger.info("message_rejected_busy", telegram_user_id=telegram_user_id)
            return

        elif lock_state == SessionState.AWAITING:
            # Continue existing session
            thread_id = await session_manager.continue_session(telegram_user_id)
            skip_intent_parser = True
        else:
            # Unknown state, treat as new session
            thread_id = await session_manager.start_new_session(telegram_user_id)
            skip_intent_parser = False
    else:
        # No active session - start new
        thread_id = await session_manager.start_new_session(telegram_user_id)
        skip_intent_parser = False

    # ... rest of processing logic ...

    try:
        # Run the graph
        result = await graph.ainvoke(state, config)

        # === Update Session State Based on Result ===
        if result.get("user_confirmed_complete"):
            # Task complete - release lock, clear history
            await session_manager.release_lock(telegram_user_id)
            if thread_id in conversation_history:
                del conversation_history[thread_id]
            logger.info("session_completed", thread_id=thread_id)

        elif result.get("awaiting_user_response"):
            # Waiting for user - update state to AWAITING
            await session_manager.update_state(telegram_user_id, SessionState.AWAITING)
            logger.info("session_awaiting_user", thread_id=thread_id)

        else:
            # Graph ended but not complete and not awaiting
            # This could be max iterations or other end condition
            # Release lock, user can start fresh
            await session_manager.release_lock(telegram_user_id)
            logger.info("session_ended_naturally", thread_id=thread_id)

        # ... send response ...

    except Exception as e:
        # On error, release lock to prevent stuck sessions
        await session_manager.release_lock(telegram_user_id)
        # ... error handling ...
```

---

### 5.3 Graph.py Updates

**Location**: `services/langgraph/src/graph.py`

**Changes:**
1. Ensure PO tools properly update `awaiting_user_response` and `user_confirmed_complete`
2. Add validation that prevents invalid state transitions

Current `product_owner.execute_tools` needs to check tool results:

```python
# In services/langgraph/src/nodes/product_owner.py

async def execute_tools(state: OrchestratorState) -> dict:
    """Execute PO tool calls and update state accordingly."""
    messages = state.get("messages", [])
    # ... tool execution logic ...

    updates = {
        "messages": messages + [tool_result_messages],
        "po_iterations": state.get("po_iterations", 0) + 1,
    }

    # Check for special tool results
    for tool_name, result in executed_tools:
        if tool_name == "respond_to_user":
            if result.get("awaiting"):
                updates["awaiting_user_response"] = True

        elif tool_name == "finish_task":
            if result.get("finished"):
                updates["user_confirmed_complete"] = True

        elif tool_name == "request_capabilities":
            if result.get("enabled"):
                updates["active_capabilities"] = result["enabled"]

    return updates
```

---

### 5.4 Base Tools State Access

**Problem**: Tools need access to current state (user_id, chat_id, thread_id) but tools are stateless.

**Solution**: Use LangGraph's `InjectedState` pattern or tool context.

**Location**: `services/langgraph/src/capabilities/base.py`

```python
"""Base tools with state injection."""

from typing import Annotated

from langchain_core.tools import InjectedToolArg, tool

from shared.redis_client import RedisStreamClient


@tool
async def respond_to_user(
    message: str,
    awaiting_response: bool = False,
    # Injected by LangGraph - not visible to LLM
    state: Annotated[dict, InjectedToolArg],
) -> dict:
    """
    Send a message to the user via Telegram.

    Args:
        message: Text to send (supports markdown)
        awaiting_response: If True, pause and wait for user reply.

    Returns:
        {"sent": True, "awaiting": awaiting_response}
    """
    telegram_user_id = state.get("telegram_user_id")
    chat_id = state.get("chat_id")
    correlation_id = state.get("correlation_id")

    if not telegram_user_id or not chat_id:
        return {"error": "Missing user context", "sent": False}

    redis = RedisStreamClient()
    await redis.connect()

    try:
        await redis.publish(
            RedisStreamClient.OUTGOING_STREAM,
            {
                "user_id": telegram_user_id,
                "chat_id": chat_id,
                "text": message,
                "correlation_id": correlation_id,
            },
        )
    finally:
        await redis.close()

    return {"sent": True, "awaiting": awaiting_response}


@tool
async def finish_task(
    summary: str,
    state: Annotated[dict, InjectedToolArg],
) -> dict:
    """
    Mark the current task as complete.

    IMPORTANT: Only call after user confirms the task is done.

    Args:
        summary: Brief summary of what was accomplished

    Returns:
        {"finished": True, "thread_id": "...", "summary": "..."}
    """
    import structlog

    logger = structlog.get_logger()

    thread_id = state.get("thread_id")
    telegram_user_id = state.get("telegram_user_id")

    logger.info(
        "task_finished",
        thread_id=thread_id,
        summary=summary,
        user_id=telegram_user_id,
    )

    return {
        "finished": True,
        "thread_id": thread_id,
        "summary": summary,
    }


@tool
def request_capabilities(
    capabilities: list[str],
    reason: str,
    state: Annotated[dict, InjectedToolArg],
) -> dict:
    """
    Request additional tools for the current task.

    Available capabilities:
        - "deploy": Deploy projects
        - "infrastructure": Manage servers and ports
        - "project_management": Create/update projects
        - "engineering": Trigger code implementation
        - "diagnose": View logs, debug issues
        - "admin": Manual system control

    Args:
        capabilities: List of capability groups to enable
        reason: Brief explanation why needed

    Returns:
        {"enabled": [...], "new_tools": [...]}
    """
    from . import CAPABILITY_REGISTRY

    # Validate capabilities
    valid_caps = set(CAPABILITY_REGISTRY.keys())
    requested = set(capabilities)
    invalid = requested - valid_caps

    if invalid:
        return {"error": f"Unknown capabilities: {invalid}"}

    # Get current and merge
    current = set(state.get("active_capabilities", []))
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

---

### 5.5 Telegram Bot Updates (Optional Enhancements)

**Location**: `services/telegram_bot/src/main.py`

**Enhancements:**
1. Typing indicator while processing
2. Session info command

```python
# In handlers or new file

async def show_typing_while_processing(chat_id: int, bot):
    """Send typing action periodically."""
    while True:
        await bot.send_chat_action(chat_id, "typing")
        await asyncio.sleep(4)  # Telegram typing lasts ~5 seconds


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming messages."""
    chat_id = update.effective_chat.id

    # Start typing indicator
    typing_task = asyncio.create_task(
        show_typing_while_processing(chat_id, context.bot)
    )

    try:
        # Publish to LangGraph via Redis
        await redis_client.publish(...)

        # Wait for response (with timeout)
        # ...
    finally:
        typing_task.cancel()


# Optional: /session command to show current session status
async def session_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current session status."""
    user_id = update.effective_user.id

    session = await session_manager.get_session(user_id)

    if not session:
        await update.message.reply_text("📭 Нет активной сессии")
        return

    status_text = {
        SessionState.PROCESSING: "⚙️ Обработка...",
        SessionState.AWAITING: "💬 Ожидаю ответа",
    }

    await update.message.reply_text(
        f"📋 *Сессия*\n\n"
        f"ID: `{session.thread_id}`\n"
        f"Статус: {status_text.get(session.state, 'Неизвестно')}\n"
        f"Начата: {session.locked_at.strftime('%H:%M:%S')}",
        parse_mode="MarkdownV2",
    )
```

---

## Phase 6: RAG Integration

### 6.1 Search Knowledge Implementation

**Location**: `services/langgraph/src/capabilities/base.py`

```python
@tool
async def search_knowledge(
    query: str,
    scope: str = "all",
    state: Annotated[dict, InjectedToolArg],
) -> dict:
    """
    Search project documentation, code, conversation history, or logs.

    Args:
        query: Natural language search query
        scope: Where to search
            - "docs": Project documentation and specs
            - "code": Source code in repositories
            - "history": Previous conversations
            - "logs": Service and deployment logs
            - "all": Search everywhere

    Returns:
        {"results": [{"source": "...", "content": "...", "relevance": 0.95}, ...]}
    """
    from ..clients.api import api_client

    user_id = state.get("user_id")  # Internal DB user ID
    current_project = state.get("current_project")

    results = []
    errors = []

    # Scope handlers
    async def search_history():
        """Search conversation summaries (already implemented)."""
        if not user_id:
            return []
        try:
            summaries = await api_client.get(
                f"rag/search?query={query}&user_id={user_id}&limit=5"
            )
            return [
                {
                    "source": "history",
                    "content": s["summary_text"],
                    "relevance": s.get("similarity", 0.8),
                    "metadata": {"created_at": s.get("created_at")},
                }
                for s in summaries
            ]
        except Exception as e:
            errors.append(f"history: {e}")
            return []

    async def search_docs():
        """Search project documentation (specs, TASK.md, README)."""
        if not current_project:
            return []
        try:
            docs = await api_client.get(
                f"rag/docs/search?query={query}&project_id={current_project}&limit=5"
            )
            return [
                {
                    "source": "docs",
                    "content": d["content"],
                    "relevance": d.get("similarity", 0.8),
                    "metadata": {"file": d.get("file_path")},
                }
                for d in docs
            ]
        except Exception as e:
            errors.append(f"docs: {e}")
            return []

    async def search_logs():
        """Search service logs (recent errors, events)."""
        if not current_project:
            return []
        try:
            # Use diagnose tool's log search
            from ..tools.diagnose import get_error_history

            error_data = await get_error_history.ainvoke({
                "project_id": current_project,
                "hours": 24,
            })
            if error_data.get("errors"):
                return [
                    {
                        "source": "logs",
                        "content": f"Error ({e['count']}x): {e['message']}",
                        "relevance": 0.7,
                        "metadata": {"count": e["count"]},
                    }
                    for e in error_data["errors"][:5]
                ]
        except Exception as e:
            errors.append(f"logs: {e}")
        return []

    async def search_code():
        """Search source code (GitHub API)."""
        if not current_project:
            return []
        try:
            # Get repo info
            project = await api_client.get(f"projects/{current_project}")
            repo_url = project.get("config", {}).get("repo_url")
            if not repo_url:
                return []

            # GitHub code search
            code_results = await api_client.get(
                f"rag/code/search?query={query}&repo_url={repo_url}&limit=5"
            )
            return [
                {
                    "source": "code",
                    "content": c["content"],
                    "relevance": c.get("similarity", 0.7),
                    "metadata": {"file": c.get("file_path"), "line": c.get("line")},
                }
                for c in code_results
            ]
        except Exception as e:
            errors.append(f"code: {e}")
            return []

    # Execute based on scope
    if scope == "all":
        import asyncio
        all_results = await asyncio.gather(
            search_history(),
            search_docs(),
            search_logs(),
            search_code(),
        )
        for r in all_results:
            results.extend(r)
    elif scope == "history":
        results = await search_history()
    elif scope == "docs":
        results = await search_docs()
    elif scope == "logs":
        results = await search_logs()
    elif scope == "code":
        results = await search_code()
    else:
        return {"error": f"Unknown scope: {scope}. Use: all, history, docs, logs, code"}

    # Sort by relevance
    results.sort(key=lambda x: x.get("relevance", 0), reverse=True)

    return {
        "results": results[:10],  # Top 10
        "total_found": len(results),
        "errors": errors if errors else None,
    }
```

---

### 6.2 RAG API Endpoints

**Location**: `services/api/src/routers/rag.py`

Add new endpoints for search:

```python
@router.get("/search")
async def search_summaries(
    query: str,
    user_id: int,
    limit: int = 5,
    db: AsyncSession = Depends(get_db),
):
    """Search conversation summaries using vector similarity."""
    from ..services.embedding import get_embedding

    embedding = await get_embedding(query)

    # pgvector similarity search
    results = await db.execute(
        select(ConversationSummary)
        .where(ConversationSummary.user_id == user_id)
        .order_by(ConversationSummary.embedding.cosine_distance(embedding))
        .limit(limit)
    )

    summaries = results.scalars().all()

    return [
        {
            "summary_text": s.summary_text,
            "created_at": s.created_at.isoformat(),
            "similarity": 1 - s.embedding.cosine_distance(embedding),  # Convert to similarity
        }
        for s in summaries
    ]


@router.get("/docs/search")
async def search_docs(
    query: str,
    project_id: str,
    limit: int = 5,
    db: AsyncSession = Depends(get_db),
):
    """Search project documentation using vector similarity."""
    from ..services.embedding import get_embedding

    embedding = await get_embedding(query)

    # Assuming ProjectDoc model with embeddings
    results = await db.execute(
        select(ProjectDoc)
        .where(ProjectDoc.project_id == project_id)
        .order_by(ProjectDoc.embedding.cosine_distance(embedding))
        .limit(limit)
    )

    docs = results.scalars().all()

    return [
        {
            "content": d.content,
            "file_path": d.file_path,
            "similarity": 1 - d.embedding.cosine_distance(embedding),
        }
        for d in docs
    ]


@router.get("/code/search")
async def search_code(
    query: str,
    repo_url: str,
    limit: int = 5,
):
    """Search code via GitHub API.

    Note: For MVP, uses GitHub's code search API.
    Future: Index code with embeddings for better semantic search.
    """
    from ..services.github import github_client

    # Extract owner/repo from URL
    # https://github.com/owner/repo -> owner/repo
    parts = repo_url.rstrip("/").split("/")
    repo = f"{parts[-2]}/{parts[-1]}"

    results = await github_client.search_code(repo, query, limit)

    return [
        {
            "content": r["text_match"],
            "file_path": r["path"],
            "line": r.get("line"),
            "similarity": 0.8,  # GitHub doesn't provide similarity score
        }
        for r in results
    ]
```

---

### 6.3 Document Embeddings Model

**Location**: `services/api/src/models/project_doc.py` (new file)

```python
"""Project documentation model for RAG."""

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID

from .base import Base


class ProjectDoc(Base):
    """Indexed project documentation for semantic search."""

    __tablename__ = "project_docs"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    project_id = Column(String, ForeignKey("projects.id"), nullable=False, index=True)

    # Document info
    file_path = Column(String, nullable=False)  # e.g., "TASK.md", "docs/api.md"
    content = Column(Text, nullable=False)
    content_hash = Column(String(64), nullable=False)  # SHA256 for dedup

    # Embedding (1536 dimensions for OpenAI ada-002)
    embedding = Column(Vector(1536))

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Index for similarity search
    __table_args__ = (
        # Create index for cosine similarity search
        # Note: Requires pgvector extension
    )
```

**Migration**: Add `project_docs` table

```python
# alembic/versions/xxx_add_project_docs.py

def upgrade():
    op.create_table(
        "project_docs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id")),
        sa.Column("file_path", sa.String()),
        sa.Column("content", sa.Text()),
        sa.Column("content_hash", sa.String(64)),
        sa.Column("embedding", Vector(1536)),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )

    # Create vector index
    op.execute("""
        CREATE INDEX project_docs_embedding_idx
        ON project_docs
        USING ivfflat (embedding vector_cosine_ops)
        WITH (lists = 100)
    """)
```

---

### 6.4 Document Indexer

**Location**: `services/scheduler/src/tasks/doc_indexer.py` (new file)

```python
"""Background task to index project documentation."""

import hashlib

import structlog

from ..clients.api import api_client
from ..clients.github import github_client
from ..services.embedding import get_embedding

logger = structlog.get_logger()

# Files to index
INDEXABLE_FILES = [
    "TASK.md",
    "README.md",
    "docs/**/*.md",
    "*.yaml",
    "*.yml",
]


async def index_project_docs(project_id: str):
    """Index documentation for a project."""
    logger.info("indexing_project_docs", project_id=project_id)

    # Get project repo info
    project = await api_client.get(f"projects/{project_id}")
    repo_url = project.get("config", {}).get("repo_url")

    if not repo_url:
        logger.warning("project_no_repo", project_id=project_id)
        return

    # Fetch files from GitHub
    for pattern in INDEXABLE_FILES:
        files = await github_client.get_files(repo_url, pattern)

        for file in files:
            content = file["content"]
            content_hash = hashlib.sha256(content.encode()).hexdigest()

            # Check if already indexed (same hash)
            existing = await api_client.get(
                f"rag/docs?project_id={project_id}&file_path={file['path']}"
            )
            if existing and existing.get("content_hash") == content_hash:
                continue  # Already indexed

            # Generate embedding
            embedding = await get_embedding(content)

            # Store
            await api_client.post(
                "rag/docs",
                json={
                    "project_id": project_id,
                    "file_path": file["path"],
                    "content": content,
                    "content_hash": content_hash,
                    "embedding": embedding,
                },
            )

            logger.info(
                "doc_indexed",
                project_id=project_id,
                file_path=file["path"],
            )


async def run_indexer():
    """Run indexer for all projects."""
    projects = await api_client.get("projects")

    for project in projects:
        try:
            await index_project_docs(project["id"])
        except Exception as e:
            logger.error(
                "indexer_failed",
                project_id=project["id"],
                error=str(e),
            )
```

---

## Testing

### Unit Tests

**Location**: `services/langgraph/tests/unit/`

```python
# test_session_manager.py

import pytest
from unittest.mock import AsyncMock, patch

from src.session_manager import SessionManager, SessionState


@pytest.fixture
def session_manager():
    return SessionManager()


@pytest.fixture
def mock_redis():
    with patch("src.session_manager.redis") as mock:
        mock_client = AsyncMock()
        mock.from_url.return_value = mock_client
        yield mock_client


class TestSessionManager:
    async def test_acquire_lock_success(self, session_manager, mock_redis):
        mock_redis.set.return_value = True

        result = await session_manager.acquire_lock(123, "thread_1")

        assert result is True
        mock_redis.set.assert_called_once()

    async def test_acquire_lock_already_locked(self, session_manager, mock_redis):
        mock_redis.set.return_value = False

        result = await session_manager.acquire_lock(123, "thread_1")

        assert result is False

    async def test_continue_session_awaiting(self, session_manager, mock_redis):
        mock_redis.get.return_value = json.dumps({
            "thread_id": "thread_1",
            "state": "awaiting",
            "locked_at": datetime.now(UTC).isoformat(),
        })

        result = await session_manager.continue_session(123)

        assert result == "thread_1"

    async def test_continue_session_processing(self, session_manager, mock_redis):
        mock_redis.get.return_value = json.dumps({
            "thread_id": "thread_1",
            "state": "processing",
            "locked_at": datetime.now(UTC).isoformat(),
        })

        result = await session_manager.continue_session(123)

        assert result is None  # Cannot continue, session is processing
```

```python
# test_worker_session_handling.py

import pytest
from unittest.mock import AsyncMock, patch

from src.worker import process_message


class TestWorkerSessionHandling:
    async def test_rejects_message_when_processing(self):
        """User sends message while previous is still processing."""
        redis_client = AsyncMock()

        with patch("src.worker.session_manager") as mock_sm:
            mock_sm.is_locked.return_value = (True, SessionState.PROCESSING)

            await process_message(redis_client, {
                "user_id": 123,
                "chat_id": 456,
                "text": "hello",
            })

            # Should publish rejection message
            redis_client.publish.assert_called_once()
            call_args = redis_client.publish.call_args
            assert "обрабатываю" in call_args[0][1]["text"]

    async def test_continues_session_when_awaiting(self):
        """User responds while session is awaiting."""
        redis_client = AsyncMock()

        with patch("src.worker.session_manager") as mock_sm:
            mock_sm.is_locked.return_value = (True, SessionState.AWAITING)
            mock_sm.continue_session.return_value = "thread_123"

            with patch("src.worker.graph") as mock_graph:
                mock_graph.ainvoke.return_value = {"messages": [...]}

                await process_message(redis_client, {
                    "user_id": 123,
                    "chat_id": 456,
                    "text": "yes, proceed",
                })

                # Should continue with same thread
                mock_sm.continue_session.assert_called_with(123)
```

### Integration Tests

**Location**: `services/langgraph/tests/integration/`

```python
# test_dynamic_po_flow.py

import pytest
from unittest.mock import AsyncMock

from src.graph import create_graph
from src.session_manager import session_manager


@pytest.fixture
async def clean_session():
    """Ensure no leftover sessions."""
    yield
    # Cleanup handled by test


class TestDynamicPOFlow:
    """Integration tests for full Dynamic PO flow."""

    async def test_simple_question_flow(self):
        """User asks simple question, gets answer, says thanks."""
        graph = create_graph()

        # 1. Initial question
        state1 = {
            "messages": [HumanMessage(content="Какие проекты у меня есть?")],
            "telegram_user_id": 123,
            "skip_intent_parser": False,
            # ... other fields
        }

        result1 = await graph.ainvoke(state1, {"configurable": {"thread_id": "test_1"}})

        # Should have project_management capability
        assert "project_management" in result1.get("active_capabilities", [])
        # Should not be awaiting
        assert not result1.get("awaiting_user_response")

    async def test_deploy_flow_with_confirmation(self):
        """User requests deploy, PO asks for confirmation, user confirms."""
        graph = create_graph()

        # 1. Deploy request
        state1 = {
            "messages": [HumanMessage(content="Задеплой hello-world-bot")],
            "telegram_user_id": 123,
            "skip_intent_parser": False,
        }

        result1 = await graph.ainvoke(state1, {"configurable": {"thread_id": "test_2"}})

        # Should have deploy capability
        assert "deploy" in result1.get("active_capabilities", [])

        # 2. Simulate user confirmation (continue session)
        state2 = {
            **result1,
            "messages": result1["messages"] + [HumanMessage(content="Да, деплой")],
            "skip_intent_parser": True,  # Continuation
            "awaiting_user_response": False,  # Reset
        }

        result2 = await graph.ainvoke(state2, {"configurable": {"thread_id": "test_2"}})

        # Should have triggered deploy or asked more questions
        # (depends on project state)

    async def test_max_iterations_safeguard(self):
        """Ensure graph stops after max iterations."""
        graph = create_graph()

        state = {
            "messages": [HumanMessage(content="Do something infinite")],
            "telegram_user_id": 123,
            "skip_intent_parser": True,
            "po_iterations": 19,  # Start near limit
        }

        result = await graph.ainvoke(state, {"configurable": {"thread_id": "test_3"}})

        # Should have stopped
        assert result.get("po_iterations", 0) <= 20
```

---

## Checklist

### Phase 5: Integration

**5.1 Session Manager**
- [ ] Create `services/langgraph/src/session_manager.py`
- [ ] Implement `SessionManager` class with Redis backend
- [ ] Add `acquire_lock`, `release_lock`, `update_state` methods
- [ ] Add `continue_session`, `start_new_session` methods
- [ ] Add session timeout via Redis TTL

**5.2 Worker Integration**
- [ ] Update `worker.py` to use SessionManager
- [ ] Handle PROCESSING state (reject new messages)
- [ ] Handle AWAITING state (continue session)
- [ ] Update session state after graph execution
- [ ] Clear conversation history on `finish_task`
- [ ] Release lock on errors

**5.3 Graph Updates**
- [ ] Verify `product_owner.execute_tools` updates flags correctly
- [ ] Ensure `awaiting_user_response` is set from `respond_to_user`
- [x] Ensure `user_confirmed_complete` is set from `finish_task`
- [x] Ensure `active_capabilities` is set from `request_capabilities`

**5.4 Base Tools Updates**
- [ ] Add `InjectedToolArg` pattern for state access
- [ ] Update `respond_to_user` to publish via Redis
- [ ] Update `finish_task` to log completion
- [ ] Update `request_capabilities` to validate and merge

**5.5 Telegram Enhancements (Optional)**
- [ ] Add typing indicator while processing
- [ ] Add `/session` command for status
- [ ] Improve error messages

**5.6 Testing**
- [ ] Unit tests for SessionManager
- [ ] Unit tests for worker session handling
- [x] Integration test: simple question flow
- [x] Integration test: deploy flow with confirmation
- [ ] Integration test: error handling flow
- [ ] Integration test: session timeout

### Phase 6: RAG Integration

**6.1 Search Knowledge Implementation**
- [ ] Implement full `search_knowledge` tool
- [ ] Add scope handlers: history, docs, logs, code
- [ ] Integrate with API client

**6.2 API Endpoints**
- [ ] Add `GET /rag/search` for similarity search
- [ ] Add `GET /rag/docs/search` for doc search
- [ ] Add `GET /rag/code/search` for code search

**6.3 Data Model**
- [ ] Create `ProjectDoc` model with embedding column
- [ ] Add migration for `project_docs` table
- [ ] Add vector index for similarity search

**6.4 Document Indexer**
- [ ] Create indexer task in scheduler
- [ ] Implement file fetching from GitHub
- [ ] Implement embedding generation
- [ ] Add deduplication via content hash

**6.5 Testing**
- [ ] Unit tests for search_knowledge tool
- [ ] Integration test: search history
- [ ] Integration test: search docs
- [ ] E2E test: PO uses search_knowledge in conversation

---

## Open Questions

| Question | Status | Decision |
|----------|--------|----------|
| Queue vs reject messages during processing? | **Decided** | Reject with notification |
| Session timeout duration? | **Decided** | 30 minutes (configurable) |
| Code search: GitHub API vs embeddings? | **Decided** | GitHub API for MVP, embeddings later |
| Document indexing frequency? | **TBD** | On commit webhook? Daily? |
| Max results from search_knowledge? | **Decided** | 10 results |

---

## Dependencies

**External:**
- Redis (already used)
- pgvector extension (already installed)
- OpenAI Embeddings API (for document embeddings)

**Internal:**
- Phase 1-4 complete (Thread ID, Capabilities, Base Tools, Capability Tools)
- API service running
- Scheduler service running

---

## Next Steps

After Phase 5-6:
1. Monitoring: Add metrics for session lifecycle, RAG usage
2. Cost tracking: Monitor embedding API costs
3. Performance: Tune vector index parameters
4. UX: Add session recovery after timeouts
