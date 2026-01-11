# Package: Worker Wrapper

**Package Name:** `worker-wrapper`
**Location:** `packages/worker-wrapper/`
**Responsibility:** Redis ↔ CLI-Agent bridge inside Worker containers.

## 1. Philosophy

The wrapper is the **nervous system** of a Worker container. It connects the external world (Redis) to the internal brain (CLI-Agent).

> **Rule #1:** Wrapper is the ONLY process that talks to Redis.
> **Rule #2:** CLI-Agent (Claude/Factory) runs in headless mode — no interactive TTY.
> **Rule #3:** PO Workers manage session continuity; Developer Workers are stateless.

## 2. Responsibilities

1. **Listen**: Subscribes to worker input Redis stream.
2. **Process**: Forwards messages to CLI-Agent (Claude Code / Factory Droid).
3. **Output Capture**: Collect agent output (stdout).
4. **Result Parsing**: Extract `<result>...</result>` JSON block.
5. **Enrichment**: Wrap verdict with telemetry (start/end time, duration).
6. **Output**: Publish results to `worker:developer:output` (code/verdict).
7. **Lifecycle Reporting**: Publish system state to `worker:lifecycle` (Started/Ready/Failed).
8. **Graceful Shutdown**: Handle SIGTERM, cleanup, report exit.

## 2.1 Queue Naming by Worker Type

| Worker Type | Input Queue | Output Queue | Notes |
|-------------|-------------|--------------|-------|
| **PO** | `worker:po:{worker_id}:input` | `worker:po:{worker_id}:output` | Per-user queues, long-lived session |
| **Developer** | `worker:developer:input` | `worker:developer:output` | **Shared queues**, ephemeral (stateless) |

**Why the difference?**
- **PO Workers** are long-lived and handle ongoing conversations with a specific user.
- **Developer Workers** are ephemeral — spawned per task, process one job, exit. Stateless: context is code in repo + error messages.

## 3. Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    WORKER CONTAINER                         │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│   ┌─────────────────────────────────────────────────────┐   │
│   │                 worker-wrapper (PID 1)              │   │
│   │                                                     │   │
│   │  1. XREAD $INPUT_QUEUE (blocking)                   │   │
│   │            │                                        │   │
│   │            ▼                                        │   │
│   │  2. Parse message: { prompt, timeout, ... }         │   │
│   │            │                                        │   │
│   │            ▼                                        │   │
│   │  3. Invoke CLI-Agent (headless subprocess)          │   │
│   │     claude --headless --session $SESSION --prompt   │   │
│   │            │                                        │   │
│   │            ▼                                        │   │
│   │  4. Wait for completion (with timeout)              │   │
│   │            │                                        │   │
│   │            ▼                                        │   │
│   │  5. XADD $OUTPUT_QUEUE { response }                 │   │
│   │            │                                        │   │
│   │            ▼                                        │   │
│   │  6. Loop back to step 1 (PO) or exit (Developer)    │   │
│   └─────────────────────────────────────────────────────┘   │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

> Queue names are determined by worker type — see table in section 2.1.

## 4. Message Formats

### 4.1 Input Message

```python
class WorkerInputMessage(BaseModel):
    """Message sent to worker."""
    
    request_id: str              # Unique ID for this request
    prompt: str                  # The message/command for the agent
    timeout: int = 1800          # Max execution time (seconds)
    # Plus other fields depending on PO (user_id) or Developer (task_id, project_id) context
    # See CONTRACTS.md for specific POWorkerInput / DeveloperWorkerInput schemas
    session_continue: bool = True  # Continue existing session
```

### 4.2 Output Message (`WorkerResult`)

See `CONTRACTS.md` for full DTO.

1.  Wrapper looks for regex: `<result>(.*?)</result>` (DOTALL).
2.  Parses JSON payload.
3.  Constructs appropriate DTO based on `WORKER_TYPE`:
    *   **PO**: `POWorkerOutput` (requires `text`).
    *   **Developer**: `DeveloperWorkerOutput` (requires `commit_sha`, `pr_url`, or `verdict`).
4.  Pushes to Redis.

### 4.3 Lifecycle Events (`worker:lifecycle`)

```python
class WorkerLifecycleEvent(BaseModel):
    """Worker lifecycle notification."""
    
    worker_id: str
    event: Literal["started", "ready", "busy", "completed", "failed", "stopped"]
    timestamp: datetime
    details: dict = {}           # Additional context (error, result summary)

> **Note**: Events `busy` and `completed` trigger `last_activity` update in Worker Manager to prevent Auto-Pause.
```

## 5. Agent Invocation

### 5.1 Claude Code (Headless)

```bash
claude \
    --headless \
    --session-id $SESSION_ID \
    --prompt "$PROMPT" \
    --output-format json \
    --timeout $TIMEOUT
```

**Environment:**
- `ANTHROPIC_API_KEY` — API key
- `SESSION_ID` — persistent session for context continuity

### 5.2 Factory Droid (Headless)

```bash
factory \
    --headless \
    --session $SESSION_ID \
    --message "$PROMPT" \
    --format json
```

**Environment:**
- `OPENAI_API_KEY` — API key
- `FACTORY_MODEL` — model selection

### 5.3 Agent Detection

Wrapper detects which agent to use via environment:

```python
AGENT_TYPE = os.environ.get("AGENT_TYPE", "claude")  # claude | factory
```

## 6. Session Management

Sessions enable context continuity across multiple messages within the same logical task.

### 6.1 Session Lifecycle

```
User Message #1 → [No session] → Create NEW session_id
User Message #2 → [Session exists, active] → Reuse session_id
User Message #3 → [Session exists, active] → Reuse session_id
... 30 min no activity ...
User Message #4 → [Session timeout] → Create NEW session_id
```

### 6.2 Session Storage (Redis)

Session state is managed by **Telegram Bot** (not wrapper):

```
session:user:{telegram_id} = {
    "session_id": "uuid",
    "worker_id": "container-id", 
    "created_at": "2026-01-10T12:00:00Z",
    "last_activity": "2026-01-10T12:05:00Z"
}
TTL: 24 hours
```

### 6.3 MVP: Timeout-Based Rotation

For MVP, session rotation is **simple**:

| Condition | Action |
|-----------|--------|
| No existing session | Create new `session_id` |
| `last_activity` > 30 min ago | Create new `session_id` |
| Worker container dead | Create new worker, **keep** `session_id` |
| Otherwise | Reuse `session_id` |

Claude internally manages context for the given session_id.

### 6.4 Post-MVP: Advanced Rotation

> **TODO (Post-MVP):** Implement smarter session rotation:
> 
> 1. **Context Overflow**: Track token usage from Claude API responses.
>    When `total_tokens > 100K`, start new session + RAG summary of old context.
> 
> 2. **Topic Change Detection**: Use embedding similarity between messages.
>    If similarity < threshold, consider new task → new session.
> 
> 3. **Explicit Commands**: User can force new session via `/new` command.

### 6.5 Session ID Flow

```
Telegram Bot                    Worker-Manager                  Worker-Wrapper
     │                               │                               │
     │ 1. get_or_create_session()    │                               │
     │    → session_id = "abc123"    │                               │
     │                               │                               │
     │ 2. create_worker(             │                               │
     │      env: SESSION_ID=abc123)  │                               │
     │ ─────────────────────────────►│                               │
     │                               │ 3. docker run -e SESSION_ID   │
     │                               │ ──────────────────────────────►
     │                               │                               │
     │ 4. send_message(prompt)       │                               │
     │ ─────────────────────────────────────────────────────────────►│
     │                               │                               │
     │                               │    5. claude --session abc123 │
     │                               │                               │
```

## 7. Error Handling

| Error | Handling |
|-------|----------|
| Agent timeout | Kill subprocess, return `status=timeout` |
| Agent crash (non-zero exit) | Return `status=error` with stderr |
| Redis disconnect | Retry with backoff, exit after 5 failures. *Note: Docker Pause freezes the process, so TCP connection might break during long pause. Wrapper MUST handle reconnection on unpause.* |
| SIGTERM received | Finish current request, cleanup, exit |

## 8. Dependencies

**Minimal:**
- `redis` (async)
- `pydantic`
- `structlog`

**System:**
- CLI-Agent binary (claude / factory) — installed separately or in base image

## 9. Dockerfile Integration

```dockerfile
# In worker-base Dockerfile

# Install wrapper
COPY packages/worker-wrapper /app/worker-wrapper
RUN pip install --no-cache-dir /app/worker-wrapper

# Entry point IS the wrapper
ENTRYPOINT ["python", "-m", "worker_wrapper"]
```

## 10. Configuration (Environment)

| Variable | Required | Description |
|----------|----------|-------------|
| `WORKER_ID` | Yes | Container identifier |
| `WORKER_TYPE` | Yes | Worker type: `po` or `developer` |
| `REDIS_URL` | Yes | Redis connection string |
| `AGENT_TYPE` | Yes | `claude` or `factory` |
| `SESSION_ID` | Yes | Session ID from Telegram Bot |
| `ANTHROPIC_API_KEY` | If Claude | Claude API key |
| `OPENAI_API_KEY` | If Factory | OpenAI API key |

## 11. Implementation Sketch

```python
# packages/worker-wrapper/src/worker_wrapper/main.py

import asyncio
import os
import subprocess
from redis.asyncio import Redis

async def main():
    worker_id = os.environ["WORKER_ID"]
    worker_type = os.environ["WORKER_TYPE"]  # "po" or "developer"
    redis = Redis.from_url(os.environ["REDIS_URL"])
    agent_type = os.environ.get("AGENT_TYPE", "claude")
    session_id = os.environ.get("SESSION_ID", str(uuid.uuid4()))
    
    if worker_type == "po":
        # PO uses unique queues per user/session
        input_stream = f"worker:po:{worker_id}:input"
        output_stream = f"worker:po:{worker_id}:output"
    else:
        # Developer uses shared queues
        input_stream = "worker:developer:input"
        output_stream = "worker:developer:output"
    
    # Report ready
    await publish_lifecycle(redis, worker_id, "ready")
    
    while True:
        # 1. Wait for message
        messages = await redis.xread({input_stream: "$"}, block=0)
        
        for stream, entries in messages:
            for entry_id, data in entries:
                request = WorkerInputMessage.model_validate(json.loads(data["data"]))
                
                # 2. Report busy
                await publish_lifecycle(redis, worker_id, "busy")
                
                # 3. Invoke agent
                result = await invoke_agent(agent_type, session_id, request)
                
                # 4. Publish response
                await redis.xadd(output_stream, {"data": result.model_dump_json()})
                
                # 5. Report ready
                await publish_lifecycle(redis, worker_id, "ready")


async def invoke_agent(agent_type: str, session_id: str, request: WorkerInputMessage):
    if agent_type == "claude":
        cmd = ["claude", "--headless", "--session-id", session_id, "--prompt", request.prompt]
    else:
        cmd = ["factory", "--headless", "--session", session_id, "--message", request.prompt]
    
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=request.timeout
        )
        
        if proc.returncode == 0:
            # Parse <result>...</result> from stdout
            result_json = parse_xml_result(stdout.decode())
            
            if worker_type == "po":
                 return POWorkerOutput(
                    request_id=request.request_id,
                    user_id=int(os.environ["USER_ID"]),
                    text=result_json.get("text", stdout.decode())
                 )
            else: # developer
                 return DeveloperWorkerOutput(
                    request_id=request.request_id,
                    task_id=request.task_id,
                    status="success",
                    commit_sha=result_json.get("commit_sha"),
                    pr_url=result_json.get("pr_url")
                 )
        else:
            # Handle Error
            ...
```

## 12. Notes

- **Session storage**: Claude manages sessions internally. We only pass `--session-id`.
- **Streaming**: MVP waits for full completion. Streaming is Post-MVP enhancement.
- **Context**: Claude maintains full context in session. No explicit passing needed.
